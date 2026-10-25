"""#320: every text read in ``kstrl/`` names utf-8 and answers for the decode.

THE RULE, which CLAUDE.md has stated since #291 and which nothing checked
until #320: a reader of any file kstrl writes must name
``encoding="utf-8"``, AND must catch ``ValueError`` alongside ``OSError``,
because ``UnicodeDecodeError`` is a ``ValueError`` and walks straight past
a fail-closed ``except OSError``. ``scaffold_ledger._read_text_or_none`` is the
worked example. #319 fixed the seam every CLI command sits behind and said
so; this is the rest.

WHAT THE SWEEP FOUND, re-derived rather than trusted. #320 estimated
"roughly 15" from an audit whose line numbers had already gone stale. The
measured census over 52 read-mode text decodes in ``kstrl/``:

    15  guarded by an OSError-ish handler with nothing covering the
        decode - the escape, and the issue's estimate was exact
     6  no encoding named, so the read is whatever the locale says
     1  of those 6 is also one of the 15 (``fixtures.py``)
    --
    20  offender sites, now zero
     6  compliant by construction: ``errors=`` is not "strict", so the
        decode cannot raise at all (measured, not assumed)
    12  no ``try`` anywhere around them, so nothing is claimed and
        nothing escapes: not offenders under this rule

A 21st site is not in that table because no census of READ CALLS can see
it. ``factory_lock.py``'s run lock reads the holder pid back out of an ``"a+"``
handle - ``fp.read(64)`` under ``except OSError`` - and the decode is at
the read, not at the ``open``. ``encodingwalk._through_handles`` is the
half of the walk that follows the handle, and that site is the only one
of its shape in the package.

THE SECOND POPULATION, once a prose disclosure and now an executed census.
A CHILD PROCESS's output is the same defect class as a file kstrl reads,
and is not the same population: ``subprocess.run(..., text=True)`` with no
``encoding=`` decodes with ``locale.getencoding()`` and no error handler,
so it is strict. This module used to record that population as a number in
a paragraph: "62 such calls" across a module count and a ``git.py`` count
that this paragraph does not repeat, because a module count and a per-file
count are exactly the kind of live numbers that rot silently. #409 found
that both had drifted from what running the walk actually finds, with
nothing failing while they did - the exact rot this module's own closing
paragraph warned about. The paragraph is gone. The population is now
``EXPECTED_TEXT_MODE_SPAWNS`` below, and ``TestEverySpawnDecodeNamesUtf8``
is what asserts it is what the walk finds; no number is recorded here that
the tests below do not also check. Unlike the read rule, the spawn rule has
one half, not two: a child's stdout is produced by git, gh, uv or a model,
so a decode fault there is a genuine caller-facing failure rather than a
file kstrl must be able to read back, and there is no handler obligation to
check for.

The walk that produces these inventories is
``tests/helpers/encodingwalk.py``; what it cannot see is named beside
the control that covers it in ``tests/test_encoding_walk.py``, rather
than listed here where a disclosure can rot without anything failing.
"""

from __future__ import annotations

from tests.helpers.astwalk import (
    Sites,
    assert_census,
    assert_sites,
    package_sources,
)
from tests.helpers.encodingwalk import package_scan, reported_sites, spells_a_token

# --------------------------------------------------------------------------
# LAYER 1: the census that enumerates no node type and no field name.
# --------------------------------------------------------------------------

