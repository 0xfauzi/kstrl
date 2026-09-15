"""Where the git identity is spelled, and which modules commit (#367).

A repository made by ``git init`` has no ``user.name`` and no
``user.email`` of its own. On a developer machine git fills both in,
from ``~/.gitconfig`` or failing that from the account name and the
hostname, so a test that commits into such a repository passes locally
and fails on a runner, which fills in neither. CI on PR #357 is the only
thing that caught it; the fix there was two ``git config`` lines in one
test, and 42 other pairs across 37 files were each a separate chance to
forget. This file is the mechanism that replaces remembering.

LAYER 1 pins every spelling of the two keys. After #367 there are five,
two of them the helper and three of them prose. A test that configures a
repository by hand instead of calling ``tests.helpers.gitrepo.set_identity``
adds a row here.

LAYER 2 pins every module that runs ``git commit``. It does NOT prove
those commits have an identity, and nothing static can: the repository
is usually built in another module. What it does is make a new commit
site impossible to add silently, so the diff that adds a row is where
somebody says which repository it commits into and that the repository
went through the helper.

WHAT NEITHER LAYER SEES. Both read values ``astwalk.folded_str`` can
decide, so a command the interpreter assembles at run time
(``" ".join(parts)``, an f-string holding a name) folds to ``None`` and
is invisible to both. Both flag rather than clear, so an over-match is a
row somebody reads, which is the direction a guard of this kind is
allowed to be wrong in.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers import astwalk

#: The two keys a commit needs. git refuses for a missing name and for a
#: missing email SEPARATELY, and the second message only appears once the
#: first is fixed, which is how a half-fix reads as a fix.
IDENTITY_KEYS: tuple[str, str] = ("user.name", "user.email")

#: Layer 1's inventory: every spelling of either key in ``tests/``, and
#: how many. Two rows are prose and one is the helper. A new row means a
#: repository is being configured by hand; route it through
#: ``tests.helpers.gitrepo.set_identity`` instead of adding the row.
EXPECTED_IDENTITY_SPELLINGS: dict[str, int] = {
    # The one home. Two literals, the two key names.
    "tests/helpers/gitrepo.py": 2,
    # Prose, not configuration: a Python fixture body asserting on a
    # model's `user.name` / `user.email` attributes, in two string
    # literals.
    "tests/test_harness_integration.py": 2,
    # Prose: one docstring, on the commit `run_scrubbed` makes.
    "tests/test_verify.py": 1,
}

#: Layer 2's inventory: every module in ``tests/`` that names a
#: ``git commit``, and how many spellings. DERIVED BY RUNNING THIS TEST,
#: never by editing it to match: set it to ``{}``, run the file, and copy
#: the ``Found:`` dict out of the failure.
#:
#: Adding a row is not forbidden, it is the point. The diff that adds one
#: is where somebody says which repository that commit runs in and that
#: the repository was through ``tests.helpers.gitrepo.set_identity``.
EXPECTED_GIT_COMMIT_SPELLINGS: dict[str, int] = {
    "tests/conftest.py": 2,
    # Prose, not a commit: the module docstring narrates "commits" and
    # "git" in separate sentences, so the folded string holds both
    # tokens without either running a commit. It sets no repository, so
    # there is nothing for gitrepo.set_identity to be checked against.
    "tests/helpers/gitrepo.py": 1,
    "tests/helpers/run_config.py": 1,
    "tests/spine_utils.py": 1,
    "tests/test_adequacy.py": 8,
    "tests/test_autonomy_ladder.py": 1,
    "tests/test_breaker.py": 2,
    "tests/test_check_result_measurement_behaviour.py": 5,
    "tests/test_cli.py": 2,
    "tests/test_contract_safety.py": 4,
    "tests/test_explicit_merge_gate.py": 1,
    "tests/test_feature_verification.py": 1,
    "tests/test_feature_verification_attribution.py": 1,
    "tests/test_git_identity_helper.py": 5,
    "tests/test_harness_path_scope.py": 3,
    "tests/test_inbox.py": 1,
    "tests/test_init_cmd.py": 3,
    "tests/test_input_hygiene.py": 4,
    "tests/test_instance_safety.py": 2,
    "tests/test_launch_session.py": 1,
    "tests/test_loop.py": 2,
    "tests/test_notify.py": 1,
    "tests/test_policy_envelope.py": 4,
    "tests/test_pr_outcomes.py": 3,
    "tests/test_progress_scope.py": 9,
    "tests/test_prompt_upgrade.py": 1,
    "tests/test_provisioning.py": 2,
    "tests/test_resume_ergonomics.py": 2,
    "tests/test_review_coverage.py": 2,
    "tests/test_review_gates.py": 1,
    "tests/test_review_payload.py": 2,
    "tests/test_run_honesty.py": 1,
    "tests/test_sandbox.py": 1,
    "tests/test_scheduler.py": 1,
    "tests/test_scope_hardening.py": 4,
    "tests/test_scope_launch_gate.py": 1,
    "tests/test_sense_cli.py": 5,
    "tests/test_sense_dampener_baseline.py": 1,
    "tests/test_sense_dampener_workflow.py": 2,
    "tests/test_spine_contract.py": 4,
    "tests/test_spine_crash_recovery.py": 1,
    "tests/test_spine_engineer_loop.py": 4,
    "tests/test_spine_golden_patterns_e2e.py": 1,
    "tests/test_spine_retry_context.py": 5,
    "tests/test_spine_worktree.py": 5,
    "tests/test_state_dir_scope.py": 2,
    "tests/test_timeout_enforcement.py": 3,
    "tests/test_verify.py": 5,
}


def _scannable_sources() -> list[Path]:
    """Every module in ``tests/`` but this one, which names what it pins."""
    return astwalk.test_sources(exclude=Path(__file__))


def _spells_an_identity_key(node: ast.AST) -> bool:
    """Does this expression FOLD to a value naming either key?

    Substring rather than equality, because the key arrives as a bare
    argv word (``"user.email"``), as a ``-c`` assignment
    (``"user.email=t@t"``) and inside prose. It names no node type and no
    field, so a value held in a list, a tuple, a module constant or an
    f-string counts exactly like one written at the call.
    """
    value = astwalk.folded_str(node) or ""
    return any(key in value for key in IDENTITY_KEYS)


def _names_a_commit(node: ast.AST) -> bool:
    """Does this expression fold to a value that RUNS ``git commit``?

    Two shapes, because the suite has both. A lone argv word, where the
    verb arrives as its own literal (``_git(root, "commit", "-m", "x")``),
    matched on the token's BASENAME so a path-spelled tool behaves. And a
    whole command line naming both ``git`` and ``commit``, which is how
    the fake-agent shell stubs spell it.

    ``grep -rn '"commit"' kstrl/`` finds nothing, so a bare ``commit``
    token in ``tests/`` is always git's; there is no ``ks commit`` for
    this to collide with, which is what lets it stay this simple.
    """
    value = astwalk.folded_str(node) or ""
    tokens = [Path(token).name for token in value.split()]
    if tokens == ["commit"]:
        return True
    return "git" in tokens and "commit" in tokens


class TestGitIdentityHasOneHome:
    def test_the_two_keys_are_spelled_only_in_the_helper(self) -> None:
        """Layer 1: the identity is configured in one place."""
        astwalk.assert_census(
            sources=_scannable_sources(),
            sees=_spells_an_identity_key,
            expected=EXPECTED_IDENTITY_SPELLINGS,
            # SPELLED OUT, one per disjunct, and not derived from
            # IDENTITY_KEYS. A control built from the same constant
            # co-varies with it, so shrinking the constant shrinks the
            # control and the guard goes blind with everything green.
            control=(
                'subprocess.run(["git", "config", "user.name", "x"])\n',
                'subprocess.run(["git", "config", "user.email", "x@y"])\n',
            ),
            message=(
                "The set of places spelling a git identity key changed. A repository "
                "that sets one key and forgets the other commits fine on a developer "
                "machine and fails on a runner, which is PR #357. Call "
                "tests.helpers.gitrepo.set_identity(repo) instead of configuring it here."
            ),
        )

    def test_every_file_that_commits_is_declared(self) -> None:
        """Layer 2: no module commits without appearing in this dict."""
        astwalk.assert_census(
            sources=_scannable_sources(),
            sees=_names_a_commit,
            expected=EXPECTED_GIT_COMMIT_SPELLINGS,
            control=(
                'subprocess.run(["git", "-C", str(repo), "commit", "-m", "x"])\n',
                'AGENT_STUB = "git add -A\\ngit commit -q -m x\\n"\n',
            ),
            message=(
                "A git commit appeared in tests/ that this dict does not declare. The "
                "repository it commits into must have been through "
                "tests.helpers.gitrepo.set_identity, or the commit takes the developer's "
                "implicit identity and fails on a runner (#357, #367). Add the row in the "
                "same diff, having checked that."
            ),
        )

    def test_the_net_walks_a_real_suite(self) -> None:
        """Without this, both censuses could be passing over nothing."""
        assert len(_scannable_sources()) > 100


class TestBothLayersCatchAPlantedSite:
    """A guard nobody mutated is a guard nobody tested, per layer."""

    def _probe(self, tmp_path: Path, body: str) -> Path:
        planted = tmp_path / "planted.py"
        planted.write_text(body, encoding="utf-8")
        return planted

    def test_a_raw_commit_in_a_new_file_is_seen(self, tmp_path: Path) -> None:
        planted = self._probe(
            tmp_path,
            'import subprocess\n\nsubprocess.run(["git", "commit", "-qm", "x"])\n',
        )
        assert astwalk.census([planted], _names_a_commit) == {"planted.py": 1}

    def test_a_commit_inside_a_shell_stub_is_seen(self, tmp_path: Path) -> None:
        planted = self._probe(tmp_path, 'STUB = "git add -A\\ngit commit -q -m x\\n"\n')
        assert astwalk.census([planted], _names_a_commit) == {"planted.py": 1}

    def test_a_config_pair_in_a_new_file_is_seen(self, tmp_path: Path) -> None:
        planted = self._probe(
            tmp_path,
            "import subprocess\n\n"
            'subprocess.run(["git", "config", "user.email", "x@y"])\n'
            'subprocess.run(["git", "config", "user.name", "x"])\n',
        )
        assert astwalk.census([planted], _spells_an_identity_key) == {"planted.py": 2}

    def test_prose_about_committing_is_not_a_commit_site(self, tmp_path: Path) -> None:
        """The over-match has a bound: `commits` is not `commit`."""
        planted = self._probe(tmp_path, '"""The engineer commits its work."""\n')
        assert astwalk.census([planted], _names_a_commit) == {}
