"""Positive controls for every shape the process-lifecycle guard claims.

``tests/test_process_lifecycle.py`` asserts that a set is empty or a
pinned inventory unchanged. So does a switched-off detector. This file
is what makes those assertions mean something: each shape below is
planted on its own and the matcher is asked about it, so a matcher that
quietly stops matching fails HERE rather than passing silently there.

The mechanism is per-matcher rather than per-test: every public matcher
in the guard has at least one control that fires on it, so stubbing any
one of them to a constant turns this file red. That is the same
arrangement ``tests/test_event_name_shapes.py`` documents next door, and
it exists because #324 records eleven guards in this repo that were
walked past, two of them a SECOND time after being fixed once.

Layer 1 is what carries almost all of them, and the reason is structural
rather than lucky: it enumerates no node types at all, so an aliased
import, a rebind, a partial, a dict value, a parameter default, a tuple
unpack, a loop variable, a class attribute, a ``setattr`` and a function
returning the bare name are all the same to it. Those last seven are the
exact shapes #336 measured surviving a widened node-type matcher.

Layer 2's resolvers are message quality for every name but ``wait``:
everything else they resolve, layer 1 has already counted. ``wait`` is
the exception because the bare word costs 9 false rows in the
vocabulary, so there layer 2 is the coverage; the guard docstring argues
it and row 51 discloses what it still misses.

THE EVASION TABLE. Each row is a way a process can be created, signalled
or reaped so that a matcher built from a list of node types misses it.
Every row has a verdict and a named test, and there are no rows without
one: 51 shapes, 46 COVERED and 5 DISCLOSED. A disclosed row is not a
caveat in prose - it is asserted in ``TestWhatTheGuardCannotSee`` in the
guard file, so the disclosure fails if it stops being true, which is the
bar ``tests/test_journal_one_writer.py`` sets.

===  ================================  =========  ==================================================
  #  Shape                             Verdict    Test
===  ================================  =========  ==================================================
  1  ``import subprocess``, attr call  COVERED    a_plain_import_and_attribute_call
  2  ``import subprocess as sp``       COVERED    an_aliased_module_import
  3  ``from subprocess import Popen``  COVERED    a_from_import
  4  a from-import renaming ``Popen``  COVERED    a_from_import_renamed
  5  a from-import renaming ``run``    COVERED    a_from_import_renamed_for_a_run
  6  rebind ``S = subprocess.Popen``   COVERED    a_module_level_rebind
  7  ``functools.partial(Popen)``      COVERED    a_functools_partial
  8  a wrapper fn in this module       COVERED    a_wrapper_function_in_the_same_module
  9  a wrapper via a PARAMETER         DISCLOSED  a_spawner_reached_through_a_parameter_is_invisible
 10  a name from a dict literal        COVERED    a_name_pulled_out_of_a_dict
 11  a name from ``dict(zip(..))``     COVERED    a_name_built_by_dict_zip
 12  a module-level CONSTANT tuple     COVERED    a_module_level_constant_tuple
 13  ``getattr(subprocess,'Popen')``   COVERED    a_getattr_with_a_literal_name
 14  ``getattr(os, "kill")``           COVERED    a_getattr_kill
 15  ``getattr(os, name)``, computed   DISCLOSED  getattr_with_a_computed_attribute_is_invisible
 16  ``os.system``                     COVERED    os_system
 17  ``os.popen``                      COVERED    os_popen
 18  ``os.spawnv`` and ``spawn*``      COVERED    os_spawn_family
 19  ``os.posix_spawn(p)``             COVERED    os_posix_spawn
 20  ``os.execv`` and ``exec*``        COVERED    os_exec_family
 21  ``os.fork`` / ``os.forkpty``      COVERED    os_fork
 22  ``pty.spawn``/``openpty``         COVERED    pty_spawn
 23  ``multiprocessing.Process``       COVERED    multiprocessing
 24  ``from multiprocessing.context``  COVERED    a_dotted_module_path
 25  ``asyncio.create_subprocess_*``   COVERED    asyncio_create_subprocess
 26  ``import asyncio.subprocess``     COVERED    a_dotted_module_path
 27  ``ProcessPoolExecutor``           COVERED    a_process_pool_executor
 28  ``pexpect.spawn``                 COVERED    pexpect
 29  ``getoutput`` and its sibling     COVERED    getoutput_and_getstatusoutput
 30  a ``shutil`` helper shelling out  DISCLOSED  a_shutil_helper_is_invisible
 31  a run-time signal number          COVERED    a_signal_number_that_arrives_at_run_time
 32  ``proc.send_signal(sig)``         COVERED    send_signal_on_a_popen
 33  ``setsid``/``set-``/``getpgid``   COVERED    setsid_and_setpgid
 34  ``os.waitpid``, ``wait3``, ...    COVERED    waitpid
 35  bare ``os.wait()``, any child     COVERED    os_wait_is_reported_and_a_popen_wait_is_not
 36  ``pidfd_send_signal``/``open``    COVERED    pidfd_signalling
 37  ``importlib.import_module(..)``   COVERED    an_import_module_by_string
 38  ``__import__('sub'+'process')``   COVERED    a_dunder_import_with_a_folded_string
 39  ``__import__(''.join(..))``       DISCLOSED  a_module_name_the_interpreter_builds_is_invisible
 40  a ``subprocess.Popen`` SUBCLASS   COVERED    a_popen_subclass
 41  a SECOND spawn or disposal        COVERED    a_second_communicate_is_counted
 42  a streamer disposed on one path   COVERED    a_streamer_with_no_finally_is_reported
 43  ``except TimeoutExpired`` alone   COVERED    a_narrow_handler_is_not_a_broad_disposal
 44  a disposal in a NESTED function   COVERED    a_nested_disposal_does_not_excuse_outer
 45  ``from os import wait``           COVERED    a_from_import_of_a_syscall_is_resolved
 46  ``webbrowser``, ``subprocess_*``  COVERED    an_indirect_spawner_module_is_seen
 47  a ``finally`` on another object   COVERED    a_finally_on_another_object_is_not_a_disposal
 48  ``finish()``/``kill()`` disposal  COVERED    only_close_counts_as_a_streamer_disposal
 49  a disposal BEFORE the build       COVERED    a_disposal_before_the_construction_does_not_count
 50  a streamer bound to no name       COVERED    a_streamer_nobody_binds_a_name_to_is_reported
 51  ``o.wait()`` via a subscript      DISCLOSED  an_os_wait_through_a_computed_receiver
===  ================================  =========  ==================================================

Row 41 is the one layer 1 is blind to by construction, and
``EXPECTED_SPAWNERS`` in the guard file is what carries it.

Row 5 is COVERED here in the sense layer 1 means it - the module lands
in the inventory whatever it calls ``run`` - and NOT in the sense #309
means it. Resolving the rename back to the subprocess name so an untimed
``_run(...)`` is reported is
``tests/test_timeout_enforcement.py::_subprocess_aliases``, and the
guard class docstring says why that rule is deliberately not duplicated
here.

THE TABLE IS ASSERTED, not decorative.
``test_every_named_test_in_the_evasion_table_exists`` parses these rows
and fails on a name that no longer resolves, because a hand-maintained
index above the tests it indexes is exactly the thing that rots on the
next rename - and this repo's rule is that without a mechanism there is
no plan.

Rows 24, 26, 35, 36 and 40 were added by measuring first: over the 127
modules of ``kstrl/`` each of them changes the pinned inventory in
exactly zero places, so none is a widening bought with false positives.
"""