#: Every expression in ``kstrl/`` that spells ``read_text`` or ``open``,
#: per module. A reader in ANY shape built on those two tokens has to
#: change this dict first, whatever it does with the result - which is
#: what layer 2 cannot promise, and the reason this exists.
#:
#: Not "no module can obtain a file's text without naming one of them":
#: ``configparser``, ``linecache`` and ``fileinput`` each decode with the
#: locale and name neither. None is live in ``kstrl/``, and the claim
#: this pin makes is about the two tokens rather than about every decode.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds one
#: is where somebody says why new code opens a file and how it answers for
#: the encoding and the decode. The count is deliberately generous - it
#: includes binary opens, write opens and the ``fcntl`` lock files -
#: because a net that decides what to leave out is a net that can be wrong
#: about what it left out.
EXPECTED_READ_SPELLINGS: dict[str, int] = {
    # #700 slice 4: `pr_section` reads the latest acceptance record back
    # (moved to acceptance_lines.py in slice 6).
    "acceptance/acceptance_lines.py": 1,
    # #700 slice 10a: `read_dispute` reads the engineer's progress log.
    "acceptance/acceptance_record.py": 1,
    "agents/codex.py": 1,
    # The verification designer's entries kept across a refusal.
    "factory/acceptance_design.py": 2,
    "agents/logging.py": 1,
    # The "a+b" append open, moved here from evolution.py by #331.
    "io/appendio.py": 1,
    "io/atomicio.py": 1,
    "learning/autonomy.py": 1,
    "learning/autonomy_replay.py": 1,
    # 1 since #406's ``_read_document`` dedup, which the baseline reader
    # module (split out of calibration.py) now shares between callers.
    "calibration/calibration_baseline.py": 1,
    "cli/cli_preflight.py": 1,
    "factory/commandrun.py": 1,
    "architect/decisions.py": 1,
    "architect/decompose_prompt.py": 2,
    "events/events.py": 1,
    "learning/evolution.py": 1,
    "factory/factory_acceptance.py": 1,
    "proc/factory_lock.py": 1,
    "pipeline/factory_result.py": 1,
    "factory/factory_worker.py": 2,
    "factory/factory_worktrees.py": 1,
    "cli/feature_cmd.py": 2,
    # 1 since #696 slice 6: the module map's line count is the one read
    # left; the interface, graph and conventions readers are gone.
    "project/feedforward.py": 1,
    "owner/inbox.py": 1,
    # 3 until #352 routed ``_ensure_gitignore``'s append through
    # ``appendio``, which encodes once for every appender. The two left
    # are both reads: the PRD file and ``_read_text_or_none``. N2 (#776)
    # moved ``_read_text_or_none`` to ``scaffold_ledger.py``.
    "project/init_cmd.py": 1,
    # Back to 1 (the ledger's own read) since #231's simplify pass (B1):
    # the memory-file read `_append_guidance` added moved to
    # `operator_context.append_guidance_record`, which is the module
    # that already owns the reader.
    "serve/intake_issue.py": 1,
    # #482: the finding status word "open", not a read.
    "review/integration.py": 1,
    # #700, the security review of #770: the root's `.git` file and the
    # back link `worktrees/<name>/gitdir`, read to prove that the git
    # directory both zones read is the root's own.
    "acceptance/isolation.py": 2,
    "loop/iteration_prompt.py": 1,
    "learning/journal_reads.py": 1,
    "knowledge/knowledge_distill.py": 1,
    "knowledge/knowledge_facts.py": 1,
    "loop/loop.py": 1,
    "plan/manifest.py": 1,
    "events/observability.py": 1,
    # Back to 1 (the existing `read_operator_file` read): the
    # `append_guidance_record` read #231 B1 added here moved again, to
    # `operator_guidance.py`, when this module crossed the 800-line
    # ratchet and that write path split out (the addendum's simplify
    # pass).
    "project/operator_context.py": 1,
    # The `append_guidance_record` read, moved from `operator_context.py`
    # in the same split.
    "project/operator_guidance.py": 1,
    # 3 until #463 added carry_interrupted_run's journal open; 5 since #482
    # added journal_integration_result's journal open. #776 divided the 5
    # between the layer modules: the attempt record has 3 (the phase
    # transcript open and two journal opens) and the ledger has 1.
    "pipeline/pipeline_attempt.py": 3,
    "pipeline/pipeline_knowledge.py": 1,
    "pipeline/pipeline_ledger.py": 1,
    "plan/prd.py": 1,
    # #700 slice 5: `ks recheck` reads a record and its index.json back.
    "acceptance/recheck.py": 1,
    # #700 slice 3: layer 1 counts every `open(...)`/`read_text(...)` call
    # shape, mode or not: `replay_lock`'s `path.open('a+', encoding='utf-8')`
    # and the `up` log's `log.open('wb')` (bytes mode, so it is not itself
    # a text read; layer 2 below excludes it for exactly that reason).
    "acceptance/replay.py": 2,
    # #803: the login of the claude user settings.
    "agents/sandbox.py": 1,
    "project/scaffold_ledger.py": 1,
    "review/security.py": 1,
    # Five reads, and five reads only. Round 1 of #228 spelled the gh
    # flag as two argv tokens, so the bare literal "open" landed here and
    # the row meant "four reads plus one argv word". That is the
    # skip-direction failure: a later commit adding one genuine read and
    # dropping the flag would have left the count at 5 and the census
    # green. The flag is now ``--state=open``, one token, and the fifth
    # read is the open-PR count streak (#228 round 2). #231's simplify
    # pass (2026-09-16) removed one raw ``read_text`` call
    # (``_run_id_from_manifest``'s) and added another
    # (``_read_manifest_json``'s, the shared reader both post-run
    # functions now go through), so this LAYER 1 count - raw
    # occurrences, not deduplicated - is unmoved at 5 even though the
    # deduplicated spelling list below loses a row. #776 S3 moved
    # _read_manifest_json, with one of the five reads, to serve_outcome.py.
    # #776 S4 moved SpendLedger.load, with one read, to serve_spend.py.
    # #776 S5 moved factory_lock_held (the lock open) to serve_gates.py and
    # the streak read to serve_github.py.
    "serve/serve.py": 1,
    "serve/serve_gates.py": 1,
    "serve/serve_github.py": 1,
    "serve/serve_outcome.py": 1,
    "serve/serve_spend.py": 1,
    "io/statedir.py": 1,
    # 4 since #414: the bad-patterns scan's read_text is gone (it reads
    # bytes now, so py_compile does its own PEP 263 decoding). 3 since
    # #696: scrub_stale_verify_commands's CLAUDE.md read is gone with the
    # whole mechanism it served (reconciling CLAUDE.md's verification
    # bullets against [verify], which the flag day retired). 1 since #696
    # slice 8 deleted check_test_adequacy's read of a changed test's
    # source and check_patch_coverage's report: the self-critique
    # progress log is the one left.
    "verify/verify.py": 1,
    "queue/workqueue.py": 1,
    "queue/workqueue_store.py": 5,
}


