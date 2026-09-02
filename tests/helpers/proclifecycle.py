"""The matchers the process-lifecycle guard is built out of.

Split out of ``tests/test_process_lifecycle.py`` for two reasons, and
only one of them is the 800-line ratchet.

The other is #324. Four of the functions below are DOMAIN-NEUTRAL AST
resolution - :func:`callee_names`, :func:`os_module_names`,
:func:`_receiver_is_os` and :func:`own_nodes` - and every one of them is
a hand-rolled copy of something this suite already has. Named, because
"there is prior art somewhere" is not a finding:

* :func:`own_nodes` is the third copy of the innermost-scope walk.
  ``tests/test_toml_readers.py::_own_nodes`` and
  ``tests/test_tui_config_walk.py::own_nodes`` are the other two, and
  the three already DISAGREE - this one stops at a ``ClassDef``, the
  other two do not.
* :func:`os_module_names` reimplements the import half of
  ``tests/test_tui_config_walk.py::collect_bindings`` and none of the
  rebind half of ``tests/test_toml_readers.py::_module_aliases``, which
  runs to a fixed point. So ``_os = os`` written in reverse source order
  resolves there and not here.
* :func:`callee_names` is the sixth spelling of "reduce a ``Call`` to
  its callee name" in ``tests/``.

#324's deliverable is a shared ``tests/helpers/astwalk.py``, and those
four are what it should delete from here. This file is NOT what #324
replaces wholesale, and the earlier claim that it was overstated the
case in the direction that gets a cleanup abandoned when it turns out
to be bigger than advertised: the vocabulary, the disposal matchers and
the spawner rules are about processes, not about ASTs, and they stay
wherever this guard lives.

:func:`process_primitive_spellings` is the one resolver here that #324
does not need to touch either, and for a better reason than domain: it
enumerates no node types at all. It reads every string in every field of
every node and intersects with a vocabulary, so there is no resolution
in it to get wrong. The guard's docstring is where that distinction is
argued.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from tests.test_journal_one_writer import folded_str

#: Words that cannot be spelled without meaning a process. Deliberately
#: NOT ``run``, ``wait``, ``poll`` or ``call``: measured, ``run`` alone
#: appears 195 times in ``kstrl/`` and almost none of them is
#: ``subprocess.run``, and a net that reports 195 rows is a net that
#: gets silenced. Those four are reached instead through the module name
#: - ``subprocess`` is spelled by ``import subprocess``, by ``import
#: subprocess as sp`` and by ``from subprocess import run as _run``
#: alike, so a module that can call any of them is in the inventory
#: whatever it calls them. ``os.wait`` is the one of the four that has no
#: module name in front of it, and :func:`os_syscall_calls` is where it
#: is picked up, by resolving the receiver.
#:
#: ``Process``, ``pidfd_open`` and ``pidfd_send_signal`` are here because
#: they cost nothing: measured over the 127 modules of ``kstrl/``, all
#: three occur zero times outside prose, so enrolling them adds no row
#: today and closes the two routes that reach neither ``subprocess`` nor
#: ``os.kill`` - a worker class imported from somewhere other than the
#: ``multiprocessing`` package root, and Linux's pidfd signalling, which
#: this interpreter does not even export (measured: ``hasattr(signal,
#: "pidfd_send_signal")`` is False on macOS 3.12.8, and True is what CI
#: would see).
PROCESS_VOCABULARY = frozenset(
    {
        # Modules that can start a process.
        "subprocess",
        "multiprocessing",
        "pexpect",
        "pty",
        # Real spawners that reach neither `subprocess` nor `os.kill`.
        # `loop.subprocess_exec` and `loop.subprocess_shell` are the
        # asyncio event-loop spawners, and `webbrowser.open` shells out
        # (measured: `inspect.getsource(webbrowser)` names `subprocess`
        # 16 times, against 0 for the DISCLOSED `shutil`). Measured cost
        # of enrolling all three over the 129 modules of `kstrl/`: zero
        # rows change.
        "webbrowser",
        # The unambiguous primitives, whoever exports them.
        "DeadlineStreamer",
        "Popen",
        "Process",
        "ProcessPoolExecutor",
        "check_call",
        "check_output",
        "communicate",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "getoutput",
        "getpgid",
        "getpgrp",
        "getstatusoutput",
        "kill",
        "killpg",
        "openpty",
        "pidfd_open",
        "pidfd_send_signal",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "send_signal",
        "setpgid",
        "setsid",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
        "terminate",
        "wait3",
        "wait4",
        "waitid",
        "waitpid",
    }
)


def spelled_strings(node: ast.AST) -> Iterator[str]:
    """Every string this ONE node spells, whatever field holds it.

    ENUMERATES NO NODE TYPES, which is the whole design. ``iter_fields``
    reaches ``Name.id``, ``Attribute.attr``, ``alias.name``,
    ``alias.asname``, ``ImportFrom.module``, ``Constant.value``,
    ``FunctionDef.name``, ``ClassDef.name``, ``keyword.arg``, ``arg.arg``
    and ``Global.names`` without any of them being written down here, so
    a shape nobody thought of is covered by the same code as the shapes
    they did.

    ``folded_str`` is added on top for the assembled case: ``"sub" +
    "process"`` is a ``BinOp`` whose fields hold no string at all, and
    CPython does not fold it at parse time.

    WHAT THIS CANNOT SEE, each pinned by a test in
    ``TestWhatTheGuardCannotSee``:

    * A name the interpreter has to BUILD. ``"".join(["sub",
      "process"])``, ``%``-formatting, ``"".join`` over a comprehension,
      a value read from the environment. ``folded_str`` answers None for
      all of them by design - it decides only what it can decide.
    * ``getattr(os, name)`` where ``name`` is a variable rather than a
      literal. Nothing spells the primitive.
    * A spawner received as a PARAMETER and called through it. The
      CALLER has to spell it, so the pair is caught at the caller's
      module and not the callee's, and a helper whose only caller is in
      another package would be invisible.

    All three are the same shape - the string is not in the source - and
    all three cost a determined author more work than importing the
    helper does. That is the bar a static guard can hold.

    A FOURTH MISS, different in kind and so listed apart: a stdlib
    helper that spawns without the caller naming a primitive.
    ``shutil`` is the one the brief for this sweep asked about, and the
    answer is measured rather than argued: on the CPython 3.12.8 this
    tree runs, ``inspect.getsource(shutil)`` contains the string
    ``subprocess`` zero times and imports only ``os``, so no ``shutil``
    call reachable from ``kstrl/`` starts a process. It is therefore NOT
    in the vocabulary, and enrolling it would cost eight rows of pure
    noise (``rmtree``, ``copy2``, ``copyfile``, ``which`` across
    ``workqueue``, ``git``, ``intake_github``, ``statedir``, ``serve``,
    ``retry_plan``, ``factory``). If a future interpreter reintroduces an
    external-tool path there, this guard would not see it;
    ``test_a_shutil_helper_is_invisible`` asserts that miss so the
    paragraph fails with it if somebody enrols the word.
    """
    for _, value in ast.iter_fields(node):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            yield from (item for item in value if isinstance(item, str))
    folded = folded_str(node)
    if folded is not None:
        yield folded


def spelled_tokens(node: ast.AST) -> Iterator[str]:
    """Every string this node spells, plus each dotted component of it.

    ``from multiprocessing.context import Process`` puts
    ``"multiprocessing.context"`` on the ``ImportFrom`` node and nothing
    else anywhere, and ``import asyncio.subprocess as asp`` puts
    ``"asyncio.subprocess"`` on the alias. Neither string is a
    vocabulary word, and both are ordinary ways to reach a spawner. The
    component split is what makes a dotted module path count as its
    package.

    MEASURED, because a widening that costs rows is a widening that gets
    silenced. Over the 129 modules of ``kstrl/`` the split changes the
    inventory in TWO places: ``procdispose.py`` and ``procgroup.py`` each
    gain ``DeadlineStreamer`` from docstrings that write
    ``agents.proc.DeadlineStreamer``. Both rows are pinned in
    ``EXPECTED_PROCESS_MODULES`` with the reason on them rather than
    special-cased out, because an exception list is how a net starts
    having a hand-tuned hole.

    THIS NUMBER HAS NOW BEEN WRONG TWICE, which is the point worth
    keeping. It said "exactly zero places", true when written and false
    by the time the module split landed; it was corrected to "exactly
    ONE" and was already two, because the second module was in the same
    diff. A measured claim in prose does not re-measure itself. The
    pinned rows do, and they are what this paragraph is for - the number
    here is commentary on them, not the mechanism.

    Prose is mostly safe for a reason worth stating: a docstring is ONE
    string, so splitting a sentence on ``.`` yields sentence fragments
    and rarely a bare vocabulary word. ``agents.proc.DeadlineStreamer``
    is the case where it does, and it is a dotted PATH inside prose
    rather than a sentence.
    """
    for spelling in spelled_strings(node):
        yield spelling
        if "." in spelling:
            yield from spelling.split(".")


def process_primitive_spellings(tree: ast.Module) -> frozenset[str]:
    """The process vocabulary one module spells, in any shape at all."""
    return frozenset(
        token
        for node in ast.walk(tree)
        for token in spelled_tokens(node)
        if token in PROCESS_VOCABULARY
    )


# --- layer 2: the message -------------------------------------------------


#: Primitives with no innocent reading as a method call anywhere in this
#: tree, so a bare name is enough to flag them and no receiver has to be
#: resolved. ``kill``, ``wait`` and ``terminate`` are deliberately
#: absent: they are also ``Popen.kill``, ``Popen.wait``,
#: ``threading.Event.wait``, ``DeadlineStreamer.kill`` and a pool
#: worker's ``terminate``, all of which are legitimate. ``os.kill`` and
#: ``os.wait`` are caught by :func:`os_syscall_calls` instead, which does
#: resolve the receiver.
BARE_SYSCALLS = frozenset(
    {
        "subprocess_exec",
        "subprocess_shell",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "getpgid",
        "getpgrp",
        "killpg",
        "pidfd_open",
        "pidfd_send_signal",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "setpgid",
        "setsid",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
        "wait3",
        "wait4",
        "waitid",
        "waitpid",
    }
)


def callee_names(node: ast.Call) -> frozenset[str]:
    """Every name this call could be invoking, as spelled.

    ``f(...)``, ``mod.f(...)`` and ``getattr(mod, "f")(...)`` all reduce
    to ``{"f"}``. A callee this cannot name at all - an element pulled
    out of a list at run time, a name rebound behind a function boundary
    - yields nothing, and layer 1 is what still sees the spelling.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return frozenset({func.attr})
    if isinstance(func, ast.Name):
        return frozenset({func.id})
    if isinstance(func, ast.Call):
        return _getattr_name(func)
    return frozenset()


