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

This module is pure logic. All git and license-resolution I/O lives in
``kstrl.verify`` / ``kstrl.licensing`` (which wrap :func:`evaluate_policy`
and :func:`classify_license` into a ``CheckResult``); keeping the
detection functions free of subprocesses and network makes every
category unit testable without a repository. License resolution itself
(uv cache, then PyPI) lives in :mod:`kstrl.licensing`; this module only
classifies an already-resolved SPDX string against the allow/deny lists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path

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
    "**/kstrl/policy.py",
    "**/kstrl/licensing.py",
    "**/kstrl/guards.py",
    "**/kstrl/fixtures.py",
    "**/kstrl/autonomy.py",
    # R8.2 ladder state. Editing it IS editing the factory's own
    # permissions, so it belongs to the enforcement surface: the CLI
    # promotion path demands an interactive terminal, and this closes the
    # obvious way around that (write the level straight to disk).
    "**/.kstrl/autonomy.json",
    ".kstrl/autonomy.json",
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

# Conservative permissive-license allowlist (exact SPDX ids). A new
# dependency whose license is not covered here (and not caught by the
# deny-list) blocks until the operator explicitly adds it - the
# "explicit allowlist" posture the roadmap specifies.
DEFAULT_LICENSE_ALLOW: tuple[str, ...] = (
    "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0",
    "ISC", "PSF-2.0", "Python-2.0", "Unlicense", "0BSD",
)