class TestNoModuleReadsAFileWithoutAppearingHere:
    """Layer 1. A census, so a shape nobody thought of still moves it."""

    def test_the_spellings_are_the_ones_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=spells_a_token,
            expected=EXPECTED_READ_SPELLINGS,
            # One control per disjunct. A single one would be a scalar
            # proof over ``read_text or open`` and would stay green with
            # either half deleted, which #324 round 2 measured happening.
            control=("p.read_text()", "open(p)"),
            message="a module's file-read spellings moved.",
        )


# --------------------------------------------------------------------------
# LAYER 2: the walk, over the package.
# --------------------------------------------------------------------------

#: Every read layer 2 CLEARS, keyed by module and expression rather than
#: by line, so an edit above a read does not fail the pin while adding a
#: read still does. Deduplicated through a set, so this cannot count; the
#: counting job is layer 1's.
EXPECTED_CLEARED_READS: tuple[str, ...] = (
    # #700 slice 4: the acceptance record a PR body repeats, utf-8, with
    # ValueError caught beside OSError (a UnicodeDecodeError is a ValueError).
    # Moved to acceptance_lines.py in slice 6.
    "acceptance/acceptance_lines.py path.read_text(encoding='utf-8')",
    # #700 slice 10a: the engineer's progress log, where it disputes a
    # check, utf-8, with ValueError caught beside OSError.
    "acceptance/acceptance_record.py progress.read_text(encoding='utf-8')",
    # #700, the security review of #770: the `.git` file of the root and the
    # back link it names, utf-8, with ValueError caught beside OSError.
    "acceptance/isolation.py (entry / 'gitdir').read_text(encoding='utf-8')",
    "acceptance/isolation.py dot_git.read_text(encoding='utf-8')",
    # #700 slice 5: an acceptance record and its index.json, utf-8, with
    # ValueError caught beside OSError.
    "acceptance/recheck.py path.read_text(encoding='utf-8')",
    # #700 slice 3: the per-machine replay lock.
    "acceptance/replay.py path.open('a+', encoding='utf-8')",
    "agents/codex.py last_msg_file.read_text(encoding='utf-8')",
    "agents/logging.py self._log_path.open('a', encoding='utf-8')",
    # #803: the claude user settings, utf-8, with ValueError caught beside OSError.
    "agents/sandbox.py path.read_text(encoding='utf-8')",
    "architect/decisions.py path.read_text(encoding='utf-8')",
    "architect/decompose_prompt.py (spec_path / name).read_text(encoding='utf-8')",
    "architect/decompose_prompt.py spec_path.read_text(encoding='utf-8')",
    "calibration/calibration_baseline.py path.read_text(encoding='utf-8')",
    "cli/cli_preflight.py open(prd_file, encoding='utf-8')",
    "cli/feature_cmd.py open(latest_path, 'w', encoding='utf-8')",
    "cli/feature_cmd.py open(repair_path, 'w', encoding='utf-8')",
    "events/events.py open(path, encoding='utf-8', errors='replace')",
    "events/observability.py for line in f on an open() handle",
    "events/observability.py open(path, encoding='utf-8')",
    "factory/acceptance_design.py open(path, 'a', buffering=1, encoding='utf-8')",
    "factory/acceptance_design.py saved.read_text(encoding='utf-8')",
    "factory/commandrun.py open(path, 'a', buffering=1, encoding='utf-8')",
    "factory/factory_worker.py open(log_path, 'a', buffering=1, encoding='utf-8', errors='replace')",  # noqa: E501
    "factory/factory_worker.py open(run_paths.engineer_log(component_id), 'a', buffering=1, encoding=",  # noqa: E501
    "factory/factory_worktrees.py open(lock_path, 'w', encoding='utf-8')",
    "io/statedir.py open(lock_path, 'a+', encoding='utf-8')",
    "knowledge/knowledge_distill.py prd_path.read_text(encoding='utf-8')",
    "knowledge/knowledge_facts.py path.read_text(encoding='utf-8')",
    "learning/autonomy.py path.read_text(encoding='utf-8')",
    # Two rows became one in #331. ``load_runs`` held a utf-8 handle open
    # and ran ``csv.DictReader`` over it, which the walk had to clear as
    # two separate reads; it reads the text once now and hands it to
    # ``evolution.experiment_rows``, the parser its sibling reader
    # ``get_experiment_trends`` already used. The disposition is stronger,
    # not merely different: the old pair named utf-8 under no handler at
    # all, and this names utf-8 AND catches ``ValueError`` beside
    # ``OSError``, which is the half a ``UnicodeDecodeError`` escapes.
    "learning/autonomy_replay.py path.read_text(encoding='utf-8')",
    "learning/journal_reads.py self.config.experiments_path.read_text(encoding='utf-8')",
    "loop/iteration_prompt.py claude_md_path.read_text(encoding='utf-8')",
    "loop/loop.py config.prompt_file.read_text(encoding='utf-8')",
    "pipeline/factory_result.py path.read_text(encoding='utf-8')",
    "pipeline/pipeline_attempt.py open(path, 'a', buffering=1, encoding='utf-8')",
    "pipeline/pipeline_knowledge.py progress_path.read_text(encoding='utf-8')",
    "plan/manifest.py open(path, encoding='utf-8')",
    "plan/prd.py open(path, encoding='utf-8')",
    "proc/factory_lock.py fp.read(64) on an open() handle",
    "proc/factory_lock.py open(lock_path, 'a+', encoding='utf-8')",
    "project/feedforward.py path.read_text(encoding='utf-8', errors='replace')",
    "project/init_cmd.py open(prd_file, encoding='utf-8')",
    "project/operator_context.py spec.path.read_text(encoding='utf-8')",
    # `append_guidance_record`'s read, same spelling as the row above
    # because both read an `OperatorFile.path`, moved to
    # `operator_guidance.py` when that write path split out of
    # `operator_context.py` under the 800-line ratchet.
    "project/operator_guidance.py spec.path.read_text(encoding='utf-8')",
    "project/scaffold_ledger.py path.read_text(encoding='utf-8')",
    "queue/workqueue.py spec_source.read_text(encoding='utf-8')",
    "queue/workqueue_store.py meta_path.read_text(encoding='utf-8')",
    "queue/workqueue_store.py open(lock_path, 'a+', encoding='utf-8')",
    "queue/workqueue_store.py self.journal_path.read_text(encoding='utf-8')",
    "queue/workqueue_store.py self.pause_path.read_text(encoding='utf-8')",
    "queue/workqueue_store.py self.spec_path(item).read_text(encoding='utf-8')",
    "review/security.py prd_path.read_text(encoding='utf-8')",
    # ``init_cmd.py path.open('a', encoding='utf-8')`` was here until
    # #352. The read is DELETED, not unseen: ``_ensure_gitignore``'s
    # append goes through ``appendio.append_records`` now, so the utf-8
    # it used to name is the one ``append_terminated`` encodes with, and
    # the ``appendio.py`` row below is where that contract is counted.
    "serve/intake_issue.py self.path.read_text(encoding='utf-8')",
    "serve/serve.py open(lock_path, 'a+', encoding='utf-8')",
    "serve/serve_gates.py open(lock_path, 'a+', encoding='utf-8')",
    "serve/serve_github.py path.read_text(encoding='utf-8')",
    "serve/serve_outcome.py path.read_text(encoding='utf-8')",
    "serve/serve_spend.py self.path.read_text(encoding='utf-8')",
    "verify/verify.py progress_path.read_text(encoding='utf-8')",
)