from __future__ import annotations

import ast

from tests import test_process_lifecycle as guard
from tests.helpers.proclifecycle import (
    BARE_SYSCALLS,
    bare_syscall_calls,
    base_disposal_guards,
    callee_names,
    calls_named,
    os_module_names,
    os_syscall_calls,
    own_nodes,
    process_primitive_spellings,
    spelled_strings,
    spelled_tokens,
    undisposed_streamer_sites,
)


def tokens(source: str) -> frozenset[str]:
    """The layer-1 vocabulary one snippet spells."""
    return process_primitive_spellings(ast.parse(source))


def syscalls(source: str) -> list[str]:
    return bare_syscall_calls(ast.parse(source))


def os_calls(source: str) -> list[str]:
    tree = ast.parse(source)
    return os_syscall_calls(tree, os_module_names(tree))


class TestLayerOneSeesEveryWayASpawnerIsNamed:
    """Import spellings, rebinds and indirection: the net, row by row."""

    def test_a_plain_import_and_attribute_call(self) -> None:
        assert tokens("import subprocess\nsubprocess.Popen(['sh'])") >= {"subprocess", "Popen"}

    def test_an_aliased_module_import(self) -> None:
        """``import subprocess as sp``: the ALIAS is what later code
        spells, and the module name survives on the import node."""
        assert tokens("import subprocess as sp\nsp.Popen(['sh'])") >= {"subprocess", "Popen"}

    def test_a_from_import(self) -> None:
        assert tokens("from subprocess import Popen\nPopen(['sh'])") >= {"subprocess", "Popen"}

    def test_a_from_import_renamed(self) -> None:
        """``from subprocess import Popen as P``: nothing downstream
        spells either word, and the import node spells both."""
        assert tokens("from subprocess import Popen as P\nP(['sh'])") >= {"subprocess", "Popen"}

    def test_a_from_import_renamed_for_a_run(self) -> None:
        """The alias hole #326 measured in a neighbouring net: ``from
        subprocess import run as _run`` reported nothing there."""
        assert tokens("from subprocess import run as _run\n_run(['sh'])") >= {"subprocess"}

    def test_a_module_level_rebind(self) -> None:
        assert tokens("import subprocess\nSpawn = subprocess.Popen\nSpawn(['sh'])") >= {"Popen"}

    def test_a_functools_partial(self) -> None:
        source = "import functools, subprocess\ngo = functools.partial(subprocess.Popen, ['sh'])"
        assert tokens(source) >= {"subprocess", "Popen"}

    def test_a_wrapper_function_in_the_same_module(self) -> None:
        """The plainest indirection there is. A wrapper hides the call
        from a reader; it cannot hide the import from the net."""
        source = "import subprocess\n\n\ndef launch(cmd):\n    return subprocess.Popen(cmd)\n"
        assert tokens(source) >= {"subprocess", "Popen"}

    def test_a_name_pulled_out_of_a_dict(self) -> None:
        assert tokens("import subprocess\n{'go': subprocess.Popen}['go'](['sh'])") >= {"Popen"}

    def test_a_name_built_by_dict_zip(self) -> None:
        """One of the seven shapes #336 measured walking past a widened
        node-type matcher."""
        source = "import subprocess\ntable = dict(zip(['go'], [subprocess.Popen]))"
        assert tokens(source) >= {"Popen"}

    def test_a_module_level_constant_tuple(self) -> None:
        """The name reached from a CONSTANT rather than a dict, which is
        its own row in the brief for this sweep."""
        source = "import subprocess\nSPAWNERS = (subprocess.Popen,)\nSPAWNERS[0](['sh'])"
        assert tokens(source) >= {"subprocess", "Popen"}

    def test_a_getattr_with_a_literal_name(self) -> None:
        assert tokens("import subprocess\ngetattr(subprocess, 'Popen')(['sh'])") >= {"Popen"}

    def test_a_parameter_default(self) -> None:
        assert tokens("import subprocess\ndef go(make=subprocess.Popen):\n    return make") >= {
            "Popen"
        }

    def test_a_tuple_unpacked_binding(self) -> None:
        assert tokens("import subprocess\nn, make = 1, subprocess.Popen") >= {"Popen"}

    def test_a_loop_variable(self) -> None:
        source = "import subprocess\nfor make in (subprocess.Popen,):\n    make(['sh'])"
        assert tokens(source) >= {"Popen"}

    def test_a_setattr(self) -> None:
        assert tokens("import subprocess\nsetattr(mod, 'go', subprocess.Popen)") >= {"Popen"}

    def test_a_function_returning_the_bare_name(self) -> None:
        source = "import subprocess\ndef make():\n    return subprocess.Popen"
        assert tokens(source) >= {"Popen"}

    def test_a_class_attribute(self) -> None:
        source = "import subprocess\nclass Runner:\n    make = subprocess.Popen"
        assert tokens(source) >= {"Popen"}

    def test_a_popen_subclass(self) -> None:
        """A subclass inherits every disposal defect of its base and is
        spelled nowhere afterwards: the construction site reads
        ``Runner(cmd)``."""
        source = "import subprocess\nclass Runner(subprocess.Popen):\n    pass\nRunner(['sh'])"
        assert tokens(source) >= {"subprocess", "Popen"}

    def test_an_import_module_by_string(self) -> None:
        source = "import importlib\nm = importlib.import_module('subprocess')"
        assert tokens(source) >= {"subprocess"}

    def test_a_dunder_import_with_a_folded_string(self) -> None:
        assert tokens("m = __import__('sub' + 'process')") >= {"subprocess"}

    def test_a_dotted_module_path(self) -> None:
        """The component split, which is what rows 24 and 26 need.

        ``from multiprocessing.context import Process`` puts only
        ``"multiprocessing.context"`` in the tree, and that string is
        not a vocabulary word. Neither is ``"asyncio.subprocess"``.
        """
        assert tokens("from multiprocessing.context import Process\nProcess(target=f)") >= {
            "multiprocessing",
            "Process",
        }
        assert tokens("import asyncio.subprocess as asp\nasp.Process") >= {"subprocess"}


