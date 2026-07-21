"""Pytest fixtures for kstrl tests.

Suite isolation (R4.1): before this conftest grew the fixtures below, the
suite appended hundreds of junk entries to the repository's real
``.kstrl/evolution.jsonl`` / ``.kstrl/experiments.tsv`` (837 of 910 journal
entries at review time were test pollution), corrupting the data the
learning loop consumes. Two layers fix that:

1. ``isolate_kstrl_state`` (autouse, function-scoped) redirects every
   relative ``.kstrl/...`` default write path into the test's ``tmp_path``.
2. ``guard_repo_kstrl_state`` (autouse, session-scoped) is the enforcement:
   it fingerprints the repo's real ``.kstrl/`` before the session and fails
   the run loudly if any test mutated it.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Generator
from pathlib import Path

import pytest

# Repository root that contains this test suite, independent of CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Environment-variable families consumed by the from_env/load paths of the
# phase configs (FactoryConfig, TimeoutConfig, ContractConfig,
# SecurityConfig, VerifyConfig, EvolutionConfig, KnowledgeConfig). Cleared
# by prefix so ambient dev-machine env cannot alter from_env/load tests,
# and so newly added vars in a family are covered without editing this list.
# The bare FACTORY_ family predates the KSTRL_ namespace and is still read
# directly by factory config, so it is scrubbed alongside the rest.
KSTRL_ENV_PREFIXES: tuple[str, ...] = (
    "FACTORY_",
    "KSTRL_FACTORY_",
    "KSTRL_TIMEOUT_",
    "KSTRL_CONTRACT_",
    "KSTRL_SECURITY_",
    "KSTRL_VERIFY_",
    "KSTRL_EVOLUTION_",
    "KSTRL_KNOWLEDGE_",
    "KSTRL_FEEDFORWARD_",
    "KSTRL_MUTATION_",
    "KSTRL_DEAD_CODE_",
    "KSTRL_LINEAR_",
    "KSTRL_NOTIFY_",
)

# Legacy single-loop env vars (exact names, no shared prefix).
_LEGACY_ENV_VARS: tuple[str, ...] = (
    "MAX_ITERATIONS",
    "AGENT_CMD",
    "MODEL",
    "MODEL_REASONING_EFFORT",
    "SLEEP_SECONDS",
    "INTERACTIVE",
    "PROMPT_FILE",
    "ALLOWED_PATHS",
    "KSTRL_BRANCH",
    "KSTRL_BRANCH",
    "PRD_FILE",
    "KSTRL_UI",
    "KSTRL_UI",
    "GUM_FORCE",
    "NO_COLOR",
    "KSTRL_ASCII",
    "KSTRL_ASCII",
    "KSTRL_AGENT_TYPE",
    "KSTRL_AGENT_TYPE",
    "KSTRL_AUTO_CHECKOUT",
    "KSTRL_AUTO_CHECKOUT",
    "KSTRL_AGENT_BUDGET_USD",
    "KSTRL_AGENT_BUDGET_USD",
)


def _clear_kstrl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every kstrl-related env var (legacy names + config families)."""
    for var in _LEGACY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in list(os.environ):
        if var.startswith(KSTRL_ENV_PREFIXES):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def isolate_kstrl_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect every evolution/experiments/knowledge write path to tmp_path.

    Why chdir is the mechanism: the bare ``EvolutionConfig()``
    constructor defaults ``journal_path``/``experiments_path`` to
    relative ``.kstrl/...`` paths resolved against CWD at write time
    (since R2.1 ``run_factory`` uses ``EvolutionConfig.load(root_dir)``,
    which anchors them to the factory root, but direct constructions in
    tests and legacy call sites remain CWD-relative).
    ``KnowledgeConfig`` likewise defaults ``knowledge_root`` to a
    relative ``.kstrl/knowledge`` and ``KnowledgeConfig.load(None)``
    resolves it against ``Path.cwd()``; there is no env override for the
    root. Pointing CWD at ``tmp_path`` therefore redirects every relative
    default in one move (journal, experiments, knowledge root, snapshot
    dirs, proposals) without touching kstrl source.

    Ambient env is cleared too, so a dev machine exporting FACTORY_* /
    KSTRL_* values cannot alter from_env/load tests.

    This redirect is convenience; ``guard_repo_kstrl_state`` is the
    enforcement.
    """
    monkeypatch.chdir(tmp_path)
    _clear_kstrl_env(monkeypatch)
    return tmp_path


def snapshot_kstrl_dir(kstrl_dir: Path) -> dict[str, str]:
    """Fingerprint every entry under ``kstrl_dir``.

    Maps the path relative to ``kstrl_dir`` to a sha256 hex digest for
    files, ``"dir"`` for directories, and ``"symlink:<target>"`` for
    symlinks. Returns an empty mapping when the directory does not exist,
    so absent-before/absent-after compares equal.
    """
    snapshot: dict[str, str] = {}
    if not kstrl_dir.exists():
        return snapshot
    for path in sorted(kstrl_dir.rglob("*")):
        rel = str(path.relative_to(kstrl_dir))
        if path.is_symlink():
            snapshot[rel] = f"symlink:{os.readlink(path)}"
        elif path.is_dir():
            snapshot[rel] = "dir"
        elif path.is_file():
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def describe_snapshot_diff(
    before: dict[str, str], after: dict[str, str]
) -> str:
    """Human-readable created/deleted/modified summary of two snapshots."""
    lines: list[str] = []
    for rel in sorted(after.keys() - before.keys()):
        lines.append(f"  created:  {rel}")
    for rel in sorted(before.keys() - after.keys()):
        lines.append(f"  deleted:  {rel}")
    for rel in sorted(before.keys() & after.keys()):
        if before[rel] != after[rel]:
            lines.append(f"  modified: {rel}")
    return "\n".join(lines)


def _guard_root() -> Path:
    """Root whose .kstrl/ the session guard protects.

    ``KSTRL_SUITE_GUARD_ROOT`` exists so the guard's failure path can be
    exercised end-to-end by a nested pytest run against a synthetic repo
    (tests/test_suite_isolation.py); it is not a knob for disabling the
    guard.
    """
    override = os.environ.get("KSTRL_SUITE_GUARD_ROOT")
    return Path(override) if override else REPO_ROOT


@pytest.fixture(scope="session", autouse=True)
def guard_repo_kstrl_state() -> Generator[None, None, None]:
    """FAIL the run loudly if any test mutated the repo's real .kstrl/.

    This is the enforcement behind the per-test redirect: the redirect
    covers the known relative-default write paths, but any test that
    reaches the real ``.kstrl/`` through an absolute path (or a future
    write path the redirect does not know about) is caught here and fails
    the whole run, so pollution of the learning loop's data can never land
    silently again.
    """
    kstrl_dir = _guard_root() / ".kstrl"
    before = snapshot_kstrl_dir(kstrl_dir)
    yield
    after = snapshot_kstrl_dir(kstrl_dir)
    if before != after:
        pytest.fail(
            "Test suite mutated the repository's real .kstrl/ directory "
            f"({kstrl_dir}).\n"
            "Tests must write only under tmp_path; the autouse "
            "isolate_kstrl_state fixture redirects the default relative "
            ".kstrl/ paths there, so a mutation here means a test used an "
            "absolute path to the repo. Changes detected:\n"
            + describe_snapshot_diff(before, after),
            pytrace=False,
        )


@pytest.fixture
def temp_project(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary project directory with kstrl structure."""
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)

    # Create minimal prompt.md
    (kstrl_dir / "prompt.md").write_text("Test prompt\n")

    # Create minimal prd.json
    (kstrl_dir / "prd.json").write_text(
        '{"branchName": "test-branch", "userStories": []}\n'
    )

    # Save current directory
    original_dir = os.getcwd()
    os.chdir(tmp_path)

    yield tmp_path

    # Restore directory
    os.chdir(original_dir)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear kstrl-related environment variables.

    Covers the legacy single-loop names plus the FACTORY_*/KSTRL_* config
    families. The autouse ``isolate_kstrl_state`` fixture already clears
    these for every test; this fixture remains for tests that want to
    state the dependency explicitly.
    """
    _clear_kstrl_env(monkeypatch)