# Substrings that deny a license outright (copyleft / source-available).
# "GPL" also matches "LGPL"/"AGPL" by design: deny wins, and the operator
# narrows it if a weak-copyleft dep is acceptable.
DEFAULT_LICENSE_DENY_PARTIAL: tuple[str, ...] = (
    "GPL", "AGPL", "SSPL", "Commons-Clause", "BUSL", "EUPL",
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
_UVLOCK_VERSION_RE = re.compile(r'^version = "([^"]+)"')

# SPDX expression operators dropped when tokenizing into license atoms.
_SPDX_OPERATORS = frozenset({"or", "and", "with"})


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


def parse_new_dependencies(
    added_lines: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """``(name, version)`` for packages newly added to ``uv.lock``.

    A new ``[[package]]`` stanza adds a column-0 ``name = "..."`` line
    immediately followed by ``version = "..."``; a version bump of an
    existing package adds only the ``version`` line (its name line is
    unchanged context), so pairing an added name with the next added
    version isolates genuinely new packages. Inline dependency refs
    (``{ name = "x" }``) are indented and never match the column-0 anchor.
    """
    deps: list[tuple[str, str]] = []
    pending: str | None = None
    for path, line in added_lines:
        if _basename(path) != "uv.lock":
            continue
        name_match = _UVLOCK_NAME_RE.match(line)
        if name_match:
            pending = name_match.group(1)
            continue
        version_match = _UVLOCK_VERSION_RE.match(line)
        if version_match and pending is not None:
            deps.append((pending, version_match.group(1)))
            pending = None
    return deps


def _spdx_atoms(expr: str) -> list[str]:
    """Split an SPDX expression into license atoms, dropping operators.

    ``"Apache-2.0 OR BSD-3-Clause"`` -> ``["Apache-2.0", "BSD-3-Clause"]``.
    """
    tokens = re.split(r"[()\s]+", expr.strip())
    return [t for t in tokens if t and t.lower() not in _SPDX_OPERATORS]


def classify_license(
    license_str: str | None,
    allow: Sequence[str],
    deny_partial: Sequence[str],
) -> str:
    """Classify a resolved license as ``allowed`` / ``denied`` / ``unknown``.

    Deny wins: a ``deny_partial`` substring anywhere in the string (case-
    insensitive) denies it - this is what catches copyleft even inside a
    compound or ``WITH``-exception expression. Otherwise every atom of the
    (possibly compound) expression must be in ``allow`` to be allowed;
    anything else - including an unresolved (None) license - is unknown.
    """
    if not license_str:
        return "unknown"
    low = license_str.lower()
    for deny in deny_partial:
        if deny.lower() in low:
            return "denied"
    atoms = _spdx_atoms(license_str)
    allow_low = {a.lower() for a in allow}
    if atoms and all(a.lower() in allow_low for a in atoms):
        return "allowed"
    return "unknown"


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
class PolicyViolation:
    """One envelope rule that fired, in structured form.

    Kept separate from the rendered ``details`` string so the verifier can
    build typed ``Finding``s (issue #148) without re-parsing prose.
    ``category`` is the rule name (``paths_deny``, ``license_denied``);
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
    # (name, version) of packages newly added to uv.lock, so the verifier
    # can resolve their licenses without re-parsing the diff.
    new_dependencies: list[tuple[str, str]] = field(default_factory=list)
    # Structured form of ``details`` for typed Finding construction.
    violations: list[PolicyViolation] = field(default_factory=list)


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
    detection. Returns every violation found, both as structured
    :class:`PolicyViolation`s (for typed Findings) and as rendered
    ``details`` strings (for the retry prompt).
    """
    violations: list[PolicyViolation] = []

    # 1. Enforcement-machinery halt (non-overridable, reported first).
    # The configurable extras can only ADD to the hardcoded set.
    machinery_patterns = (
        list(ENFORCEMENT_MACHINERY_PATHS) + list(config.enforcement_paths_extra)
    )
    machinery = [f for f in changed_files if _match_glob(f, machinery_patterns)]
    machinery_hit = bool(machinery)
    if machinery_hit:
        violations.append(PolicyViolation(
            category="enforcement_machinery",
            severity="critical",
            location=", ".join(sorted(machinery)[:5]),
            explanation=(
                "HALT: enforcement-machinery paths modified (non-overridable, "
                "blocks at every autonomy level): "
                + ", ".join(sorted(machinery))
            ),
            suggestion=(
                "Revert these paths. Changes to the policy file, CI "
                "workflows, or verifier code must be made by a human, "
                "never inside an automated run."
            ),
        ))

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
        violations.append(PolicyViolation(
            category="paths_deny",
            location=", ".join(sorted(p.split(" (")[0] for p in deny_hits)[:5]),
            explanation="Denied paths modified: " + "; ".join(sorted(deny_hits)),
            suggestion="Revert these paths or widen [policy] paths_deny.",
        ))

    # 3. Size caps (lockfiles excluded from the count).
    counted = [
        (added, removed, path)
        for (added, removed, path) in numstat
        if _basename(path) not in LOCKFILE_BASENAMES
    ]
    n_files = len(counted)
    n_lines = sum((added or 0) + (removed or 0) for (added, removed, _p) in counted)
    if config.max_files_changed >= 0 and n_files > config.max_files_changed:
        violations.append(PolicyViolation(
            category="max_files_changed",
            explanation=(
                f"Too many files changed: {n_files} > max_files_changed "
                f"{config.max_files_changed} (lockfiles excluded)"
            ),
            suggestion="Split the change into smaller components.",
        ))
    if config.max_lines_changed >= 0 and n_lines > config.max_lines_changed:
        violations.append(PolicyViolation(
            category="max_lines_changed",
            explanation=(
                f"Too many lines changed: {n_lines} > max_lines_changed "
                f"{config.max_lines_changed} (lockfiles excluded)"
            ),
            suggestion="Split the change into smaller components.",
        ))

    # 4. New dependencies (uv.lock). Detected regardless of deps_allow_new
    # so the verifier can license-check them; only blocked here when
    # deps_allow_new is false.
    added_lines = parse_added_lines(diff_text)
    new_dependencies = parse_new_dependencies(added_lines)
    if not config.deps_allow_new and new_dependencies:
        names = sorted({name for name, _v in new_dependencies})
        shown = ", ".join(names[:20])
        if len(names) > 20:
            shown += f", ... (+{len(names) - 20} more)"
        violations.append(PolicyViolation(
            category="deps_allow_new",
            location="uv.lock",
            explanation=(
                f"New dependencies added while deps_allow_new=false: {shown}"
            ),
            suggestion=(
                "Drop the dependency, or set [policy] deps_allow_new = true."
            ),
        ))

    # 5. Secret patterns over added lines (raises on a bad regex).
    secret_hits = _scan_secrets(added_lines, config.secret_patterns)
    if secret_hits:
        violations.append(PolicyViolation(
            category="secret_pattern",
            location=", ".join(sorted(secret_hits)[:5]),
            explanation=(
                "Possible secrets in added lines: "
                + ", ".join(sorted(secret_hits))
            ),
            suggestion=(
                "Remove the credential and rotate it; load secrets from the "
                "environment instead."
            ),
        ))

    details = [v.explanation for v in violations]
    ok = not any(v.blocking for v in violations)
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
        new_dependencies=new_dependencies, violations=violations,
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
    # ADDITIVE ONLY: extra paths joined to ENFORCEMENT_MACHINERY_PATHS for
    # the non-overridable halt. A repo protects its own verifier/CI code
    # here; nothing in config can shrink the hardcoded set.
    enforcement_paths_extra: list[str] = field(default_factory=list)
    # License gate: a newly-added uv.lock dependency whose resolved SPDX
    # license matches a deny_partial substring is blocked; one whose every
    # atom is in license_allow passes; anything else is unknown (blocked,
    # add it to license_allow to permit). Empty license_allow disables the
    # gate entirely.
    license_allow: list[str] = field(
        default_factory=lambda: list(DEFAULT_LICENSE_ALLOW)
    )
    license_deny_partial: list[str] = field(
        default_factory=lambda: list(DEFAULT_LICENSE_DENY_PARTIAL)
    )
    # What to do when a license cannot be resolved from any source:
    # "block" (default, fail-closed - an unprovable dependency is not
    # inside the envelope, consistent with the rest of this check) or
    # "advisory" (record it and pass, for operators who accept the risk of
    # offline/cache-miss resolution).
    license_unresolved: str = "block"
    # Whether license resolution may fall back to the PyPI JSON API.
    # A real config field, not a bare env read, so it is covered by
    # envelope_hash: a run that silently skipped the network must not
    # claim the same policy hash as one that consulted it.
    license_use_network: bool = True
    # Reserved for the R8.7 release gate: stored and hashed into the run
    # manifest's policy envelope, not yet enforced (Phase 1 has no deploy
    # step). L3+ may set true.
    deploy: bool = False

    def __post_init__(self) -> None:
        if self.license_unresolved not in ("block", "advisory"):
            raise PolicyConfigError(
                f"invalid license_unresolved {self.license_unresolved!r}; "
                "expected 'block' or 'advisory'"
            )

    @classmethod
    def from_env(cls) -> PolicyConfig:
        """Load from environment only (defaults + env overlay).

        List fields (``paths_deny``, ``secret_patterns``, the license
        lists, ``enforcement_paths_extra``) are toml-only and keep their
        defaults here.
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
            enforcement_paths_extra=list(defaults.enforcement_paths_extra),
            license_allow=list(defaults.license_allow),
            license_deny_partial=list(defaults.license_deny_partial),
            license_unresolved=os.environ.get(
                "KSTRL_POLICY_LICENSE_UNRESOLVED", defaults.license_unresolved,
            ),
            license_use_network=_env_bool(
                "KSTRL_POLICY_LICENSE_NET", defaults.license_use_network
            ),
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
        license_allow = (
            [str(s) for s in section["license_allow"]]
            if isinstance(section.get("license_allow"), list)
            else list(defaults.license_allow)
        )
        license_deny_partial = (
            [str(s) for s in section["license_deny_partial"]]
            if isinstance(section.get("license_deny_partial"), list)
            else list(defaults.license_deny_partial)
        )
        enforcement_paths_extra = (
            [str(s) for s in section["enforcement_paths_extra"]]
            if isinstance(section.get("enforcement_paths_extra"), list)
            else list(defaults.enforcement_paths_extra)
        )
        license_unresolved = (
            str(section["license_unresolved"])
            if "license_unresolved" in section
            else defaults.license_unresolved
        )
        license_use_network = (
            bool(section["license_use_network"])
            if "license_use_network" in section
            else defaults.license_use_network
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
        if "KSTRL_POLICY_LICENSE_UNRESOLVED" in os.environ:
            license_unresolved = os.environ["KSTRL_POLICY_LICENSE_UNRESOLVED"]
        if "KSTRL_POLICY_LICENSE_NET" in os.environ:
            license_use_network = os.environ["KSTRL_POLICY_LICENSE_NET"] == "1"
        if "KSTRL_POLICY_DEPLOY" in os.environ:
            deploy = os.environ["KSTRL_POLICY_DEPLOY"] == "1"

        return cls(
            enabled=enabled,
            paths_deny=paths_deny,
            max_files_changed=max_files_changed,
            max_lines_changed=max_lines_changed,
            deps_allow_new=deps_allow_new,
            secret_patterns=secret_patterns,
            enforcement_paths_extra=enforcement_paths_extra,
            license_allow=license_allow,
            license_deny_partial=license_deny_partial,
            license_unresolved=license_unresolved,
            license_use_network=license_use_network,
            deploy=deploy,
        )

    def envelope_hash(self) -> str:
        """SHA-256 of the resolved envelope for the run manifest.

        Hashes the effective config (post env/toml resolution), so the
        audit record captures what was ENFORCED, not merely what the file
        on disk said. Every knob that can change a verdict is a field on
        this dataclass - including ``license_use_network`` and
        ``license_unresolved`` - so two runs with the same hash enforced
        the same rules (an env-only toggle would otherwise let a weaker
        run claim an unchanged envelope).
        """
        payload = {f.name: getattr(self, f.name) for f in fields(self)}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