class TestLayerOneSeesEveryOtherWayToStartAProcess:
    """Not everything that forks is ``subprocess``."""

    def test_os_system(self) -> None:
        assert tokens("import os\nos.system('ls')") >= {"system"}

    def test_os_popen(self) -> None:
        assert tokens("import os\nos.popen('ls')") >= {"popen"}

    def test_os_spawn_family(self) -> None:
        assert tokens("import os\nos.spawnv(os.P_NOWAIT, '/bin/sh', ['sh'])") >= {"spawnv"}

    def test_os_posix_spawn(self) -> None:
        assert tokens("import os\nos.posix_spawn('/bin/sh', ['sh'], {})") >= {"posix_spawn"}
        assert tokens("import os\nos.posix_spawnp('sh', ['sh'], {})") >= {"posix_spawnp"}

    def test_os_fork(self) -> None:
        assert tokens("import os\nif os.fork() == 0:\n    pass") >= {"fork"}
        assert tokens("import os\npid, fd = os.forkpty()") >= {"forkpty"}

    def test_os_exec_family(self) -> None:
        assert tokens("import os\nos.execv('/bin/sh', ['sh'])") >= {"execv"}

    def test_pty_spawn(self) -> None:
        assert tokens("import pty\npty.spawn(['sh'])") >= {"pty", "spawn"}
        assert tokens("import pty\nm, s = pty.openpty()") >= {"pty", "openpty"}

    def test_multiprocessing(self) -> None:
        assert tokens("import multiprocessing\nmultiprocessing.Process(target=f)") >= {
            "multiprocessing",
            "Process",
        }

    def test_asyncio_create_subprocess(self) -> None:
        exec_source = (
            "import asyncio\n\n\nasync def go():\n    await asyncio.create_subprocess_exec('sh')\n"
        )
        shell_source = (
            "import asyncio\n\n\nasync def go():\n    await asyncio.create_subprocess_shell('ls')\n"
        )
        assert tokens(exec_source) >= {"create_subprocess_exec"}
        assert tokens(shell_source) >= {"create_subprocess_shell"}

    def test_a_process_pool_executor(self) -> None:
        source = "from concurrent.futures import ProcessPoolExecutor\nProcessPoolExecutor(2)"
        assert tokens(source) >= {"ProcessPoolExecutor"}

    def test_pexpect(self) -> None:
        """Not a dependency of this tree today, which is the reason to
        enrol the word rather than a reason not to: it is what makes a
        new dependency that spawns visible in the diff that adds it."""
        assert tokens("import pexpect\npexpect.spawn('sh')") >= {"pexpect", "spawn"}

    def test_getoutput_and_getstatusoutput(self) -> None:
        """``subprocess.getoutput`` runs a SHELL and waits, and takes no
        ``timeout`` at all, so it is the one spawn-and-wait name that
        cannot be made to satisfy #309."""
        assert tokens("import subprocess\nsubprocess.getoutput('ls')") >= {"getoutput"}
        assert tokens("import subprocess\nsubprocess.getstatusoutput('ls')") >= {"getstatusoutput"}


