"""Where the git identity is spelled, and which modules commit (#367).

What a repository needs and why it lives on the repository rather than
in the environment is ``tests/helpers/gitrepo.py``'s docstring; this
file is the mechanism that keeps that the only place it is spelled.

LAYER 1 pins every spelling of the two keys. After #367 there are five,
two of them the helper and three of them prose. A test that configures a
repository by hand instead of calling ``tests.helpers.gitrepo.set_identity``
adds a row here.

LAYER 2 pins every module that runs ``git commit``. It does NOT prove
those commits have an identity: the repository is usually built in
another module. What it does is make a new commit site impossible to add
silently, so the diff that adds a row is where somebody says which
repository it commits into and that the repository went through the
helper. Its limit: it pins per-file COUNTS, so any new or changed
commit spelling, in a new file or an already-declared one, moves a count
and forces a declaration (measured: one added init-and-commit function
in a declared file turns it red); what it cannot do is PROVE that the
repository a declared commit runs in carries an identity. A test that
hand-inits a repository and hands it to production code that commits
never appears here at all, because ``kstrl/`` is outside this corpus. A
creation-side census, over ``git init`` / ``git clone`` instead of
``git commit``, is the next step and not this one.

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
    # #482: materialize commits the fixture's base and feature trees into a
    # repository it has just put through tests.helpers.gitrepo.set_identity.
    "tests/helpers/calibration_integration_fixture.py": 2,
    # Prose, not a commit: the module docstring narrates "commits" and
    # "git" in separate sentences, so the folded string holds both
    # tokens without either running a commit. It sets no repository, so
    # there is nothing for gitrepo.set_identity to be checked against.
    "tests/helpers/gitrepo.py": 1,
    # #482: merged_feature and commit_file, in a repo set_identity configured.
    "tests/helpers/integration_harness.py": 2,
    "tests/helpers/run_config.py": 1,
    "tests/spine_utils.py": 1,
    "tests/test_adequacy.py": 8,
    "tests/test_agent_processes_outlive_run.py": 1,
    "tests/test_autonomy_ladder.py": 1,
    # #414/#425: three `git commit` spellings - `_commit_rename`, the
    # rename-then-break test, and `_advance_main` - all into a repository
    # `tests.conftest.make_review_repo` already put through
    # `tests.helpers.gitrepo.set_identity`.
    "tests/test_bad_patterns_diff_scope.py": 3,
    "tests/test_breaker.py": 3,
    "tests/test_build_manifest_preflight.py": 1,
    # #399 added two real-repository tests (a stubbed-git refusal, which
    # makes no commit, and a latin-1-bytes commit through `_git`/`_repo`,
    # both already declared here, plus a second real commit in the
    # latter). Both repositories go through
    # `tests.helpers.gitrepo.set_identity` via this file's own `_repo`.
    # #423's fixer pass added a third: a rename-source misparse regression
    # test that commits its own repo through `_git`/`gitrepo.set_identity`
    # directly (not through `_init_repo`, so it is a second call site in
    # this file, each committing once).
    "tests/test_check_result_measurement_behaviour.py": 7,
    # tests/test_child_output_encoding.py (#409) has no row here: it builds its
    # repository through tests.conftest.make_review_repo, already declared and
    # counted at conftest.py's row above, the same shape
    # tests/test_patch_coverage.py's row below argues for.
    "tests/test_cli.py": 2,
    "tests/test_contract_safety.py": 4,
    # `ready_repo` calls `tests.helpers.gitrepo.set_identity` before it
    # commits (#198), once per scenario that builds its own fixture.
    "tests/test_doctor.py": 7,
    # #399: the rewritten locale-pinned bad_patterns test commits into a
    # real repository through `tests.helpers.gitrepo.set_identity`.
    "tests/test_encoding_sites.py": 2,
    "tests/test_explicit_merge_gate.py": 1,
    # #481: two commits, the base commit _git_project makes and the one
    # _advance makes, both into a repository _git_project put through
    # tests.helpers.gitrepo.set_identity. The third count is prose: the
    # module docstring names "git" and "commit" in one string.
    "tests/test_feature_base.py": 3,
    "tests/test_feature_verification.py": 1,
    "tests/test_feature_verification_attribution.py": 1,
    "tests/test_git_identity_helper.py": 5,
    # #423's `_repo_with_tricky_names` fixture, through
    # `tests.helpers.gitrepo.set_identity`, which this row declares.
    "tests/test_git_path_spelling.py": 6,
    "tests/test_harness_path_scope.py": 3,
    "tests/test_inbox.py": 1,
    "tests/test_inbox_resolves_on_completion.py": 1,
    "tests/test_init_cmd.py": 3,
    "tests/test_input_hygiene.py": 4,
    "tests/test_instance_safety.py": 2,
    # #459: five `git commit` spellings, every one into a repository
    # this file's `isolated_repo` put through
    # `tests.helpers.gitrepo.set_identity`.
    "tests/test_language_ignores.py": 5,
    "tests/test_launch_session.py": 1,
    # #544: three `git commit` sites (`_seed`, `_commit_all` and the
    # deletion test), every one into a repository `_seed` put through
    # `tests.helpers.gitrepo.set_identity`.
    "tests/test_lockfile_scope.py": 3,
    "tests/test_loop.py": 2,
    "tests/test_notify.py": 1,
    # #152 simplify pass: the five real commits this file used to make
    # directly (via gitrepo.git_in) moved into
    # tests.conftest.make_review_repo, already declared and counted at
    # its own call site (tests/conftest.py's row above). What is left
    # here is the module docstring's own prose - "real temp git
    # repository" and "feature commit" land "git" and "commit" as
    # separate exact tokens in the same folded string, the identical
    # shape gitrepo.py's own row above is declared for.
    "tests/test_patch_coverage.py": 1,
    # #399 addendum A1: the real end-to-end unquote-round-trip test adds two
    # commits (a base commit, then the four tricky filenames) through
    # tests.helpers.gitrepo, which this file already imports and whose
    # set_identity it calls first. #399 blocker 1 adds two more of the same
    # shape, for the quote-and-accent-on-one-path regression test.
    "tests/test_policy_envelope.py": 8,
    "tests/test_pr_outcomes.py": 3,
    # #531: one commit per fixture repo, after set_identity.
    "tests/test_prelaunch_refusal_exit.py": 1,
    "tests/test_progress_scope.py": 9,
    "tests/test_prompt_upgrade.py": 1,
    "tests/test_provisioning.py": 2,
    "tests/test_resume_ergonomics.py": 2,
    # #465: the parked-merge and resume tests commit through set_identity.
    # The single_pr shared-branch regression test (blocker 1 of the PR
    # #471 fixer round) adds two more of the same shape: the shared
    # repo's own base commit and comp-b's agent-script commit onto the
    # shared branch, both into repositories built through
    # `tests.helpers.gitrepo.set_identity`.
    "tests/test_merge_gate_park.py": 2,
    "tests/test_resume_reclaims_own_branch.py": 5,
    "tests/test_retry_carries_flags.py": 1,
    # #498: one base commit, into the repository `_repo` builds through
    # `tests.helpers.gitrepo.git_in` / `set_identity`.
    "tests/test_review_agent_fallback.py": 2,
    "tests/test_review_coverage.py": 2,
    "tests/test_review_gates.py": 1,
    "tests/test_review_payload.py": 2,
    "tests/test_run_honesty.py": 1,
    "tests/test_run_record_version.py": 1,
    # Prose, not a commit: a tuple of literal argv-prefix strings an
    # allowlist test checks a Claude reviewer's permission RULES against
    # ("git commit" among them), never spawned.
    "tests/test_sandbox.py": 1,
    "tests/test_scheduler.py": 1,
    "tests/test_scope_hardening.py": 4,
    "tests/test_scope_launch_gate.py": 1,
    "tests/test_check_cli.py": 5,
    "tests/test_check_baseline_document.py": 1,
    # #400: two diff-driven-check tests, each committing one file onto a
    # branch. The repository comes from test_check_cli._make_repo, which
    # calls tests.helpers.gitrepo.set_identity.
    "tests/test_check_baseline_cli.py": 2,
    "tests/test_spine_contract.py": 4,
    "tests/test_spine_crash_recovery.py": 2,
    # #543: the bash engineer commits in worktrees of a repo
    # spine_utils.init_kstrl_repo put through set_identity, and the merging
    # gh stub commits in a clone that includes that repo's config.
    "tests/test_spine_dependency_base.py": 2,
    "tests/test_spine_engineer_loop.py": 4,
    "tests/test_spine_golden_patterns_e2e.py": 1,
    # #154: TestSpineReleaseRef's `_enable_release` commits the inert
    # `[release]` kstrl.toml onto the repo `init_kstrl_repo` already put
    # through `tests.helpers.gitrepo.set_identity`.
    "tests/test_spine_pr_failures.py": 1,
    "tests/test_spine_retry_context.py": 5,
    "tests/test_spine_worktree.py": 5,
    # #435: seven commits, all through tests.helpers.gitrepo.set_identity,
    # into the fixture's own repo/wt1/merger repositories (the builder)
    # and the fx.worktree / a plain local repo the individual tests
    # commit into directly. Ten since the #435 fix-round's case-B
    # fixture (A0): three more, into the fixture's own repo/sibling-clone
    # repositories and the fx.worktree it builds, all through
    # set_identity the same way.
    "tests/test_stale_base_ref.py": 10,
    "tests/test_state_dir_scope.py": 2,
    "tests/test_timeout_enforcement.py": 3,
    # Prose, not commits: assertion strings checking what
    # `run_scrubbed`'s rendered command STARTS WITH or what a mocked
    # call log CONTAINS, plus a docstring paragraph. Nothing here spawns
    # git; the one real commit `run_scrubbed` makes belongs to
    # `kstrl/verify.py`, which is production code and out of this census.
    # #399 added TestCheckBadPatterns and the bytecode-destination test as
    # real-repository tests, briefly raising this row to 11 through this
    # file's own `_repo`/`_commit`. #399's simplify pass on #405 (C1) moved
    # both classes onto `tests.conftest.make_review_repo`, already counted
    # at its own call site (the `tests/conftest.py` row above), and dropped
    # `_repo`/`_commit` from this file entirely - back to 5, this file's
    # value before #399 touched it.
    "tests/test_verify.py": 5,
    # #416: two `git commit` argv spellings, both into a repository built by
    # this file's own `_repo`, which calls `tests.helpers.gitrepo.set_identity`
    # right after `git init` - the base commit and the undecodable-CONTENT
    # commit. The undecodable-PATH fixture's commit is made by way of `git
    # commit-tree` / `git update-ref` (built through the index rather than
    # the working tree, since APFS cannot hold that filename) and this
    # census's own predicate does not see it: it tokenises to `commit-tree`,
    # not the bare `commit` this file requires (#416's simplify review; an
    # earlier version of this comment credited that call as the third row,
    # which was a docstring mention of "this commit" that has since been
    # reworded away).
    # #414 test 13 adds two more `git commit` argv spellings, into the
    # repository this file's own `_repo` builds, which calls
    # `tests.helpers.gitrepo.set_identity` right after `git init` - the
    # latin-1 source file on main, and the rename onto `work`.
    "tests/test_undecodable_diff.py": 4,
    # #233: one base commit for the two-attempt factory-run fixture, into a
    # repository this file's own `two_attempt_run` builds through
    # `tests.helpers.gitrepo.git_in` / `set_identity`.
    "tests/test_attempt_iteration_readings.py": 1,
    # #447: one base commit per factory-run project, into a repository
    # `_git_project` builds through `tests.helpers.gitrepo.git_in` /
    # `set_identity`.
    "tests/test_retry_journal_rows.py": 1,
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
    if "user." not in value:
        return False
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
    if "commit" not in value:
        return False
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
    """A guard nobody mutated is a guard nobody tested, per layer.

    The three positive plants that used to live here are redundant with
    ``assert_census``'s own control assertion above: both layers'
    ``control=`` strings ARE a raw commit, a shell-stub commit and a
    config pair, and ``assert_census`` already fails loudly if any of
    them stops firing. What that control cannot show is the other
    direction, that the net has a bound rather than matching everything
    that mentions committing, which is the one test kept here.
    """

    def _probe(self, tmp_path: Path, body: str) -> Path:
        planted = tmp_path / "planted.py"
        planted.write_text(body, encoding="utf-8")
        return planted

    def test_prose_about_committing_is_not_a_commit_site(self, tmp_path: Path) -> None:
        """The over-match has a bound: `commits` is not `commit`."""
        planted = self._probe(tmp_path, '"""The engineer commits its work."""\n')
        assert astwalk.census([planted], _names_a_commit) == {}
