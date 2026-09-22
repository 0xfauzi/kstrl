"""#320: every text read in ``kstrl/`` names utf-8 and answers for the decode.

THE RULE, which CLAUDE.md has stated since #291 and which nothing checked
until #320: a reader of any file kstrl writes must name
``encoding="utf-8"``, AND must catch ``ValueError`` alongside ``OSError``,
because ``UnicodeDecodeError`` is a ``ValueError`` and walks straight past
a fail-closed ``except OSError``. ``init_cmd._read_text_or_none`` is the
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
it. ``factory.py``'s run lock reads the holder pid back out of an ``"a+"``
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

import pytest

from tests.helpers.astwalk import (
    Sites,
    assert_census,
    assert_sites,
    blind_spot,
    package_sources,
    spells,
)
from tests.helpers.encodingspawn import (
    SPAWN_TARGETS,
    bytes_mode_census,
    reported_spawns,
    text_mode_census,
)
from tests.helpers.encodingspawn import (
    package_scan as spawn_scan,
)
from tests.helpers.encodingspawn import (
    scan_source as scan_spawn_source,
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
    "agents/codex.py": 1,
    "agents/logging.py": 1,
    # The "a+b" append open, moved here from evolution.py by #331.
    "appendio.py": 1,
    "atomicio.py": 1,
    "autonomy.py": 1,
    "autonomy_replay.py": 1,
    # 1 since #406's ``_read_document`` dedup, which the baseline reader
    # module (split out of calibration.py) now shares between callers.
    "calibration_baseline.py": 1,
    "cli.py": 1,
    "commandrun.py": 1,
    "decisions.py": 1,
    "decompose.py": 2,
    "events.py": 1,
    "evolution.py": 2,
    "factory.py": 6,
    "feature_cmd.py": 2,
    "feedforward.py": 8,
    # 3 until the snapshot half of fixtures.py moved to
    # fixtures_snapshot.py under the 800-line ratchet. The read that
    # went with it is the row below; the count is re-derived by
    # running the census, not edited to match.
    "fixtures.py": 2,
    "fixtures_snapshot.py": 1,
    "inbox.py": 1,
    # 3 until #352 routed ``_ensure_gitignore``'s append through
    # ``appendio``, which encodes once for every appender. The two left
    # are both reads: the PRD file and ``_read_text_or_none``.
    "init_cmd.py": 2,
    "init_wizard.py": 1,
    # Back to 1 (the ledger's own read) since #231's simplify pass (B1):
    # the memory-file read `_append_guidance` added moved to
    # `operator_context.append_guidance_record`, which is the module
    # that already owns the reader.
    "intake_github.py": 1,
    "knowledge.py": 3,
    "licensing.py": 1,
    "loop.py": 2,
    "manifest.py": 1,
    "observability.py": 1,
    # Back to 1 (the existing `read_operator_file` read): the
    # `append_guidance_record` read #231 B1 added here moved again, to
    # `operator_guidance.py`, when this module crossed the 800-line
    # ratchet and that write path split out (the addendum's simplify
    # pass).
    "operator_context.py": 1,
    # The `append_guidance_record` read, moved from `operator_context.py`
    # in the same split.
    "operator_guidance.py": 1,
    "parsers.py": 1,
    "pipeline.py": 3,
    "prd.py": 1,
    # 4 until #352 routed ``mark_applied`` through ``appendio`` for the
    # same reason. The three left are the CLAUDE.md read, the proposal
    # read, and the CLAUDE.md write.
    "proposals.py": 3,
    "security.py": 1,
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
    # deduplicated spelling list below loses a row.
    "serve.py": 5,
    "statedir.py": 1,
    "tui/embed.py": 1,
    "tui/runs.py": 2,
    "tui/session.py": 1,
    "tui/tail.py": 2,
    # 4 since #414: the bad-patterns scan's read_text is gone (it reads
    # bytes now, so py_compile does its own PEP 263 decoding). The four
    # left: CLAUDE.md, the self-critique progress log, check_test_adequacy's
    # read of a changed test's source, and check_patch_coverage's report.
    "verify.py": 4,
    "workqueue.py": 6,
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
    "agents/codex.py last_msg_file.read_text(encoding='utf-8')",
    "agents/logging.py self._log_path.open('a', encoding='utf-8')",
    "autonomy.py path.read_text(encoding='utf-8')",
    # Two rows became one in #331. ``load_runs`` held a utf-8 handle open
    # and ran ``csv.DictReader`` over it, which the walk had to clear as
    # two separate reads; it reads the text once now and hands it to
    # ``evolution.experiment_rows``, the parser its sibling reader
    # ``get_experiment_trends`` already used. The disposition is stronger,
    # not merely different: the old pair named utf-8 under no handler at
    # all, and this names utf-8 AND catches ``ValueError`` beside
    # ``OSError``, which is the half a ``UnicodeDecodeError`` escapes.
    "autonomy_replay.py path.read_text(encoding='utf-8')",
    "calibration_baseline.py path.read_text(encoding='utf-8')",
    "cli.py open(prd_file, encoding='utf-8')",
    "commandrun.py open(path, 'a', buffering=1, encoding='utf-8')",
    "decisions.py path.read_text(encoding='utf-8')",
    "decompose.py (spec_path / name).read_text(encoding='utf-8')",
    "decompose.py spec_path.read_text(encoding='utf-8')",
    "events.py open(path, encoding='utf-8', errors='replace')",
    "evolution.py self.config.experiments_path.read_text(encoding='utf-8')",
    "factory.py fp.read(64) on an open() handle",
    "factory.py open(lock_path, 'a+', encoding='utf-8')",
    "factory.py open(lock_path, 'w', encoding='utf-8')",
    "factory.py open(log_path, 'a', buffering=1, encoding='utf-8', errors='replace')",
    "factory.py open(run_paths.engineer_log(component_id), 'a', buffering=1, encoding=",
    "factory.py path.read_text(encoding='utf-8')",
    "feature_cmd.py open(latest_path, 'w', encoding='utf-8')",
    "feature_cmd.py open(repair_path, 'w', encoding='utf-8')",
    "feedforward.py filepath.read_text(encoding='utf-8', errors='replace')",
    "feedforward.py path.read_text(encoding='utf-8')",
    "feedforward.py path.read_text(encoding='utf-8', errors='replace')",
    "feedforward.py py_file.read_text(encoding='utf-8', errors='replace')",
    "fixtures.py full_path.read_text(encoding='utf-8')",
    "fixtures.py open(prd_path, encoding='utf-8')",
    "fixtures_snapshot.py snapshot_path.read_text(encoding='utf-8')",
    "init_cmd.py open(prd_file, encoding='utf-8')",
    # ``init_cmd.py path.open('a', encoding='utf-8')`` was here until
    # #352. The read is DELETED, not unseen: ``_ensure_gitignore``'s
    # append goes through ``appendio.append_records`` now, so the utf-8
    # it used to name is the one ``append_terminated`` encodes with, and
    # the ``appendio.py`` row below is where that contract is counted.
    "init_cmd.py path.read_text(encoding='utf-8')",
    "init_wizard.py toml_path.read_text(encoding='utf-8')",
    "intake_github.py self.path.read_text(encoding='utf-8')",
    "knowledge.py path.read_text(encoding='utf-8')",
    "knowledge.py prd_path.read_text(encoding='utf-8')",
    "knowledge.py target.read_text(encoding='utf-8')",
    "licensing.py Path(match).read_text(encoding='utf-8', errors='replace')",
    "loop.py claude_md_path.read_text(encoding='utf-8')",
    "loop.py config.prompt_file.read_text(encoding='utf-8')",
    "manifest.py open(path, encoding='utf-8')",
    "observability.py for line in f on an open() handle",
    "observability.py open(path, encoding='utf-8')",
    "operator_context.py spec.path.read_text(encoding='utf-8')",
    # `append_guidance_record`'s read, same spelling as the row above
    # because both read an `OperatorFile.path`, moved to
    # `operator_guidance.py` when that write path split out of
    # `operator_context.py` under the 800-line ratchet.
    "operator_guidance.py spec.path.read_text(encoding='utf-8')",
    "parsers.py source_path.read_text(encoding='utf-8')",
    "pipeline.py open(path, 'a', buffering=1, encoding='utf-8')",
    "pipeline.py progress_path.read_text(encoding='utf-8')",
    "prd.py open(path, encoding='utf-8')",
    "proposals.py claude_md.read_text(encoding='utf-8')",
    # ``proposals.py open(path, 'a', encoding='utf-8')`` was here until
    # #352, and is deleted for the same reason as the ``init_cmd`` row
    # above: ``mark_applied`` appends through ``appendio`` now.
    "proposals.py path.read_text(encoding='utf-8')",
    "security.py prd_path.read_text(encoding='utf-8')",
    "serve.py open(lock_path, 'a+', encoding='utf-8')",
    "serve.py path.read_text(encoding='utf-8')",
    "serve.py self.path.read_text(encoding='utf-8')",
    "statedir.py open(lock_path, 'a+', encoding='utf-8')",
    "tui/embed.py open(run_paths.root / 'orchestrator.log', 'a', buffering=1, encoding='",
    "tui/runs.py open(lock_path, 'a+', encoding='utf-8')",
    "tui/session.py open(run_paths.root / 'orchestrator.log', 'a', buffering=1, encoding='",
    "verify.py (root / 'CLAUDE.md').read_text(encoding='utf-8')",
    "verify.py full.read_text(encoding='utf-8', errors='replace')",
    "verify.py json_path.read_text(encoding='utf-8')",
    "verify.py progress_path.read_text(encoding='utf-8')",
    "workqueue.py meta_path.read_text(encoding='utf-8')",
    "workqueue.py open(lock_path, 'a+', encoding='utf-8')",
    "workqueue.py self.journal_path.read_text(encoding='utf-8')",
    "workqueue.py self.pause_path.read_text(encoding='utf-8')",
    "workqueue.py self.spec_path(item).read_text(encoding='utf-8')",
    "workqueue.py spec_source.read_text(encoding='utf-8')",
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
    "cli.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "factory.py fp (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "fixtures.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "init_cmd.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "manifest.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
    "prd.py f (the read is deferred to wherever this value is drained, "
    "which this walk cannot locate, so no handler can be credited with "
    "covering it)",
)


EXPECTED_DECIDED_OUT: tuple[str, ...] = (
    # The journal's "a+b" probe-and-append. Binary, so no codec applies;
    # #331 moved it from evolution.py to appendio.open_for_append, where
    # six appenders now share it.
    "appendio.py open",
    "atomicio.py os.open",
    "factory.py EvolutionJournal.open",
    "pipeline.py EvolutionJournal.open",
    "tui/runs.py open",
    "tui/tail.py open",
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


# --------------------------------------------------------------------------
# #409: THE SECOND POPULATION -- child-process spawns, layer 1 and layer 2.
# --------------------------------------------------------------------------

#: Every AST node in ``kstrl/`` whose folded value is EXACTLY ``"subprocess"``
#: - the module name itself, not any of the seven callables built on it -
#: counted per module. ``spells(token)`` is an EQUALITY net
#: (``tests/helpers/astwalk/net.py``), so it does not count the word inside
#: a longer prose sentence; it counts ``import subprocess``,
#: ``import subprocess as sp``, ``from subprocess import run as _run`` (the
#: module name lives in ``ImportFrom.module``) and a bare ``subprocess``
#: reference such as the attribute root of ``subprocess.run(...)``.
#: Deliberately generous the same way ``EXPECTED_READ_SPELLINGS`` is: a
#: module cannot spawn without naming ``subprocess`` somewhere, and a net
#: that decides what to leave out can be wrong about what it left out.
#: Unchanged by this fix: adding ``encoding="utf-8"`` spells no ``subprocess``.
EXPECTED_SUBPROCESS_SPELLINGS: dict[str, int] = {
    "agents/codex.py": 4,
    "agents/proc.py": 7,
    "breaker.py": 4,
    "contract.py": 5,
    "doctor.py": 5,
    "factory.py": 13,
    "fixtures.py": 3,
    # 58: 57 since #435 (see git blame), +1 for `merge_base_ref`, hoisted
    # here from `verify.py` by the #435 fix-round's A0.
    "git.py": 58,
    "intake_github.py": 3,
    "licensing.py": 3,
    "observability.py": 5,
    "pr.py": 22,
    "procdispose.py": 10,
    "procgroup.py": 4,
    "procgroup_listing.py": 6,
    "retry_plan.py": 5,
    "serve.py": 8,
    "statedir.py": 3,
    "timeout.py": 3,
    "tui/screens/home.py": 3,
    "tui/screens/retry.py": 2,
    # 25: `_base_finding`'s `git show` spawn (#414/#425); the sibling
    # `git merge-base` spawn moved to `git.py` with the #435 fix-round.
    "verify.py": 25,
}


#: How many TEXT-MODE spawns each module holds. Unchanged by replacing
#: ``text=True,`` with ``encoding="utf-8",``, which moves a site between
#: ``clear`` and ``reported``, never between text mode and bytes mode.
#: ``timeout.py`` is NOT a row here: its one text-mode-looking call also
#: forwards ``**kwargs`` (A1, #409's simplify pass), so it is undecided
#: rather than counted as text mode at all - see ``EXPECTED_UNDECIDED_SPAWNS``.
EXPECTED_TEXT_MODE_SPAWNS: dict[str, int] = {
    "agents/codex.py": 1,
    "agents/proc.py": 1,
    "breaker.py": 1,
    "doctor.py": 1,
    "factory.py": 3,
    # 23 since #435: `resolve_ref`'s deleted body held one text-mode spawn.
    "git.py": 23,
    "intake_github.py": 1,
    "licensing.py": 1,
    "pr.py": 11,
    "procgroup_listing.py": 1,
    "retry_plan.py": 1,
    "serve.py": 1,
    "statedir.py": 1,
    "tui/screens/home.py": 1,
    "verify.py": 1,
}


#: Every text-mode spawn this walk CLEARS, keyed by module and expression,
#: deduplicated so a repeated identical call counts once. A row VANISHING is
#: the dangerous direction: the walk stopped seeing a spawn rather than the
#: spawn being deleted.
EXPECTED_CLEARED_SPAWNS: tuple[str, ...] = (
    "agents/codex.py subprocess.run(['codex', 'exec', '--help'], check=False, stdout=subpro",
    "agents/proc.py subprocess.Popen(cmd, shell=shell, stdin=subprocess.PIPE, stdout=subpr",
    "breaker.py subprocess.run(['git', *args], cwd=cwd, capture_output=True, encoding=",
    # codespell:ignore-next-line
    "doctor.py subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=Tru",
    "factory.py subprocess.run(['git', 'branch', '-D', branch], cwd=root_dir, capture_",
    "factory.py subprocess.run(['git', 'worktree', 'add', str(worktree_path), '-b', br",
    "factory.py subprocess.run(['git', 'worktree', 'add', str(worktree_path), branch_n",
    # codespell:ignore-next-line
    "git.py subprocess.run(['git', 'add', '--', file], cwd=cwd, capture_output=Tru",
    "git.py subprocess.run(['git', 'branch', flag, '--', branch_name], cwd=cwd, ca",
    "git.py subprocess.run(['git', 'check-ignore', '-v', '--', file], cwd=cwd, cap",
    "git.py subprocess.run(['git', 'checkout', '-b', branch], cwd=cwd, capture_out",
    "git.py subprocess.run(['git', 'checkout', '-b', branch_name, base, '--'], cwd",
    "git.py subprocess.run(['git', 'checkout', branch, '--'], cwd=cwd, capture_out",
    "git.py subprocess.run(['git', 'config', '--get', 'remote.origin.url'], cwd=cw",
    "git.py subprocess.run(['git', 'diff', '--name-only', '--cached', '-z'], cwd=c",
    "git.py subprocess.run(['git', 'diff', '--name-only', '-z'], cwd=cwd, capture_",
    "git.py subprocess.run(['git', 'diff', '--name-status', '-z', '-M', '-C', f'{b",
    "git.py subprocess.run(['git', 'diff', '--name-status', '-z', ref, '--'], cwd=",
    "git.py subprocess.run(['git', 'diff', '--numstat', '-z', f'{base_ref}...HEAD'",
    "git.py subprocess.run(['git', 'diff', f'{base_ref}...HEAD', '--'], cwd=cwd, c",
    "git.py subprocess.run(['git', 'fetch', '--', 'origin', base_branch], cwd=cwd,",
    "git.py subprocess.run(['git', 'for-each-ref', '--format=%(refname)%09%(symref",
    "git.py subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '",
    "git.py subprocess.run(['git', 'merge', '--no-edit', '--', branch], cwd=cwd, c",
    "git.py subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=cwd,",
    "git.py subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=path",
    "git.py subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=path, capt",
    "git.py subprocess.run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD'], cw",
    # codespell:ignore-next-line
    "git.py subprocess.run(['git', 'rev-parse', '--verify', '--quiet', f'{candidat",
    "intake_github.py subprocess.run(['gh', *args], cwd=str(cwd) if cwd else None, capture_o",
    "licensing.py subprocess.run(['uv', 'cache', 'dir'], capture_output=True, encoding='",
    "pr.py subprocess.run(['gh', 'auth', 'status'], capture_output=True, encoding",
    "pr.py subprocess.run(['gh', 'pr', 'close', str(pr_number), '--comment', 'Sup",
    "pr.py subprocess.run(['gh', 'pr', 'create', '--title', title, '--body', body",
    "pr.py subprocess.run(['gh', 'pr', 'merge', str(pr_number), f'--{method}', '-",
    "pr.py subprocess.run(['gh', 'pr', 'merge', str(pr_number), f'--{method}'], c",
    "pr.py subprocess.run(['gh', 'pr', 'view', str(pr_number), '--json', 'mergeab",
    "pr.py subprocess.run(['gh', 'pr', 'view', str(pr_number), '--json', 'state']",
    "pr.py subprocess.run(['git', 'push', '--delete', '--', 'origin', branch], cw",
    "pr.py subprocess.run(['git', 'push', '-u', '--', 'origin', branch], cwd=cwd,",
    "retry_plan.py subprocess.run(['git', 'branch', '-D', failed_branch], cwd=root_dir, c",
    "serve.py subprocess.Popen(command, cwd=str(cwd), env=env, stdout=subprocess.PIP",
    "statedir.py subprocess.run(['git', '-C', str(root_dir), 'remote', 'get-url', 'orig",
    "tui/screens/home.py subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=root_",
    "verify.py subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, stdout=subp",
)


#: Every text-mode spawn that names utf-8 and decodes LENIENTLY, kept out of
#: the cleared set on purpose: ``procgroup_listing.py`` reads ``ps`` output
#: for a daemon diagnostic, and a decode error there is a ``ValueError``
#: that would escape a fail-closed ``except OSError`` and take the daemon
#: down over a diagnostic. It is the one lenient decode in the package. A
#: SECOND row arriving here is the dangerous direction: it means ``errors=``
#: was used to quiet a site rather than to name a considered exception.
EXPECTED_LENIENT_SPAWNS: tuple[str, ...] = (
    "procgroup_listing.py subprocess.Popen(PS_ARGV, stdout=subprocess.PIPE, stderr=subprocess.PI",
)


#: How many spawns that decode nothing (no ``text=``, no
#: ``universal_newlines=``, no ``encoding=``, no ``errors=``) each module
#: holds. Unchanged by this fix, which touches only text-mode sites. A COUNT
#: dict rather than a row inventory (D2, #409's simplify pass), so a site
#: misfiled INTO this bucket moves a number here even when its unparsed text
#: happens to deduplicate against an existing row - which is exactly what
#: A1's ``**kwargs`` misfile did before A1 existed. A count going UP is the
#: dangerous direction: it means a site that used to decode now does not, a
#: behaviour change nobody declared; adding an encoding to one of these
#: would flip it to text mode and change its ``.stdout`` from ``bytes`` to
#: ``str``.
EXPECTED_BYTES_MODE_SPAWNS: dict[str, int] = {
    "doctor.py": 1,
    "factory.py": 9,
    # 7: `merge_base_ref`'s `git merge-base` stdout, hoisted here from
    # `verify.py` by the #435 fix-round, still decoded by hand.
    "git.py": 7,
    "observability.py": 1,
    "retry_plan.py": 3,
    # 1: `_base_finding`'s `git show` (#414/#425); its sibling moved above.
    "verify.py": 1,
}


#: Two calls this walk cannot fully decide, sorted and deduplicated to one
#: row each. ``cli.py app.run`` is both of ``kstrl/cli.py``'s ``app.run()``
#: calls: the resolver cannot type ``app``, so it is a candidate it cannot
#: decide either way. Checked by hand: both are the Textual application's
#: own ``run``. ``timeout.py subprocess.run`` is ``run_with_timeout``, which
#: names ``encoding="utf-8"`` explicitly AND forwards ``**kwargs`` to the
#: same call (A1): the forwarded dict is opaque, so no explicit keyword on
#: the call proves anything, and a caller could pass ``errors="replace"``
#: through it. Measured against the walk before A1: this exact shape was
#: wrongly CLEARED, not bytes mode - the more severe of the two over-matches
#: A1 closes; see the two probes below that cover both.
EXPECTED_UNDECIDED_SPAWNS: tuple[str, ...] = (
    "cli.py app.run",
    "timeout.py subprocess.run",
)


class TestEverySpawnDecodeNamesUtf8:
    """#409, layer 1 and layer 2 over the second population: child-process
    spawns rather than files kstrl reads.

    FIVE PINS, EACH ITS OWN BUCKET: every text-mode spawn is in exactly one
    of ``EXPECTED_CLEARED_SPAWNS``, ``EXPECTED_LENIENT_SPAWNS``,
    ``EXPECTED_UNDECIDED_SPAWNS`` and the reported set (asserted empty), and
    every spawn that decodes nothing is counted in
    ``EXPECTED_BYTES_MODE_SPAWNS``.
    """

    def test_the_subprocess_spellings_are_the_ones_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=spells("subprocess"),
            expected=EXPECTED_SUBPROCESS_SPELLINGS,
            # One control, because `spells` is one predicate with one
            # disjunct, unlike the read walk's `read_text or open`.
            control="import subprocess",
            message="a module's subprocess spellings moved.",
        )

    def test_nothing_is_reported_and_the_undecided_row_is_the_pinned_one(self) -> None:
        """``seen=()`` on its own is the shape CLAUDE.md guard rule 2
        forbids: it passes when the walk is right AND when the walk has
        been switched off. Its controls are the three planted-shape probes
        below, which run ``scan_spawn_source`` directly and assert it DOES
        report, plus ``undecided=EXPECTED_UNDECIDED_SPAWNS`` itself, a
        non-empty claim that a gutted walk also fails.
        """
        assert_sites(
            reported_spawns(spawn_scan()).without_line_numbers(),
            seen=(),
            undecided=EXPECTED_UNDECIDED_SPAWNS,
            message=(
                "a text-mode subprocess spawn in kstrl/ does not name utf-8 "
                "strictly. See this module's docstring for the rule."
            ),
        )

    def test_the_cleared_spawns_are_the_ones_pinned(self) -> None:
        found = Sites(spawn_scan().clear).without_line_numbers()
        assert found.seen == EXPECTED_CLEARED_SPAWNS, (
            "the set of text-mode spawns this walk clears moved. A row that "
            "VANISHED is the dangerous direction: the walk stopped seeing a "
            f"spawn rather than the spawn being deleted. Found: {list(found.seen)}"
        )

    def test_the_text_mode_spawn_counts_are_the_ones_pinned(self) -> None:
        assert text_mode_census(spawn_scan()) == EXPECTED_TEXT_MODE_SPAWNS

    def test_the_lenient_spawn_is_the_one_pinned(self) -> None:
        found = Sites(spawn_scan().lenient).without_line_numbers()
        assert found.seen == EXPECTED_LENIENT_SPAWNS, (
            "the set of lenient-decode spawns moved. A row ARRIVING is the "
            f"dangerous direction: errors= quieted a site. Found: {list(found.seen)}"
        )

    def test_the_bytes_mode_spawn_counts_are_the_ones_pinned(self) -> None:
        assert bytes_mode_census(spawn_scan()) == EXPECTED_BYTES_MODE_SPAWNS, (
            "the count of bytes-mode spawns per module moved. A count going "
            "UP means a site that used to decode now does not, a behaviour "
            "change nobody declared."
        )

    def test_the_spawn_targets_are_the_seven_and_agree_with_the_timeout_audit(
        self,
    ) -> None:
        """Derived from ``inspect.signature`` over every public ``subprocess``
        callable, not a hand-written list: #340 and #324 merged a
        hand-written ``SPAWN_FUNCS`` into a tree at zero readers with
        ``getoutput`` ungated, so a CPython release adding or dropping one
        of these seven must be loud rather than silent, AND two lists
        naming one concept is exactly what let that merge through with both
        branches independently green and no conflict on either constant.
        """
        assert SPAWN_TARGETS == frozenset(
            {
                "subprocess.Popen",
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "subprocess.getoutput",
                "subprocess.getstatusoutput",
                "subprocess.run",
            }
        )

        from tests.test_timeout_enforcement import POPEN_TARGET, TestSubprocessTimeoutAudit

        assert SPAWN_TARGETS == TestSubprocessTimeoutAudit.SPAWN_TARGETS | {POPEN_TARGET}

    def test_a_spawn_that_names_no_encoding_is_reported(self) -> None:
        scan = scan_spawn_source('import subprocess\nsubprocess.run(["x"], text=True)\n')
        assert scan.clear == ()
        assert len(scan.reported) == 1
        assert "names no encoding" in scan.reported[0]

    def test_a_spawn_that_decodes_leniently_is_not_cleared(self) -> None:
        scan = scan_spawn_source(
            'import subprocess\nsubprocess.run(["x"], encoding="utf-8", errors="replace")\n'
        )
        assert scan.clear == ()
        assert len(scan.lenient) == 1

    def test_a_spawn_with_errors_and_no_encoding_is_reported(self) -> None:
        """CPython's second disjunct: an ``errors=`` alone is TEXT mode with
        the LOCALE encoding, so filing it as bytes mode is a false clear."""
        scan = scan_spawn_source('import subprocess\nsubprocess.run(["x"], errors="replace")\n')
        assert scan.bytes_mode == ()
        assert scan.clear == ()
        assert len(scan.reported) == 1

    def test_a_spawn_whose_text_flag_does_not_fold_is_reported(self) -> None:
        scan = scan_spawn_source('import subprocess\nsubprocess.run(["x"], text=flag)\n')
        assert len(scan.reported) == 1
        assert "does not fold" in scan.reported[0]

    def test_a_spawn_naming_encoding_and_forwarding_kwargs_is_undecided_not_cleared(
        self,
    ) -> None:
        """A1, the shape live in ``kstrl/timeout.py``: ``**fwd`` is opaque,
        so the explicit ``encoding="utf-8"`` beside it proves nothing - the
        dict could carry an ``errors=`` this walk never sees. Measured
        against the pre-fix walk: this shape was wrongly CLEARED.
        """
        scan = scan_spawn_source(
            'import subprocess\nsubprocess.run(["x"], encoding="utf-8", **fwd)\n'
        )
        assert scan.clear == ()
        assert scan.bytes_mode == ()
        assert scan.reported == ()
        assert len(scan.undecided) == 1
        assert "subprocess.run" in scan.undecided[0]

    def test_a_bare_kwargs_spawn_is_undecided_not_bytes_mode(self) -> None:
        """A1's other half: no explicit keyword at all, so ``text_mode``
        found no ``text=``/``universal_newlines=`` flag and returned
        ``False``. Filed as BYTES MODE before this fix - the other
        over-matching answer to the same opaque ``**kwargs``.
        """
        scan = scan_spawn_source("import subprocess\nsubprocess.run(cmd, **kwargs)\n")
        assert scan.clear == ()
        assert scan.bytes_mode == ()
        assert scan.reported == ()
        assert len(scan.undecided) == 1
        assert "subprocess.run" in scan.undecided[0]

    def test_a_spawn_whose_errors_does_not_fold_is_reported_not_cleared(self) -> None:
        """A2. ``is_strict`` treats an unfoldable ``errors=`` as strict,
        #320's READ rule's correct answer and this SPAWN rule's wrong one: a
        variable could hold ``"replace"`` at run time. This file asks the
        question itself rather than changing ``is_strict``, which the read
        rule also relies on.
        """
        scan = scan_spawn_source(
            'import subprocess\nsubprocess.run(["x"], encoding="utf-8", errors=mode)\n'
        )
        assert scan.clear == ()
        assert scan.lenient == ()
        assert len(scan.reported) == 1
        assert "strictness is unproven" in scan.reported[0]

    def _spawn_walk_sees_anything(self, source: str) -> bool:
        """True when the spawn walk registers ANYTHING for this source, in
        any of its five buckets. Used only by the two disclosures below."""
        scan = scan_spawn_source(source)
        return bool(
            scan.clear or scan.reported or scan.undecided or scan.lenient or scan.bytes_mode
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_asyncio_create_subprocess_exec_is_a_blind_spot(self) -> None:
        """A3, disclosed rather than silently missed. Not one of the seven
        ``subprocess`` callables ``SPAWN_TARGETS`` derives, so a call to it
        is not even a CANDIDATE for either bucket. Neither shape exists in
        ``kstrl/`` today; a widened walk XPASSes and ``strict=True`` fails
        that, so the disclosure is edited in the same diff.
        """
        blind_spot(
            self._spawn_walk_sees_anything,
            "import asyncio\n\n\nasync def f():\n"
            '    p = await asyncio.create_subprocess_exec("x")\n'
            "    out = (await p.communicate())[0].decode()\n",
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_os_popen_is_a_blind_spot(self) -> None:
        """A3's sibling: not a ``subprocess`` callable at all, so it is
        invisible to a target set derived from ``inspect.signature`` over
        that one module, and to ``scan_source``'s own literal-token guard.
        """
        blind_spot(self._spawn_walk_sees_anything, 'import os\nos.popen("ls").read()\n')
