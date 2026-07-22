"""R8.1 policy envelope: declarative, mechanically-enforced merge guardrails.

Machine-made merge decisions are only defensible inside an explicit,
written envelope. Before this module the rules were implicit and
scattered (diff-scope, allowed paths, bad-pattern secrets). The
``[policy]`` section makes them one auditable thing that the Phase 1
mechanical verifier enforces on ARTIFACTS - the git diff and ``uv.lock`` -
never on agent self-report.

Opt-in by design: ``PolicyConfig.enabled`` defaults False, so existing
runs are unchanged. When a repo opts in, a violation fails Phase 1 and
blocks the merge. The autonomy ladder (R8.2) will later modulate
severity per level; today "enabled" means "blocking".

This module is pure logic. All git I/O lives in ``kstrl.verify`` (which
wraps :func:`evaluate_policy` into a ``CheckResult``); keeping the
detection functions free of subprocesses makes every category unit
testable without a repository. License gating (``license_allow`` /
``license_deny_partial``) is intentionally deferred to a follow-up: it
needs dist-metadata resolution that ``uv.lock`` does not carry, measured
separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path

# Enforcement-machinery paths: the levers an agent could pull to weaken
# the envelope itself - the CI that runs the checks and the config file
# that defines them. Modifying any of these is a non-overridable hard
# fail whenever the envelope is enabled, independent of ``paths_deny``
# and of the autonomy level (R8.2). Hardcoded on purpose: a repo cannot
# opt out of protecting its own guardrails.
ENFORCEMENT_MACHINERY_PATHS: tuple[str, ...] = (
    ".github/workflows/**",
    "kstrl.toml",
    "ralph.toml",
)

# Conservative default deny-list written by ``ks init``. Repo-owned: each
# repo carries its own envelope so policy cannot drift silently.
DEFAULT_PATHS_DENY: tuple[str, ...] = (
    ".github/workflows/**",
    "kstrl.toml",
    "ralph.toml",
    ".kstrl/**",
    "**/*.pem",
    "**/.env*",
)

# Default secret regexes, matched against ADDED diff lines across every
# changed file (broader than the .py-only ``check_bad_patterns`` scan).
DEFAULT_SECRET_PATTERNS: tuple[str, ...] = (
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----",
    r"sk-[a-zA-Z0-9]{20,}",
    r"ghp_[a-zA-Z0-9]{36}",
    r"xox[bpoas]-[a-zA-Z0-9-]+",
)

# Basenames of machine-generated lockfiles, excluded from the size caps:
# a one-line dependency bump can rewrite hundreds of lockfile lines, so
# counting them would make ``max_lines_changed`` meaningless. Lockfiles
# remain subject to ``paths_deny`` and ``deps_allow_new``.
LOCKFILE_BASENAMES: frozenset[str] = frozenset({
    "uv.lock", "poetry.lock", "Pipfile.lock",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock",
})

_UVLOCK_NAME_RE = re.compile(r'^name = "([^"]+)"')


class PolicyConfigError(ValueError):
    """A policy value is itself malformed (e.g. an uncompilable secret
    regex). The verifier turns this into a fail-CLOSED check: a broken
    envelope must never silently pass a diff."""


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value == "1"


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if value is None else int(value)


def _glob_to_regex(pattern: str) -> str:
    """Translate a gitignore-style glob to an anchored regex string.

    ``**`` crosses directory separators; a ``**/`` segment matches zero
    or more leading directories (so ``**/*.pem`` matches both ``key.pem``
    and ``a/b/key.pem``); ``*`` matches within a single path segment;
    ``?`` matches one non-separator character.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return "^" + "".join(out) + "$"


def _match_glob(path: str, patterns: Sequence[str]) -> str | None:
    """Return the first pattern matching ``path``, else None."""
    for pattern in patterns:
        if re.match(_glob_to_regex(pattern), path):
            return pattern
    return None


def parse_added_lines(diff_text: str) -> list[tuple[str, str]]:
    """Extract ``(path, added_line)`` pairs from unified-diff text.

    The destination file is tracked from ``+++ b/<path>`` headers; added
    lines are those starting with a single ``+`` (not the ``+++``
    header). Content is returned without the leading ``+``.
    """
    added: list[tuple[str, str]] = []
    current: str | None = None
    prev = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            current = None
        elif line.startswith("+++ ") and prev.startswith("--- "):
            # Real file header: git always emits the '--- ' / '+++ ' pair.
            # Gating on the preceding '--- ' means an ADDED content line
            # that happens to render as '+++ ...' is treated as content,
            # not misread as a new file header.
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            current = None if target == "/dev/null" else target
        elif line.startswith("+"):
            if current is not None:
                added.append((current, line[1:]))
        prev = line
    return added


def _new_dependencies(added_lines: Sequence[tuple[str, str]]) -> list[str]:
    """Top-level package names newly added to ``uv.lock``.

    Each ``[[package]]`` stanza owns a column-0 ``name = "..."`` line; a
    version bump of an existing package touches only its ``version``
    line, so an ADDED ``name = "..."`` line reliably marks a genuinely
    new dependency. Inline dependency refs (``{ name = "x" }``) are
    indented and never match the column-0 anchor.
    """
    names: list[str] = []
    for path, line in added_lines:
        if _basename(path) == "uv.lock":
            match = _UVLOCK_NAME_RE.match(line)
            if match:
                names.append(match.group(1))
    return names


def _scan_secrets(
    added_lines: Sequence[tuple[str, str]], patterns: Sequence[str],
) -> set[str]:
    """Return paths whose added lines match any secret pattern.

    A pattern that will not compile is a policy misconfiguration, raised
    as :class:`PolicyConfigError` so the check fails closed rather than
    silently scanning with fewer patterns.
    """
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise PolicyConfigError(
                f"invalid secret_pattern {pattern!r}: {exc}"
            ) from exc
    hits: set[str] = set()
    for path, line in added_lines:
        for regex in compiled:
            if regex.search(line):
                hits.add(path)
                break
    return hits


@dataclass(frozen=True)
class PolicyEvaluation:
    """Outcome of evaluating a change against the envelope.

    ``machinery_hit`` is surfaced separately from ``ok`` because touching
    enforcement machinery is the one violation that cannot be relaxed by
    config or by autonomy level.
    """

    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)
    machinery_hit: bool = False


def evaluate_policy(
    changed_files: Sequence[str],
    numstat: Sequence[tuple[int | None, int | None, str]],
    diff_text: str,
    config: PolicyConfig,
) -> PolicyEvaluation:
    """Evaluate a change against the policy envelope from artifacts alone.

    ``changed_files`` is the rename-aware path list; ``numstat`` is
    ``(added, removed, path)`` per file (None counts = binary); and
    ``diff_text`` is the unified diff used for secret and new-dependency
    detection. Returns every violation found; the verifier renders them
    into the ``CheckResult`` details.
    """
    details: list[str] = []

    # 1. Enforcement-machinery halt (non-overridable, reported first).
    machinery = [
        f for f in changed_files
        if _match_glob(f, ENFORCEMENT_MACHINERY_PATHS)
    ]
    machinery_hit = bool(machinery)
    if machinery_hit:
        details.append(
            "HALT: enforcement-machinery paths modified (non-overridable, "
            "blocks at every autonomy level): " + ", ".join(sorted(machinery))
        )

    # 2. Denied paths (configurable; machinery paths already reported).
    machinery_set = set(machinery)
    deny_hits: list[str] = []
    for path in changed_files:
        if path in machinery_set:
            continue
        pattern = _match_glob(path, config.paths_deny)
        if pattern:
            deny_hits.append(f"{path} (deny '{pattern}')")
    if deny_hits:
        details.append("Denied paths modified: " + "; ".join(sorted(deny_hits)))

    # 3. Size caps (lockfiles excluded from the count).
    counted = [
        (added, removed, path)
        for (added, removed, path) in numstat
        if _basename(path) not in LOCKFILE_BASENAMES
    ]
    n_files = len(counted)
    n_lines = sum((added or 0) + (removed or 0) for (added, removed, _p) in counted)
    if config.max_files_changed >= 0 and n_files > config.max_files_changed:
        details.append(
            f"Too many files changed: {n_files} > max_files_changed "
            f"{config.max_files_changed} (lockfiles excluded)"
        )
    if config.max_lines_changed >= 0 and n_lines > config.max_lines_changed:
        details.append(
            f"Too many lines changed: {n_lines} > max_lines_changed "
            f"{config.max_lines_changed} (lockfiles excluded)"
        )

    # 4. New dependencies (uv.lock), when disallowed.
    added_lines = parse_added_lines(diff_text)
    if not config.deps_allow_new:
        new_deps = sorted(set(_new_dependencies(added_lines)))
        if new_deps:
            shown = ", ".join(new_deps[:20])
            if len(new_deps) > 20:
                shown += f", ... (+{len(new_deps) - 20} more)"
            details.append(
                f"New dependencies added while deps_allow_new=false: {shown}"
            )

    # 5. Secret patterns over added lines (raises on a bad regex).
    secret_hits = _scan_secrets(added_lines, config.secret_patterns)
    if secret_hits:
        details.append(
            "Possible secrets in added lines: " + ", ".join(sorted(secret_hits))
        )

    ok = not details
    if ok:
        summary = (
            f"policy envelope satisfied ({n_files} files, {n_lines} lines, "
            "within limits)"
        )
    else:
        summary = f"{len(details)} policy violation(s)"
        if machinery_hit:
            summary += " including enforcement-machinery halt"
    return PolicyEvaluation(
        ok=ok, summary=summary, details=details, machinery_hit=machinery_hit,
    )


@dataclass(frozen=True)
class PolicyConfig:
    """Declarative merge-policy envelope (R8.1), read from ``[policy]``.

    Opt-in: ``enabled`` defaults False so existing runs are unchanged.
    When enabled, a violation fails Phase 1 mechanical verification and
    blocks the merge. All checks read artifacts (git diff, ``uv.lock``),
    never agent self-report. Set a numeric cap negative to disable it.
    """

    enabled: bool = False
    paths_deny: list[str] = field(
        default_factory=lambda: list(DEFAULT_PATHS_DENY)
    )
    max_files_changed: int = 40
    max_lines_changed: int = 1500
    deps_allow_new: bool = False
    secret_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_SECRET_PATTERNS)
    )
    # Reserved for the R8.7 release gate: stored and hashed into the run
    # manifest's policy envelope, not yet enforced (Phase 1 has no deploy
    # step). L3+ may set true.
    deploy: bool = False

    @classmethod
    def from_env(cls) -> PolicyConfig:
        """Load from environment only (defaults + env overlay).

        List fields (``paths_deny``, ``secret_patterns``) are toml-only
        and keep their defaults here.
        """
        defaults = cls()
        return cls(
            enabled=_env_bool("KSTRL_POLICY_ENABLED", defaults.enabled),
            paths_deny=list(defaults.paths_deny),
            max_files_changed=_env_int(
                "KSTRL_POLICY_MAX_FILES", defaults.max_files_changed
            ),
            max_lines_changed=_env_int(
                "KSTRL_POLICY_MAX_LINES", defaults.max_lines_changed
            ),
            deps_allow_new=_env_bool(
                "KSTRL_POLICY_DEPS_ALLOW_NEW", defaults.deps_allow_new
            ),
            secret_patterns=list(defaults.secret_patterns),
            deploy=_env_bool("KSTRL_POLICY_DEPLOY", defaults.deploy),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> PolicyConfig:
        """Load with precedence: env > toml > defaults.

        Reads the ``[policy]`` section from ``<root_dir>/kstrl.toml``,
        then overlays explicitly-set env vars. List fields are toml-only.
        """
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "policy")
        defaults = cls()

        enabled = (
            bool(section["enabled"]) if "enabled" in section else defaults.enabled
        )
        paths_deny = (
            [str(p) for p in section["paths_deny"]]
            if isinstance(section.get("paths_deny"), list)
            else list(defaults.paths_deny)
        )
        max_files_changed = (
            int(section["max_files_changed"])
            if "max_files_changed" in section
            else defaults.max_files_changed
        )
        max_lines_changed = (
            int(section["max_lines_changed"])
            if "max_lines_changed" in section
            else defaults.max_lines_changed
        )
        deps_allow_new = (
            bool(section["deps_allow_new"])
            if "deps_allow_new" in section
            else defaults.deps_allow_new
        )
        secret_patterns = (
            [str(s) for s in section["secret_patterns"]]
            if isinstance(section.get("secret_patterns"), list)
            else list(defaults.secret_patterns)
        )
        deploy = (
            bool(section["deploy"]) if "deploy" in section else defaults.deploy
        )

        # Env overrides (scalars/bools only; lists are toml-only).
        if "KSTRL_POLICY_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_POLICY_ENABLED"] == "1"
        if "KSTRL_POLICY_MAX_FILES" in os.environ:
            max_files_changed = int(os.environ["KSTRL_POLICY_MAX_FILES"])
        if "KSTRL_POLICY_MAX_LINES" in os.environ:
            max_lines_changed = int(os.environ["KSTRL_POLICY_MAX_LINES"])
        if "KSTRL_POLICY_DEPS_ALLOW_NEW" in os.environ:
            deps_allow_new = os.environ["KSTRL_POLICY_DEPS_ALLOW_NEW"] == "1"
        if "KSTRL_POLICY_DEPLOY" in os.environ:
            deploy = os.environ["KSTRL_POLICY_DEPLOY"] == "1"

        return cls(
            enabled=enabled,
            paths_deny=paths_deny,
            max_files_changed=max_files_changed,
            max_lines_changed=max_lines_changed,
            deps_allow_new=deps_allow_new,
            secret_patterns=secret_patterns,
            deploy=deploy,
        )

    def envelope_hash(self) -> str:
        """SHA-256 of the resolved envelope for the run manifest.

        Hashes the effective config (post env/toml resolution), so the
        audit record captures what was ENFORCED, not merely what the file
        on disk said.
        """
        payload = {f.name: getattr(self, f.name) for f in fields(self)}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