def _getattr_name(func: ast.Call) -> frozenset[str]:
    """``getattr(x, "kill")`` as a callee, when the attribute is a literal."""
    if not isinstance(func.func, ast.Name) or func.func.id != "getattr":
        return frozenset()
    if len(func.args) < 2:
        return frozenset()
    name = folded_str(func.args[1])
    return frozenset() if name is None else frozenset({name})


def os_module_names(tree: ast.Module) -> frozenset[str]:
    """Names bound to the ``os`` module in this file.

    ``import os``, ``import os as _os`` and a plain rebind ``_os = os``.
    Message quality only: a receiver this cannot resolve leaves
    :func:`os_syscall_calls` silent, and layer 1 still has the ``kill``
    spelling in the module's token set.
    """
    names = {"os"}
    for node in ast.walk(tree):
        names |= _os_binding(node, names)
    return frozenset(names)


def os_direct_names(tree: ast.Module) -> frozenset[str]:
    """Local names bound DIRECTLY to an os syscall, however renamed.

    ``from os import wait`` and ``from os import wait as _w`` leave no
    receiver for :func:`_receiver_is_os` to resolve, so the call is a
    bare ``wait()`` that layer 2 could not see. Layer 1 could not see it
    either, because ``wait`` is deliberately absent from
    :data:`PROCESS_VOCABULARY` - measured, enrolling the bare word costs
    9 modules of false rows, which is a net that gets silenced.

    Measured before this existed: a new module doing ``from os import
    wait`` then ``return wait()`` gave 40 passed on the guard file and
    4977 passed on the full suite, while a positive control spelling
    ``subprocess`` gave 3 failures. ``os.wait()`` reaps ANY child of this
    process, so a second one anywhere can collect a child ``procgroup``
    is still waiting on and turn a bounded wait into a permanent one.

    Resolving the import is the fix rather than widening the vocabulary,
    because the import statement is where the ambiguity is actually
    removed: ``wait`` the token is three different methods, and
    ``from os import wait`` is the one spelling that says which.
    """
    wanted = BARE_SYSCALLS | AMBIGUOUS_OS_CALLS
    return frozenset(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "os"
        for alias in node.names
        if alias.name in wanted
    )