class TestLayerOneSeesEveryWayASignalIsSent:
    """The #308/#329 half: signalling, not spawning."""

    def test_os_kill(self) -> None:
        assert tokens("import os\nos.kill(pid, 9)") >= {"kill"}

    def test_os_killpg(self) -> None:
        assert tokens("import os\nos.killpg(pgid, 9)") >= {"killpg"}

    def test_a_renamed_kill_import(self) -> None:
        assert tokens("from os import kill as k\nk(pid, 9)") >= {"kill"}

    def test_a_getattr_kill(self) -> None:
        assert tokens("import os\ngetattr(os, 'kill')(pid, 9)") >= {"kill"}

    def test_a_signal_number_that_arrives_at_run_time(self) -> None:
        """The signal being a variable changes nothing: the guard is
        about the CALL, and a runtime signal number is if anything the
        more dangerous case, because nobody reading the line can see
        which signal it sends."""
        assert tokens("import os\nos.kill(pid, chosen_signal)") >= {"kill"}

    def test_send_signal_on_a_popen(self) -> None:
        assert tokens("proc.send_signal(sig)") >= {"send_signal"}

    def test_setsid_and_setpgid(self) -> None:
        assert tokens("import os\nos.setsid()\nos.setpgid(0, 0)") >= {"setsid", "setpgid"}
        assert tokens("import os\nos.getpgid(pid)\nos.getpgrp()") >= {"getpgid", "getpgrp"}

    def test_waitpid(self) -> None:
        assert tokens("import os\nos.waitpid(pid, 0)") >= {"waitpid"}
        assert tokens("import os\nos.wait3(0)\nos.wait4(pid, 0)") >= {"wait3", "wait4"}
        assert tokens("import os\nos.waitid(a, b, c)") >= {"waitid"}

    def test_pidfd_signalling(self) -> None:
        """Linux's route to a signal that names neither ``kill`` nor
        ``killpg``. Not exported by the interpreter this tree runs on
        (measured: ``hasattr(signal, "pidfd_send_signal")`` is False on
        macOS 3.12.8), and exported by the one CI runs on."""
        source = "import os, signal\nfd = os.pidfd_open(pid)\nsignal.pidfd_send_signal(fd, 9)"
        assert tokens(source) >= {"pidfd_open", "pidfd_send_signal"}