#: Every ``read_text``/``open`` call the walk decides is NOT its subject,
#: keyed by module and callee. Without this the walk has a silent fourth
#: bucket and ``encodingwalk.Scan``'s claim to partition is untested.
#:
#: The direction that matters here is the opposite of the cleared pin's.
#: A row ARRIVING means a call that used to answer to the encoding rule
#: now answers to nothing, which is how a widened ``STDLIB_READERS`` or a
#: newly resolvable receiver would remove a site from the guard in
#: silence.
#: Every read the walk can see and CANNOT PROVE anything about, keyed by
#: module and expression. Six rows, and the number is the whole point.
#:
#: #344 took five review rounds, and rounds one to four each ended the
#: same way: the walk cleared shapes that escaped at run time, a fix
#: landed, and the next round found more. Eight, nine, nineteen, then
#: nine again. Round 4 changed the rule - a site the walk cannot PROVE
#: compliant is undecided, never cleared - and round 5 applied that rule
#: to the three layers the round-4 change never reached: handle scope,
#: consumer timing, and members that change what decoding happens.
#:
#: THE COST, measured rather than hoped: 85 cleared with 19 known holes
#: became 78 cleared with 7 rows a reader can work through.
#:
#: ALL SIX ARE THE SAME FACT. A handle handed to a callee is a read this
#: walk cannot PLACE: ``json.load(f)`` reads eagerly and ``csv.reader(f)``
#: reads nothing at all, and no amount of AST tells them apart. All six
#: were checked by hand and all six are compliant - three cover the
#: decode, two have no handler so the caller answers, and ``factory.py``
#: stores the handle in a dataclass whose own read is tracked separately.
#: The rows say "I cannot establish when this callee reads", which is
#: true, rather than "fine", which was not.
#:
#: THE SEVENTH WAS ``verify.py``'s bad-patterns scan; #414 removed the row
#: rather than pinning it, because the scan reads bytes now, so there is
#: no `read_text` left to be undecided about (#425 simplify pass F6).
EXPECTED_UNDECIDED: tuple[str, ...] = (
    "cli/cli_preflight.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "plan/manifest.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "plan/prd.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "proc/factory_lock.py fp (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    # #632: `_dispatch_fixture` calls a runner through `_RUNNERS`, a callee
    # with no name. Checked by hand: the one runner that reads a file is
    "project/init_cmd.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
)


EXPECTED_DECIDED_OUT: tuple[str, ...] = (
    # #700 slice 3: the `up` log is opened "wb" and read back through
    # `readable_text`, which decodes with errors="replace". Binary, so no
    # codec applies at the open.
    "acceptance/replay.py log.open",
    "factory/factory_acceptance.py EvolutionJournal.open",
    # The journal's "a+b" probe-and-append. Binary, so no codec applies;
    # #331 moved it from evolution.py to appendio.open_for_append, where
    # six appenders now share it.
    "io/appendio.py open",
    "io/atomicio.py os.open",
    "pipeline/pipeline_attempt.py EvolutionJournal.open",
    "pipeline/pipeline_ledger.py EvolutionJournal.open",
)


class TestEveryReadInThePackageIsAccountedFor:
    """Layer 2 over ``kstrl/``: nothing reported, a pinned undecided row,
    and a named inventory of everything cleared.

    The inventory is here because ``reported == ()`` on its own is not a
    control - CLAUDE.md guard-design rule 2 - since it is also what a
    walk that has been switched off returns.
    Pinning the CLEARED half means a walk that stops seeing a read fails
    this test with the row it lost, which is the direction #324 records
    eleven guards failing in silently.

    THREE PINS AND NO FOURTH BUCKET. Every read the walk sees is in
    exactly one of ``EXPECTED_CLEARED_READS``, ``EXPECTED_UNDECIDED`` and
    the reported set, and every call it decides is not a read is in
    ``EXPECTED_DECIDED_OUT``. The undecided pin is the one #344 round 4
    added, and it exists because the alternative was worse: for three
    rounds the walk answered "clear" where the honest answer was "I did
    not look inside that", and each round's review found more of it.
    """

    def test_nothing_is_reported_and_the_undecided_row_is_the_pinned_one(self) -> None:
        assert_sites(
            reported_sites(package_scan()).without_line_numbers(),
            seen=(),
            undecided=EXPECTED_UNDECIDED,
            message=(
                "a text read in kstrl/ does not name utf-8, or sits under a "
                "fail-closed OSError handler with nothing covering "
                "UnicodeDecodeError. See this module's docstring for the rule."
            ),
        )

    def test_the_cleared_reads_are_the_ones_pinned(self) -> None:
        found = Sites(package_scan().clear).without_line_numbers()
        assert found.seen == EXPECTED_CLEARED_READS, (
            "the set of text reads this walk can see moved. A row that "
            "VANISHED is the dangerous direction: the walk stopped seeing a "
            f"read rather than the read being deleted. Found: {list(found.seen)}"
        )

    def test_the_decided_out_calls_are_the_ones_pinned(self) -> None:
        found = Sites(package_scan().decided_out).without_line_numbers()
        assert found.seen == EXPECTED_DECIDED_OUT, (
            "the set of read_text/open calls this walk decides are not its "
            "subject moved. A row ARRIVING is the dangerous direction: a "
            "text reader has been decided out and now answers to nothing. "
            f"Found: {list(found.seen)}"
        )
