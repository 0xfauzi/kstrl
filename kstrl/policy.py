"""R8.1 policy envelope: declarative, mechanically-enforced merge guardrails.

Machine-made merge decisions are only defensible inside an explicit,
written envelope. Before this module the rules were implicit and
scattered (diff-scope, allowed paths, bad-pattern secrets). The
``[policy]`` section makes them one auditable thing that the Phase 1
mechanical verifier enforces on the git diff, never on agent
self-report. It reads no lockfile and no license registry (#696): the
security reviewer lists every dependency a change adds.

Opt-in by design: ``PolicyConfig.enabled`` defaults False, so existing
runs are unchanged. When a repo opts in, a violation fails Phase 1 and
blocks the merge. The autonomy ladder (R8.2) will later modulate
severity per level; today "enabled" means "blocking".

This module is pure logic. All git I/O lives in ``kstrl.verify``, which
wraps :func:`evaluate_policy` into a ``CheckResult``; keeping the
detection functions free of subprocesses makes every category testable
without a repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path

from kstrl.config_numbers import SIGNED, check_numbers

# Enforcement-machinery paths: every lever an agent could pull to weaken
# the envelope itself. Issue #148 names three surfaces and all three are
# covered here: the policy FILE that defines the rules, the CI WORKFLOWS
# that run the gates, and the VERIFIER CODE that implements them -
# rewriting `check_policy_envelope` defeats the envelope just as
# thoroughly as deleting the config.
#
# Modifying any of these is a non-overridable hard fail whenever the
# envelope is enabled, independent of ``paths_deny`` and of the autonomy
# level (R8.2). Hardcoded on purpose: a repo can ADD to this set via
# ``enforcement_paths_extra`` but can never remove from it, so an agent
# editing `kstrl.toml` cannot widen its own permissions.
ENFORCEMENT_MACHINERY_PATHS: tuple[str, ...] = (
    # CI that runs the gates
    ".github/workflows/**",
    # the policy file itself (both spellings)
    "kstrl.toml",
    "ralph.toml",
    # verifier code: the Phase 1 mechanical enforcement surface. The
    # leading `**/` matches zero or more directories, so these cover both
    # `kstrl/verify.py` at the repo root and a nested/vendored checkout.
    "**/kstrl/verify.py",
    "**/kstrl/scrubbed_run.py",
    "**/kstrl/verify_model.py",
    "**/kstrl/verify_commands.py",
    "**/kstrl/verify_diff.py",
    "**/kstrl/policy.py",
    "**/kstrl/guards.py",
    "**/kstrl/autonomy.py",
    "**/kstrl/statedir.py",
    "**/kstrl/waivers.py",  # #595: turns an approved item into a waiver
    # R8.2 / R8.9 control-plane state. Live copies live under XDG
    # (outside the tree); these legacy in-tree paths stay in the halt
    # set so a diff cannot recreate agent-editable control files.
    "**/.kstrl/autonomy.json",
    ".kstrl/autonomy.json",
    "**/.kstrl/inbox.jsonl",
    ".kstrl/inbox.jsonl",
    "**/.kstrl/queue/spend.json",
    ".kstrl/queue/spend.json",
    "**/.kstrl/queue/pause.json",
    ".kstrl/queue/pause.json",
    "**/.kstrl/queue/github_processed.json",
    ".kstrl/queue/github_processed.json",
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
# changed file, here and in ``check_bad_patterns`` (#619).
DEFAULT_SECRET_PATTERNS: tuple[str, ...] = (
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----",
    r"sk-[a-zA-Z0-9]{20,}",
    r"ghp_[a-zA-Z0-9]{36}",
    r"xox[bpoas]-[a-zA-Z0-9-]+",
)


class PolicyConfigError(ValueError):
    """A policy value is itself malformed (e.g. an uncompilable secret
    regex). The verifier turns this into a fail-CLOSED check: a broken
    envelope must never silently pass a diff."""


def count_diff_size(
    numstat: Sequence[tuple[int | None, int | None, str]],
) -> tuple[int, int]:
    """``(files, lines)`` for a ``git diff --numstat`` result.

    Every file counts, a lockfile or other generated file included: kstrl
    holds no list of which files a toolchain generates (#696).

    ``lines`` is lines ADDED PLUS REMOVED, git's own sense of "lines
    changed", so deleting pre-existing code raises it. It measures churn,
    not the artifact getting bigger, and callers must not describe it as
    growth of the tree.

    Shared by the R8.1 size caps and the #265 divergence detector, which
    must agree about how large a change is. Binary files report ``-`` for
    both counts (``None`` here) and so contribute their file but no lines.
    """
    return len(numstat), sum((added or 0) + (removed or 0) for added, removed, _ in numstat)


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


def unquote_diff_path(path: str) -> str:
    """Undo git's C-quoting of a ``+++``/``--- `` diff header path (#399).

    ``core.quotepath`` (default true) wraps a path in double quotes and
    escapes anything unusual: a non-ASCII byte as a backslash-octal escape,
    and a literal backslash, double quote or control character (a tab
    included) with its own single-character escape. ``git diff
    --name-status`` never quotes the SAME path, so the two commands can
    spell one file differently unless this is undone wherever a diff
    header path is read.

    ``bytes.decode("unicode_escape")`` decodes both escape forms to code
    points below 256 - a Latin-1 view of the original UTF-8 bytes -
    re-encoding as Latin-1 and decoding as UTF-8 recovers the real
    characters. Only a path git actually quoted (wrapped in ``"..."``)
    goes through this; an unquoted path is returned unchanged.

    Public so another reader of a diff header can share it (#408).
    """
    if not (path.startswith('"') and path.endswith('"') and len(path) >= 2):
        return path
    return path[1:-1].encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")


def diff_header_path(header: str) -> str:
    """The path from a whole ``---``/``+++`` diff header line (#408).

    Unquoted BEFORE git's ``a/``/``b/`` prefix is removed. C-quoting
    wraps the WHOLE ``b/<path>`` token in double quotes, so a quoted
    header does not start with ``b/`` at all and stripping first would
    be a no-op, leaving the quotes, the octal escapes and the prefix all
    in place. ``/dev/null`` is never quoted and never carries a prefix,
    so it passes through unchanged either way. This is the one place
    :func:`parse_added_lines` reads a header path.
    """
    path = unquote_diff_path(header[4:].strip())
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_added_lines(diff_text: str) -> list[tuple[str, str]]:
    """Extract ``(path, added_line)`` pairs from unified-diff text.

    The destination file is tracked from ``+++ b/<path>`` headers; added
    lines are those starting with a single ``+`` (not the ``+++``
    header). Content is returned without the leading ``+``. The header
    path is read through :func:`diff_header_path`, so it matches what
    ``git diff --name-status`` reports for the same file.
    """
    added: list[tuple[str, str]] = []
    current: str | None = None
    prev = ""
    # git ends a line at "\n" only (#695). str.splitlines() also splits at
    # "\r", "\x0c", "\x1c" and more, so a key after one of those on an added
    # line lost its "+" and was not scanned. A CRLF line keeps its old hash.
    for raw in diff_text.split("\n"):
        line = raw.removesuffix("\r")
        if line.startswith("diff --git"):
            current = None
        elif line.startswith("+++ ") and prev.startswith("--- "):
            # Real file header: git always emits the '--- ' / '+++ ' pair.
            # Gating on the preceding '--- ' means an ADDED content line
            # that happens to render as '+++ ...' is treated as content,
            # not misread as a new file header.
            target = diff_header_path(line)
            current = None if target == "/dev/null" else target
        elif line.startswith("+"):
            if current is not None:
                added.append((current, line[1:]))
        prev = line
    return added


def _scan_secrets(
    added_lines: Sequence[tuple[str, str]],
    patterns: Sequence[str],
) -> dict[str, set[str]]:
    """Map each path whose added lines match a secret pattern to those lines' hashes.

    Each hash is the first 12 hex characters of the line's sha256, so two
    secrets in one file are two findings (#595: an approval covers the
    explanation, which lists them); the plaintext is in the diff anyway.

    A pattern that will not compile is a policy misconfiguration, raised
    as :class:`PolicyConfigError` so the check fails closed rather than
    silently scanning with fewer patterns.
    """
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise PolicyConfigError(f"invalid secret_pattern {pattern!r}: {exc}") from exc
    hits: dict[str, set[str]] = {}
    for path, line in added_lines:
        matched = False  # for/break beats any(): 1.8x faster at 100k lines
        for regex in compiled:
            if regex.search(line):
                matched = True
                break
        if matched:
            # surrogateescape gives back the bytes git printed (#695): a line
            # from a binary file is not utf-8, and a utf-8 line hashes as before.
            digest = hashlib.sha256(line.encode("utf-8", "surrogateescape")).hexdigest()[:12]
            hits.setdefault(path, set()).add(digest)
    return hits


@dataclass(frozen=True)
class PolicyViolation:
    """One envelope rule that fired, in structured form.

    Kept separate from the rendered ``details`` string so the verifier can
    build typed ``Finding``s (issue #148) without re-parsing prose.
    ``category`` is the rule name (``paths_deny``, ``secret_pattern``);
    ``severity`` is ``critical`` for the enforcement-machinery halt,
    ``high`` for other blocking violations, ``advisory`` for notices that
    do not block.
    """

    category: str
    explanation: str
    location: str = ""
    severity: str = "high"
    suggestion: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity != "advisory"


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
    # Structured form of ``details`` for typed Finding construction.
    violations: list[PolicyViolation] = field(default_factory=list)


def evaluate_policy(
    changed_files: Sequence[str],
    numstat: Sequence[tuple[int | None, int | None, str]],
    diff_text: str,
    config: PolicyConfig,
) -> PolicyEvaluation:
    """Evaluate a change against the policy envelope from the diff alone.

    ``changed_files`` is the rename-aware path list; ``numstat`` is
    ``(added, removed, path)`` per file (None counts = binary); and
    ``diff_text`` is the unified diff the secret patterns read.
    Returns every violation found, both as structured
    :class:`PolicyViolation`s (for typed Findings) and as rendered
    ``details`` strings (for the retry prompt).
    """
    violations: list[PolicyViolation] = []

    # 1. Enforcement-machinery halt (non-overridable, reported first).
    # The configurable extras can only ADD to the hardcoded set.
    machinery_patterns = list(ENFORCEMENT_MACHINERY_PATHS) + list(config.enforcement_paths_extra)
    machinery = [f for f in changed_files if _match_glob(f, machinery_patterns)]
    machinery_hit = bool(machinery)
    if machinery_hit:
        violations.append(
            PolicyViolation(
                category="enforcement_machinery",
                severity="critical",
                location=", ".join(sorted(machinery)[:5]),
                explanation=(
                    "HALT: enforcement-machinery paths modified (non-overridable, "
                    "blocks at every autonomy level): " + ", ".join(sorted(machinery))
                ),
                suggestion=(
                    "Revert these paths. Changes to the policy file, CI "
                    "workflows, or verifier code must be made by a human, "
                    "never inside an automated run."
                ),
            )
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
        violations.append(
            PolicyViolation(
                category="paths_deny",
                location=", ".join(sorted(p.split(" (")[0] for p in deny_hits)[:5]),
                explanation="Denied paths modified: " + "; ".join(sorted(deny_hits)),
                suggestion="Revert these paths or widen [policy] paths_deny.",
            )
        )

    # 3. Size caps.
    n_files, n_lines = count_diff_size(numstat)
    if config.max_files_changed >= 0 and n_files > config.max_files_changed:
        violations.append(
            PolicyViolation(
                category="max_files_changed",
                explanation=(
                    f"Too many files changed: {n_files} > max_files_changed "
                    f"{config.max_files_changed}"
                ),
                suggestion="Split the change into smaller components.",
            )
        )
    if config.max_lines_changed >= 0 and n_lines > config.max_lines_changed:
        violations.append(
            PolicyViolation(
                category="max_lines_changed",
                explanation=(
                    f"Too many lines changed: {n_lines} > max_lines_changed "
                    f"{config.max_lines_changed}"
                ),
                suggestion="Split the change into smaller components.",
            )
        )

    # 4. Secret patterns over added lines (raises on a bad regex).
    secret_hits = _scan_secrets(parse_added_lines(diff_text), config.secret_patterns)
    if secret_hits:
        violations.append(
            PolicyViolation(
                category="secret_pattern",
                location=", ".join(sorted(secret_hits)[:5]),
                explanation=(
                    "Possible secrets in added lines: "
                    + ", ".join(
                        f"{path}#{','.join(sorted(secret_hits[path]))}"
                        for path in sorted(secret_hits)
                    )
                ),
                suggestion=(
                    "Remove the credential and rotate it; load secrets from the "
                    "environment instead."
                ),
            )
        )

    details = [v.explanation for v in violations]
    ok = not any(v.blocking for v in violations)
    if ok:
        summary = f"policy envelope satisfied ({n_files} files, {n_lines} lines, within limits)"
    else:
        summary = f"{len(details)} policy violation(s)"
        if machinery_hit:
            summary += " including enforcement-machinery halt"
    return PolicyEvaluation(
        ok=ok,
        summary=summary,
        details=details,
        machinery_hit=machinery_hit,
        violations=violations,
    )


@dataclass(frozen=True)
class PolicyConfig:
    """Declarative merge-policy envelope (R8.1), read from ``[policy]``.

    Opt-in: ``enabled`` defaults False so existing runs are unchanged.
    When enabled, a violation fails Phase 1 mechanical verification and
    blocks the merge. All checks read the git diff, never agent
    self-report. Set a numeric cap negative to disable it.
    """

    enabled: bool = False
    paths_deny: list[str] = field(default_factory=lambda: list(DEFAULT_PATHS_DENY))
    # A negative cap disables the cap and 0 allows nothing (#571: SIGNED).
    max_files_changed: int = field(default=40, metadata=SIGNED)
    max_lines_changed: int = field(default=1500, metadata=SIGNED)
    secret_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_SECRET_PATTERNS))
    # ADDITIVE ONLY: extra paths joined to ENFORCEMENT_MACHINERY_PATHS for
    # the non-overridable halt. A repo protects its own verifier/CI code
    # here; nothing in config can shrink the hardcoded set.
    enforcement_paths_extra: list[str] = field(default_factory=list)
    # Reserved for the R8.7 release gate: stored and hashed into the run
    # manifest's policy envelope, not yet enforced (Phase 1 has no deploy
    # step). L3+ may set true.
    deploy: bool = False

    @classmethod
    def from_env(cls) -> PolicyConfig:
        """Load from environment only (defaults + env overlay).

        List fields (``paths_deny``, ``secret_patterns``,
        ``enforcement_paths_extra``) are toml-only and keep their
        defaults here.
        """
        defaults = cls()
        return cls(
            enabled=_env_bool("KSTRL_POLICY_ENABLED", defaults.enabled),
            paths_deny=list(defaults.paths_deny),
            max_files_changed=_env_int("KSTRL_POLICY_MAX_FILES", defaults.max_files_changed),
            max_lines_changed=_env_int("KSTRL_POLICY_MAX_LINES", defaults.max_lines_changed),
            secret_patterns=list(defaults.secret_patterns),
            enforcement_paths_extra=list(defaults.enforcement_paths_extra),
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

        enabled = bool(section["enabled"]) if "enabled" in section else defaults.enabled
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
        secret_patterns = (
            [str(s) for s in section["secret_patterns"]]
            if isinstance(section.get("secret_patterns"), list)
            else list(defaults.secret_patterns)
        )
        enforcement_paths_extra = (
            [str(s) for s in section["enforcement_paths_extra"]]
            if isinstance(section.get("enforcement_paths_extra"), list)
            else list(defaults.enforcement_paths_extra)
        )
        deploy = bool(section["deploy"]) if "deploy" in section else defaults.deploy

        # Env overrides (scalars/bools only; lists are toml-only).
        if "KSTRL_POLICY_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_POLICY_ENABLED"] == "1"
        if "KSTRL_POLICY_MAX_FILES" in os.environ:
            max_files_changed = int(os.environ["KSTRL_POLICY_MAX_FILES"])
        if "KSTRL_POLICY_MAX_LINES" in os.environ:
            max_lines_changed = int(os.environ["KSTRL_POLICY_MAX_LINES"])
        if "KSTRL_POLICY_DEPLOY" in os.environ:
            deploy = os.environ["KSTRL_POLICY_DEPLOY"] == "1"

        return check_numbers(
            cls(
                enabled=enabled,
                paths_deny=paths_deny,
                max_files_changed=max_files_changed,
                max_lines_changed=max_lines_changed,
                secret_patterns=secret_patterns,
                enforcement_paths_extra=enforcement_paths_extra,
                deploy=deploy,
            )
        )

    def envelope_hash(self) -> str:
        """SHA-256 of the resolved envelope for the run manifest.

        Hashes the effective config (post env/toml resolution), so the
        audit record captures what was ENFORCED, not merely what the file
        on disk said. Every knob that can change a verdict is a field on
        this dataclass, so two runs with the same hash enforced the same
        rules (an env-only toggle would otherwise let a weaker run claim
        an unchanged envelope).
        """
        payload = {f.name: getattr(self, f.name) for f in fields(self)}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