def _os_binding(node: ast.AST, known: set[str]) -> set[str]:
    """The names ONE statement binds to the ``os`` module."""
    if isinstance(node, ast.Import):
        return {a.asname or a.name for a in node.names if a.name == "os"}
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
        if node.value.id in known:
            return {t.id for t in node.targets if isinstance(t, ast.Name)}
    return set()


def bare_syscall_calls(tree: ast.Module) -> list[str]:
    """Every call to a primitive that only ``procgroup`` may make."""
    return [
        _hit(node, name)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for name in sorted(callee_names(node) & BARE_SYSCALLS)
    ]


#: Syscalls whose NAME is also an ordinary method in this tree, so the
#: receiver has to be resolved before one can be reported. ``kill`` is
#: ``Popen.kill`` and ``DeadlineStreamer.kill``; ``wait`` is
#: ``Popen.wait``, ``threading.Event.wait`` and a queue's. ``os.wait()``
#: is here and not merely absent because it reaps ANY child of this
#: process, so a second one anywhere can collect a child ``procgroup``
#: is still waiting on and turn a bounded wait into a permanent one.
#: Measured: zero occurrences in ``kstrl/`` and in ``tests/`` today, so
#: enrolling it costs nothing.
AMBIGUOUS_OS_CALLS = frozenset({"kill", "wait"})


