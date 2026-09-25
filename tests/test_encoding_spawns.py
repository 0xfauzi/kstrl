"""#409: every child-process spawn in kstrl/ decodes its output as utf-8.

Split out of tests/test_encoding_readers.py under the 800-line ratchet
(#482). The rule and its history are in that module's docstring.
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

# --------------------------------------------------------------------------
# #409: THE SECOND POPULATION -- child-process spawns, layer 1 and layer 2.
# --------------------------------------------------------------------------

#: Every AST node in ``kstrl/`` whose folded value is EXACTLY ``"subprocess"`` - the module name
#: itself, not any of the seven callables built on it - counted per module. ``spells(token)`` is an
#: EQUALITY net (``tests/helpers/astwalk/net.py``), so it does not count the word inside a longer
#: prose sentence; it counts ``import subprocess``, ``import subprocess as sp``,
#: ``from subprocess import run as _run`` (the module name lives in ``ImportFrom.module``) and a
#: bare ``subprocess`` reference such as the attribute root of ``subprocess.run(...)``.
#: Deliberately generous the same way ``EXPECTED_READ_SPELLINGS`` is: a module cannot spawn
#: without naming ``subprocess`` somewhere, and a net that decides what to leave out can be wrong
#: about what it left out.
#: Unchanged by this fix: adding ``encoding="utf-8"`` spells no ``subprocess``.
EXPECTED_SUBPROCESS_SPELLINGS: dict[str, int] = {
    "agents/codex.py": 4,
    "agents/proc.py": 7,
    "breaker.py": 4,
    "contract.py": 5,
    "doctor.py": 5,
    "factory.py": 15,
    "fixtures.py": 3,
    # 64: 58 after #435, +2 for #465's `branch_sha`, +2 for #459's `ignored_paths`,
    # +2 for #500's `tracked_files_at`.
    "git.py": 64,
    "intake_github.py": 3,
    # #508: the import, and `TimeoutExpired` from `run_scrubbed`.
    "learning_fixture.py": 2,
    "licensing.py": 3,
    "observability.py": 5,
    "pr.py": 18,
    "pr_state.py": 5,
    "procdispose.py": 10,
    "procgroup.py": 4,
    "procgroup_listing.py": 6,
    "retry_plan.py": 5,
    "serve.py": 8,
    "statedir.py": 3,
    "timeout.py": 3,
    "tui/screens/home.py": 3,
    "tui/screens/retry.py": 2,
    # 25: `_base_finding`'s `git show` (#414/#425); `git merge-base` moved to `git.py` in #435.
    "verify.py": 25,
    "worktree_sweep.py": 2,  # #461: the import, and `TimeoutExpired` from `run_scrubbed`
}


#: How many TEXT-MODE spawns each module holds. Unchanged by replacing ``text=True,``
#: with ``encoding="utf-8",``, which moves a site between ``clear`` and ``reported``,
#: never between text mode and bytes mode.
#: ``timeout.py`` is NOT a row here: its one text-mode-looking call also
#: forwards ``**kwargs`` (A1, #409's simplify pass), so it is undecided
#: rather than counted as text mode at all - see ``EXPECTED_UNDECIDED_SPAWNS``.
EXPECTED_TEXT_MODE_SPAWNS: dict[str, int] = {
    "agents/codex.py": 1,
    "agents/proc.py": 1,
    "breaker.py": 1,
    "doctor.py": 1,
    "factory.py": 4,
    # 26: 23 after #435, +1 for #465's `branch_sha`, +1 for #459's `ignored_paths`,
    # +1 for #500's `tracked_files_at`.
    "git.py": 26,
    "intake_github.py": 1,
    "licensing.py": 1,
    "pr.py": 9,
    "pr_state.py": 2,
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
    "factory.py subprocess.run(['git', 'worktree', 'list', '--porcelain', '-z'], cwd=r",
    # codespell:ignore-next-line
    "git.py subprocess.run(['git', 'add', '--', file], cwd=cwd, capture_output=Tru",
    "git.py subprocess.run(['git', 'branch', flag, '--', branch_name], cwd=cwd, ca",
    "git.py subprocess.run(['git', 'check-ignore', '--stdin', '-z'], cwd=cwd, capt",
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
    "git.py subprocess.run(['git', 'ls-tree', '-r', '--name-only', '-z', sha], cwd",
    "git.py subprocess.run(['git', 'merge', '--no-edit', '--', branch], cwd=cwd, c",
    "git.py subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=cwd,",
    "git.py subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=path",
    "git.py subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=path, capt",
    "git.py subprocess.run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD'], cw",
    "git.py subprocess.run(['git', 'rev-parse', '--verify', '--quiet', f'refs/head",
    # codespell:ignore-next-line
    "git.py subprocess.run(['git', 'rev-parse', '--verify', '--quiet', f'{candidat",
    "intake_github.py subprocess.run(['gh', *args], cwd=str(cwd) if cwd else None, capture_o",
    "licensing.py subprocess.run(['uv', 'cache', 'dir'], capture_output=True, encoding='",
    "pr.py subprocess.run(['gh', 'auth', 'status'], capture_output=True, encoding",
    "pr.py subprocess.run(['gh', 'pr', 'close', str(pr_number), '--comment', 'Sup",
    "pr.py subprocess.run(['gh', 'pr', 'create', '--title', title, '--body', body",
    "pr.py subprocess.run(['gh', 'pr', 'merge', str(pr_number), f'--{method}', '-",
    "pr.py subprocess.run(['gh', 'pr', 'merge', str(pr_number), f'--{method}'], c",
    "pr.py subprocess.run(['git', 'push', '--delete', '--', 'origin', branch], cw",
    "pr.py subprocess.run(['git', 'push', '-u', '--', 'origin', branch], cwd=cwd,",
    "pr_state.py subprocess.run(['gh', 'pr', 'view', str(pr_number), '--json', 'mergeab",
    "pr_state.py subprocess.run(['gh', 'pr', 'view', str(pr_number), '--json', 'state,m",
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