class TestLayerTwoNamesTheLine:
    """The message half. Everything here is already caught by layer 1;
    what these assert is that the operator is told WHERE and WHAT."""

    def test_a_bare_syscall_is_reported_with_its_line(self) -> None:
        hits = syscalls("import os\n\nos.killpg(pgid, 9)\n")
        assert hits == ["line 3: killpg(os.killpg ...)"]

    def test_every_bare_syscall_name_is_matched(self) -> None:
        """No member of the list is there for decoration.

        The count is asserted as well as each member, because iterating
        :data:`BARE_SYSCALLS` to check that :func:`bare_syscall_calls`
        matches every member of :data:`BARE_SYSCALLS` cannot detect a
        DROPPED member: delete ``killpg`` from the frozenset and this
        loop simply runs one fewer time and passes. The floor is what
        makes a deletion visible; it is a floor rather than an equality
        so that adding a syscall stays a one-line change.
        """
        assert len(BARE_SYSCALLS) >= 27, (
            f"BARE_SYSCALLS has shrunk to {len(BARE_SYSCALLS)}. A syscall removed "
            "from the vocabulary is a syscall this guard stops seeing, so the diff "
            "that removes one has to say why and move this floor with it."
        )
        for name in sorted(BARE_SYSCALLS):
            assert syscalls(f"import os\nos.{name}(a)"), name
        for name in ("killpg", "fork", "posix_spawn", "setsid", "subprocess_exec"):
            assert name in BARE_SYSCALLS, name

    def test_a_from_import_of_a_syscall_is_resolved(self) -> None:
        """``from os import wait`` leaves no receiver to resolve.

        MEASURED before the resolver existed: a module doing this gave
        40 passed on the guard file and 4977 passed on the full suite,
        while a positive control spelling ``subprocess`` gave 3
        failures. ``wait`` cannot go in the vocabulary - the bare word
        costs 9 modules of false rows - so the import statement, which
        is the one place the ambiguity is actually removed, is resolved
        instead.
        """
        assert os_calls("from os import wait\nwait()")
        assert os_calls("from os import wait as _w\n_w()")
        assert syscalls("from os import killpg\nkillpg(p, s)")
        assert os_calls("from queue import Queue\nq.wait()") == []

    def test_an_indirect_spawner_module_is_seen(self) -> None:
        """Three words that reach neither ``subprocess`` nor ``os.kill``.

        Measured cost of enrolling all three over the 129 modules of
        ``kstrl/``: zero rows change.
        """
        assert tokens("import webbrowser\nwebbrowser.open(u)") == {"webbrowser"}
        assert syscalls("await loop.subprocess_exec(a)")
        assert syscalls("await loop.subprocess_shell(a)")

    def test_os_kill_is_reported_and_a_popen_kill_is_not(self) -> None:
        """The line that keeps this guard obeyable. ``proc.kill()`` is
        how a ``Popen`` is killed and is legitimate everywhere; only the
        SYSCALL belongs to ``procgroup``. A rule that flagged both would
        be a rule people turn off."""
        assert os_calls("import os\nos.kill(pid, 9)") == ["line 2: os.kill(os.kill ...)"]
        assert os_calls("proc.kill()") == []
        assert os_calls("self.kill()") == []
        assert os_calls("worker.kill()") == []

    def test_os_wait_is_reported_and_a_popen_wait_is_not(self) -> None:
        """``os.wait()`` reaps ANY child of this process, so a second
        caller of it can collect a child ``procgroup`` is still waiting
        on and turn a bounded wait into a permanent one."""
        assert os_calls("import os\nos.wait()") == ["line 2: os.wait(os.wait ...)"]
        assert os_calls("proc.wait(timeout=5)") == []
        assert os_calls("event.wait()") == []

    def test_os_kill_through_an_aliased_module(self) -> None:
        assert os_calls("import os as _os\n_os.kill(pid, 9)")

    def test_os_kill_through_a_module_rebind(self) -> None:
        assert os_calls("import os\n_os = os\n_os.kill(pid, 9)")

    def test_os_kill_through_getattr(self) -> None:
        assert os_calls("import os\ngetattr(os, 'kill')(pid, 9)")

    def test_a_popen_construction_is_counted_however_it_is_spelled(self) -> None:
        assert len(calls_named(ast.parse("import subprocess\nsubprocess.Popen(c)"), "Popen")) == 1
        assert len(calls_named(ast.parse("from subprocess import Popen\nPopen(c)"), "Popen")) == 1
        assert len(calls_named(ast.parse("getattr(subprocess, 'Popen')(c)"), "Popen")) == 1

    def test_a_second_communicate_is_counted(self) -> None:
        """#326's shape: the primary collection plus a hand-rolled
        disposal."""
        source = "try:\n    p.communicate(timeout=t)\nexcept X:\n    p.communicate(timeout=g)\n"
        assert len(calls_named(ast.parse(source), "communicate")) == 2

    def test_callee_names_reduces_the_three_call_shapes(self) -> None:
        assert callee_names(_first_call("f(1)")) == {"f"}
        assert callee_names(_first_call("mod.f(1)")) == {"f"}
        assert callee_names(_first_call("getattr(mod, 'f')(1)")) == {"f"}
        assert callee_names(_first_call("getattr(mod, name)(1)")) == frozenset()

    def test_os_module_names_resolves_the_three_bindings(self) -> None:
        assert os_module_names(ast.parse("import os")) == {"os"}
        assert os_module_names(ast.parse("import os as _os")) == {"os", "_os"}
        assert os_module_names(ast.parse("import os\nq = os")) == {"os", "q"}

    def test_spelled_strings_reads_fields_no_matcher_names(self) -> None:
        """The field walk is the reason layer 1 needs no node-type list.
        Asserted directly so that replacing it with a hand-written list
        of the fields it happens to reach today fails here."""
        node = ast.parse("import subprocess as sp").body[0]
        assert isinstance(node, ast.Import)
        assert set(spelled_strings(node.names[0])) == {"subprocess", "sp"}

    def test_spelled_tokens_adds_the_dotted_components(self) -> None:
        """And nothing else: the whole string is still yielded, so a
        component split can never LOSE a spelling."""
        node = ast.parse("import asyncio.subprocess").body[0]
        assert isinstance(node, ast.Import)
        assert set(spelled_tokens(node.names[0])) == {
            "asyncio.subprocess",
            "asyncio",
            "subprocess",
        }