def os_syscall_calls(tree: ast.Module, os_names: frozenset[str]) -> list[str]:
    """``os.kill(...)`` / ``os.wait()``, and their ``getattr`` spellings.

    Separate from :func:`bare_syscall_calls` because these names are
    also ordinary methods. The receiver is what tells a syscall from a
    method, and only here - or, where there is no receiver at all,
    :func:`os_direct_names` resolving the import that removed it.
    """
    direct = os_direct_names(tree)
    hits = [
        _hit(node, f"os.{name}")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if _receiver_is_os(node.func, os_names)
        for name in sorted(callee_names(node) & AMBIGUOUS_OS_CALLS)
    ]
    hits += [
        _hit(node, f"os.{node.func.id}")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if isinstance(node.func, ast.Name)
        if node.func.id in direct
    ]
    return hits


def _receiver_is_os(func: ast.expr, os_names: frozenset[str]) -> bool:
    """Is this callee reached through the ``os`` module?

    The receiver is FLATTENED rather than required to be a bare
    ``ast.Name``, so ``ns.os.kill(pid, 9)`` is seen. The narrow form
    missed it, and ``tests/test_tui_config_walk.py::dotted_name`` had
    been flattening attribute chains for exactly this since it was
    written - #324's case in one line.
    """
    if isinstance(func, ast.Attribute):
        return _dotted_head(func.value) in os_names
    if isinstance(func, ast.Call) and func.args:
        return _dotted_head(func.args[0]) in os_names
    return False


def _dotted_head(node: ast.expr) -> str:
    """The last component of ``a.b.c``, or the name, or ``""``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def calls_named(tree: ast.Module, wanted: str) -> list[ast.Call]:
    """Every call in this module whose callee is spelled ``wanted``."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        if wanted in callee_names(node)
    ]


def _hit(node: ast.Call, name: str) -> str:
    return f"line {node.lineno}: {name}({ast.unparse(node.func)} ...)"


# --- layer 2, second half: where a disposal has to be ATTACHED ------------


#: The harness's own spawner. A module that constructs one owns a child
#: for as long as something keeps reading it, so it is in the layer-1
#: vocabulary and the rule below is its layer-2 message.
STREAMER_NAME = "DeadlineStreamer"

#: What counts as letting go of a streamer in a ``finally``. ONE name,
#: and the two that were here with it are the reason to say why.
#:
#: ``finish`` is the ORDERLY disposal and waits ten seconds for a child
#: to leave on its own: measured, ``finish()`` on a live child takes
#: 10.0028s, which on the abandonment path is exactly the billed spend
#: on a discarded answer that ``DeadlineStreamer.close`` exists to stop.
#: ``kill`` never calls ``_settle``, so it leaves ``_disposed`` False and
#: the streamer in ``_ACTIVE`` for a later ``kill_active_process_groups``
#: to signal a corpse. Both left the guard GREEN on a site doing the
#: thing the guard is for.
STREAMER_DISPOSALS = frozenset({"close"})

#: The disposals a broad clause has to reach for a raw ``Popen``.
POPEN_DISPOSALS = frozenset({"drain_or_abandon", "reap_or_abandon"})


def own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node under ``scope`` that no NESTED scope owns.

    The attribution rule ``tests/test_toml_readers.py`` already uses:
    a helper defined inside a function belongs to the helper, not to the
    function that holds it, or a disposal written in one nested function
    would excuse a construction in another.
    """
    stack: list[ast.AST] = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _function_scopes(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function in the module, nested ones included."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _finally_disposes(node: ast.AST, receiver: str) -> bool:
    """Is this a ``try`` whose ``finally`` calls ``<receiver>.close()``?

    THE RECEIVER IS CHECKED, which the first version did not do. It asked
    only whether some ``try`` in the enclosing scope had some ``finally``
    call whose callee NAME was a disposal, so any unrelated cleanup
    satisfied it. Measured three ways: replacing ``streamer.close()``
    with ``_tmp.close()`` left the guard green while the child was a
    confirmed live leak, and ``agents/codex.py`` already carries a second
    ``try``/``finally`` in the same scope for its temp file, so one edit
    of that ``unlink()`` to a ``close()`` would have switched the rule off
    for that adapter with nothing else changing.

    The receiver is flattened, so ``self._streamer.close()`` matches a
    construction bound to ``self._streamer``.
    """
    if not isinstance(node, ast.Try | ast.TryStar) or not node.finalbody:
        return False
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr in STREAMER_DISPOSALS
        and _dotted_head(inner.func.value) == receiver
        for statement in node.finalbody
        for inner in ast.walk(statement)
    )


def _binding_for(scope: ast.AST, call: ast.Call) -> str:
    """The name ``call``'s result was bound to, or ``""``.

    A construction nobody keeps a name for cannot be disposed of, so the
    empty string is a site the rule reports rather than a case to skip.
    """
    for node in own_nodes(scope):
        if isinstance(node, ast.Assign) and node.value is call and len(node.targets) == 1:
            return _dotted_head(node.targets[0])
        if isinstance(node, ast.AnnAssign) and node.value is call:
            return _dotted_head(node.target)
    return ""


def undisposed_streamer_sites(tree: ast.Module) -> list[str]:
    """A ``DeadlineStreamer`` built with no ``finally`` that closes IT.

    #326's second shape. Every adapter's ``run`` is a GENERATOR, and a
    generator can be abandoned mid-yield: ``decompose`` raises from
    inside its own ``for`` loop over one today. Nothing then called
    ``finish``, so the agent CLI kept running and spending after the
    caller had given up on it, and it was not even collected, because
    the reader thread holds a strong reference the ``WeakSet`` does not.

    A ``finally`` is what CPython guarantees will run there, via the
    ``GeneratorExit`` thrown at the suspended ``yield``, so a ``finally``
    is what this insists on. A disposal on the straight-line path only
    is exactly the state every one of these five sites was in.

    Three things are required and the last two were added after the first
    version was measured passing a live leak: the ``finally`` must call a
    disposal, it must call it ON THE STREAMER, and its ``try`` must not
    start before the construction, so a cleanup block that has already
    run cannot be credited with covering one.
    """
    hits: list[str] = []
    for scope in _function_scopes(tree):
        owned = list(own_nodes(scope))
        hits.extend(
            _hit(node, f"{scope.name}: {STREAMER_NAME}")
            for node in owned
            if isinstance(node, ast.Call)
            if STREAMER_NAME in callee_names(node)
            if not _is_let_go_of(scope, owned, node)
        )
    return hits


def _is_let_go_of(scope: ast.AST, owned: list[ast.AST], call: ast.Call) -> bool:
    """Does a ``try`` in this scope close the streamer THIS call built?"""
    receiver = _binding_for(scope, call)
    if not receiver:
        return False
    return any(
        isinstance(node, ast.Try | ast.TryStar)
        and node.lineno >= call.lineno
        and _finally_disposes(node, receiver)
        for node in owned
    )


def base_disposal_guards(tree: ast.Module) -> int:
    """``except BaseException:`` clauses that dispose of a child and re-raise.

    The #326 rule ``procgroup._read_ps`` states: every exit that is not
    a completed read leaves a child behind, so every one of them goes
    through the same disposal. ``verify`` and ``serve`` each guarded
    their ``communicate`` with ``except TimeoutExpired`` alone, which is
    the same defect one exception type wider.

    All THREE parts are required - the broad type, a disposal call, and
    a bare ``raise`` - because dropping any one of them is a different
    way to have the same hole, and a matcher that checked only the
    handler type would pass a handler that swallowed.
    """
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        if _is_broad(node.type)
        if any(isinstance(inner, ast.Raise) and inner.exc is None for inner in ast.walk(node))
        if any(
            isinstance(inner, ast.Call) and callee_names(inner) & POPEN_DISPOSALS
            for inner in ast.walk(node)
        )
    )


def _is_broad(annotation: ast.expr | None) -> bool:
    """``except:`` and ``except BaseException:`` and nothing narrower.

    Four spellings, not two: bare, the name, ``builtins.BaseException``,
    and a one-element tuple. ``tests/test_toml_readers.py::_handler_names``
    decodes all four and this had two, which is fail-SAFE here - a
    spelling it misses drops the pinned count and fails the test - but
    "it fails in the right direction" is how a matcher stays wrong.
    """
    if annotation is None:
        return True
    if isinstance(annotation, ast.Tuple):
        return any(_is_broad(element) for element in annotation.elts)
    return _dotted_head(annotation) == "BaseException"