class TestLayerTwoSeesAnUnattachedDisposal:
    """#326's other half: the disposal exists but not on every path.

    These four controls are the reason the two rules in the guard are
    not prose. Each plants the exact code that was in the tree before
    this PR and asserts the matcher reports it, and each has a negative
    twin so "flag everything" does not pass.
    """

    def test_a_streamer_with_no_finally_is_reported(self) -> None:
        """What all five construction sites looked like."""
        source = (
            "def run(cmd):\n"
            "    streamer = DeadlineStreamer(cmd)\n"
            "    for line in streamer.lines():\n"
            "        yield line\n"
            "    streamer.finish()\n"
        )
        assert undisposed_streamer_sites(ast.parse(source)) == [
            "line 2: run: DeadlineStreamer(DeadlineStreamer ...)"
        ]

    def test_a_streamer_disposed_in_a_finally_is_not(self) -> None:
        source = (
            "def run(cmd):\n"
            "    streamer = DeadlineStreamer(cmd)\n"
            "    try:\n"
            "        for line in streamer.lines():\n"
            "            yield line\n"
            "        streamer.finish()\n"
            "    finally:\n"
            "        streamer.close()\n"
        )
        assert undisposed_streamer_sites(ast.parse(source)) == []

    def test_a_finally_on_another_object_is_not_a_disposal(self) -> None:
        """The receiver is checked, which the first version did not do.

        MEASURED as a live leak: with the name unchecked, replacing
        ``streamer.close()`` with ``_tmp.close()`` left the guard green
        while the child kept running. ``agents/codex.py`` already carries
        a second ``try``/``finally`` in the same scope for its temp file,
        so one edit of that ``unlink()`` to a ``close()`` would have
        switched the rule off for that adapter with nothing else changing.
        """
        source = (
            "def run(cmd):\n"
            "    streamer = DeadlineStreamer(cmd)\n"
            "    try:\n"
            "        yield from streamer.lines()\n"
            "    finally:\n"
            "        _tmp.close()\n"
        )
        assert undisposed_streamer_sites(ast.parse(source))

    def test_only_close_counts_as_a_streamer_disposal(self) -> None:
        """``finish`` and ``kill`` used to satisfy this rule and must not.

        ``finish`` waits ten seconds for a child to leave on its own -
        measured, ``finish()`` on a live child takes 10.0028s - which on
        the abandonment path is exactly the billed spend on a discarded
        answer the rule exists to stop. ``kill`` never calls ``_settle``,
        so it leaves ``_disposed`` False and the streamer in ``_ACTIVE``
        for a later ``kill_active_process_groups`` to signal a corpse.
        """
        template = (
            "def run(cmd):\n"
            "    streamer = DeadlineStreamer(cmd)\n"
            "    try:\n"
            "        yield from streamer.lines()\n"
            "    finally:\n"
            "        streamer.{}()\n"
        )
        assert undisposed_streamer_sites(ast.parse(template.format("finish")))
        assert undisposed_streamer_sites(ast.parse(template.format("kill")))
        assert undisposed_streamer_sites(ast.parse(template.format("close"))) == []

    def test_a_disposal_before_the_construction_does_not_count(self) -> None:
        """A cleanup block that has already finished cannot cover a
        streamer built after it."""
        source = (
            "def run(cmd):\n"
            "    try:\n"
            "        pass\n"
            "    finally:\n"
            "        streamer.close()\n"
            "    streamer = DeadlineStreamer(cmd)\n"
            "    yield from streamer.lines()\n"
        )
        assert undisposed_streamer_sites(ast.parse(source))

    def test_a_streamer_nobody_binds_a_name_to_is_reported(self) -> None:
        """You cannot dispose of what you did not keep."""
        source = "def run(cmd):\n    yield from DeadlineStreamer(cmd).lines()\n"
        assert undisposed_streamer_sites(ast.parse(source))

    def test_a_dotted_receiver_matches_a_dotted_binding(self) -> None:
        """``self._streamer.close()`` disposes of ``self._streamer``."""
        source = (
            "def run(self, cmd):\n"
            "    self._streamer = DeadlineStreamer(cmd)\n"
            "    try:\n"
            "        yield from self._streamer.lines()\n"
            "    finally:\n"
            "        self._streamer.close()\n"
        )
        assert undisposed_streamer_sites(ast.parse(source)) == []

    def test_a_nested_disposal_does_not_excuse_outer(self) -> None:
        """``own_nodes``'s whole job. A ``finally`` written inside a
        nested helper belongs to the helper, and crediting it to the
        function that merely contains it is how a guard passes the code
        it exists to catch."""
        source = (
            "def run(cmd):\n"
            "    streamer = DeadlineStreamer(cmd)\n"
            "\n"
            "    def unrelated():\n"
            "        try:\n"
            "            pass\n"
            "        finally:\n"
            "            other.close()\n"
            "\n"
            "    return streamer\n"
        )
        assert undisposed_streamer_sites(ast.parse(source))

    def test_own_nodes_stops_at_a_nested_scope(self) -> None:
        """Asserted directly, so replacing the walk with ``ast.walk``
        fails here as well as in the test above."""
        tree = ast.parse("def outer():\n    a = 1\n\n    def inner():\n        b = 2\n")
        outer = tree.body[0]
        assert isinstance(outer, ast.FunctionDef)
        named = {n.id for n in own_nodes(outer) if isinstance(n, ast.Name)}
        assert named == {"a"}

    def test_a_narrow_handler_is_not_a_broad_disposal(self) -> None:
        """What ``verify`` and ``serve`` each had: the right disposal
        attached to the wrong set of exits."""
        narrow = (
            "try:\n"
            "    out = p.communicate(timeout=t)\n"
            "except subprocess.TimeoutExpired:\n"
            "    drain_or_abandon(p, g)\n"
            "    raise\n"
        )
        assert base_disposal_guards(ast.parse(narrow)) == 0

    def test_a_broad_disposal_needs_all_three_parts(self) -> None:
        """The type, the call and the bare re-raise. Dropping any one is
        a different way to have the same hole, so each is asserted."""
        whole = (
            "try:\n"
            "    out = p.communicate(timeout=t)\n"
            "except BaseException:\n"
            "    drain_or_abandon(p, g)\n"
            "    raise\n"
        )
        assert base_disposal_guards(ast.parse(whole)) == 1
        assert base_disposal_guards(ast.parse(whole.replace("    raise\n", "    pass\n"))) == 0
        assert (
            base_disposal_guards(ast.parse(whole.replace("drain_or_abandon(p, g)", "log(p)"))) == 0
        )
        assert base_disposal_guards(ast.parse(whole.replace("BaseException", "OSError"))) == 0

    def test_a_bare_except_counts_and_reap_or_abandon_does_too(self) -> None:
        """``except:`` is as broad as ``BaseException``, and the sibling
        disposal is as good as the drain, so neither is a way to write
        the guard out of the rule."""
        source = "try:\n    p.wait(timeout=t)\nexcept:\n    reap_or_abandon(p, g)\n    raise\n"
        assert base_disposal_guards(ast.parse(source)) == 1


class TestTheEvasionTableIsAssertedRatherThanWritten:
    """The module docstring's 44 rows, checked against reality.

    A table of test names sitting above the tests it names is a thing
    that rots silently on the next rename, and a guard file whose own
    index is stale is a guard nobody trusts. This is 20 lines that make
    the index a mechanism.
    """

    @staticmethod
    def _rows() -> list[tuple[str, str, str]]:
        """``(number, verdict, test)`` for every row of the table."""
        lines = (__doc__ or "").splitlines()
        rows = []
        for line in lines:
            parts = line.split("  ")
            cells = [cell.strip() for cell in parts if cell.strip()]
            if len(cells) == 4 and cells[0].isdigit():
                rows.append((cells[0], cells[2], cells[3]))
        return rows

    def test_the_table_still_has_all_of_its_rows(self) -> None:
        """Vacuity control: a parse that stops working must not read as
        a table with nothing wrong in it.

        The NUMBERS are checked, not just the count. A length assertion
        alone passes on a table with two row 7s and no row 8, which is
        exactly what a hand-edited ASCII table drifts into.
        """
        rows = self._rows()
        numbers = [int(number) for number, _, _ in rows]
        assert numbers == list(range(1, 52)), (
            "The evasion table's row numbers are no longer 1..51 in order. A "
            f"duplicate or a gap means a row was lost in an edit. Found: {numbers}"
        )
        verdicts = {verdict for _, verdict, _ in rows}
        assert verdicts == {"COVERED", "DISCLOSED"}, verdicts

    def test_every_named_test_in_the_evasion_table_exists(self) -> None:
        """Every row names a test that exists, here or in the guard file."""
        here = {
            name.removeprefix("test_")
            for cls in (
                TestLayerOneSeesEveryWayASpawnerIsNamed,
                TestLayerOneSeesEveryOtherWayToStartAProcess,
                TestLayerOneSeesEveryWayASignalIsSent,
                TestLayerTwoNamesTheLine,
                TestLayerTwoSeesAnUnattachedDisposal,
            )
            for name in vars(cls)
            if name.startswith("test_")
        }
        there = {
            name.removeprefix("test_")
            for name in vars(guard.TestWhatTheGuardCannotSee)
            if name.startswith("test_")
        }
        known = here | there
        # EXACT, not `startswith` in either direction. The prefix form
        # made rows 3, 4 and 5 mutually satisfiable - `a_from_import`
        # is a prefix of `a_from_import_renamed` - so a deleted control
        # could be covered by a sibling whose name merely started the
        # same way.
        missing = sorted(
            f"row {number}: {test}" for number, _verdict, test in self._rows() if test not in known
        )
        assert missing == [], (
            "The evasion table names tests that do not exist. A renamed or deleted "
            "control leaves the row claiming a shape is covered when nothing checks "
            f"it any more. Rows: {missing}"
        )


def _first_call(source: str) -> ast.Call:
    return next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call))
