"""A child process is created, signalled and let go of in ONE place.

``kstrl.procgroup`` is that place. Five defects of this class have been
found one at a time, and each one's investigation found the next: #308
(the safe-pgid guard written out three times), #298 (a zombie read as
running), #309 (an unkillable ``ps`` with no bound on it), #326 (a killed
child abandoned with no register and no close, at three sites), #329 (a
caller-supplied pgid signalled with no guard at all). Naming the next
site in a docstring did not stop it: ``kstrl/procgroup.py`` said "THIS
FIXES ONE SITE, NOT THE CLASS" and listed the other three, and they were
still there four PRs later. A rule with no mechanism is not a plan.

TWO LAYERS, because one of them is a net and the other is a message. The
shape is ``tests/test_event_names_have_one_home.py``'s, and so is the
reason: #324 records eleven AST guards in this repo that enumerated node
types and were walked past, two of them holed a SECOND time after being
fixed once. Widening a matcher is necessary and not sufficient.

LAYER 1, :func:`process_primitive_spellings`, records WHICH MODULES
spell a process primitive at all, and which ones each spells. It
enumerates no node types and resolves nothing: it reads every string in
every field of every node, plus the folded value of anything that folds,
and intersects that with a fixed vocabulary. A process cannot be
created, signalled or reaped without naming one of those words
somewhere - ``import subprocess as sp``, ``from subprocess import Popen
as P``, ``getattr(os, "kill")``, ``functools.partial(subprocess.Popen)``,
a name pulled out of a dict, ``importlib.import_module("subprocess")``
and ``__import__("sub" + "process")`` all spell one - so a new module
that touches processes has to appear here first, whatever it does
afterwards.

Pinned as a SET of tokens per module rather than a count, and that is
measured rather than aesthetic: ``subprocess`` alone is spelled 187
times in ``kstrl/`` and 57 of those are in ``git.py``, so a count would
move on any unrelated edit and become something to silence. A token set
moves when a module gains a NEW capability - ``git.py`` reaching for
``Popen``, anything at all reaching for ``killpg`` - which is the event
worth failing on.

LAYER 2 names the offending line and says what to do about it. It is not
redundant: layer 1 can only say "this module's vocabulary moved", which
is the wrong message when the answer is "you killed a child and dropped
it, call ``procgroup.drain_or_abandon``". Layer 1 in turn catches what
layer 2 cannot, which after this PR is every spelling of every name it
resolves - so layer 2's resolvers are message quality, not coverage, and
are allowed to be simple.

Layer 2 also carries the two rules that are about a SECOND site inside
an ALREADY-enrolled module, which is the one thing layer 1 is blind to
by construction: the ``Popen`` construction inventory, and the
``communicate`` count outside ``procgroup``. #326 was exactly that - a
second ``communicate`` in ``verify.py``, in a module that already had
one.

WHAT NEITHER LAYER SEES is disclosed on :func:`spelled_strings` and each
miss is pinned by a test in ``TestWhatTheGuardCannotSee`` that asserts
the miss, so a disclosure fails if it stops being true.

WHICH HALF IS CLOSED BY CONSTRUCTION, AND WHAT #324 IS FOR. This is the
distinction the sweep for #324 turned on and it is worth stating flatly,
because the two halves of this file are not the same kind of thing.

* LAYER 1 IS CLOSED BY CONSTRUCTION, in the sense
  ``EXPECTED_JOURNAL_PATH_SITES`` is: it inventories every module that
  OBTAINS the resource, keyed on the resource's own vocabulary and not
  on a list of node types or a list of resolution failures. A module
  that can start, signal or reap a process has to name one of those
  words somewhere in its source, so it lands in the inventory whatever
  shape the naming takes. The four things it cannot see are the four
  where THE STRING IS NOT IN THE SOURCE AT ALL - a name the interpreter
  builds, ``getattr`` with a computed attribute, a spawner handed in as
  a parameter from another package, and a stdlib helper that spawns
  without the caller naming a primitive (``shutil`` is the measured
  example, and it does not spawn on this interpreter). Each is asserted
  in ``TestWhatTheGuardCannotSee``.
* LAYER 2 IS NOT. Its resolvers - :func:`callee_names`,
  :func:`os_module_names`, :func:`_receiver_is_os` - are the twelfth
  hand-rolled AST matcher in this repo, and #324 records ten instances
  of that class being walked past. They are here for MESSAGE QUALITY
  and nothing else: everything they resolve, layer 1 has already
  counted, so a hole in one of them costs a worse failure message and
  not a missed site. The three rules that layer 1 genuinely cannot
  carry - the ``Popen`` inventory, the ``communicate`` count and the
  ``finally`` rule, all of which are about a SECOND site inside an
  already-enrolled module - are closed over
  :data:`EXPECTED_POPEN_SITES` rather than over the whole tree, which
  is narrower than closed by construction and is said here rather than
  implied.

#324 IS WHAT CLOSES THE SECOND BULLET. Its deliverable is a shared
``tests/helpers/astwalk.py`` so every guard in this suite is built on
one reviewed matcher instead of a dozen bespoke ones. Every matcher this
file uses lives in ``tests/helpers/proclifecycle.py`` under one name for
exactly that reason: when #324 lands, replacing them is a deletion
rather than an archaeology exercise, and layer 1 needs nothing from it.
Deliberately NOT done here: a thirteenth bespoke multi-layer resolver,
or an evasion battery over shapes layer 1 already covers by
construction.

POSIX. Process groups do not exist on Windows, but this file reads
source text and never signals anything, so it runs everywhere.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, lru_cache

from tests.helpers.proclifecycle import (
    POPEN_DISPOSALS,
    bare_syscall_calls,
    base_disposal_guards,
    calls_named,
    os_module_names,
    os_syscall_calls,
    process_primitive_spellings,
    undisposed_streamer_sites,
)
from tests.test_journal_one_writer import label, package_sources, parsed
from tests.test_timeout_enforcement import TestSubprocessTimeoutAudit

#: The modules whose job this is. Every "outside here" rule below means
#: outside BOTH: ``procgroup`` reads a group's liveness and decides
#: whether a signal may be sent, ``procdispose`` lets go of what survives
#: a kill. They are two files because one was 871 lines and the
#: 800-line ratchet is a real bound, and because reading a group and
#: abandoning a child are two jobs; they are one home.
PROCESS_HOME = frozenset({"procgroup.py", "procdispose.py"})


#: Every module in ``kstrl/`` that spells a process primitive, and which
#: ones it spells.
#:
#: Adding a row, or a token to a row, is not forbidden: it is the point.
#: The diff that adds one is where somebody says why new code creates or
#: signals a process for itself instead of calling ``procgroup``.
#:
#: Two rows are the WORD and not the call, and are here because a net
#: that quietly dropped them would be a net with a hand-tuned exception
#: list: ``autonomy.py`` and ``inbox.py`` each spell ``system`` as the
#: default value of an ``actor`` parameter.
EXPECTED_PROCESS_MODULES: dict[str, tuple[str, ...]] = {
    # The home, in two files. `procgroup` reads a group and guards a
    # signal; `procdispose` lets go of what survives. `DeadlineStreamer`
    # is prose in both: the docstrings name the one caller whose disposal
    # is `reap_or_abandon` rather than `drain_or_abandon`, and
    # `spelled_tokens` splits `agents.proc.DeadlineStreamer` on the dot.
    # It is a real token here and this is the ONE place in `kstrl/` where
    # the dotted split changes the inventory - the row is pinned rather
    # than special-cased, because an exception list is how a net starts
    # having a hand-tuned hole.
    "procdispose.py": ("DeadlineStreamer", "Popen", "communicate", "kill", "subprocess"),
    "procgroup.py": (
        "DeadlineStreamer",
        "Popen",
        "communicate",
        "getpgid",
        "getpgrp",
        "kill",
        "killpg",
        "subprocess",
        "terminate",
    ),
    # The four modules that construct a Popen. `killpg` and `kill` are
    # gone from all three non-home ones as of this PR; a token coming
    # back is a hand-rolled copy of a `procgroup` routine.
    "agents/proc.py": ("DeadlineStreamer", "Popen", "kill", "subprocess"),
    "serve.py": ("Popen", "communicate", "subprocess"),
    "verify.py": ("Popen", "communicate", "subprocess"),
    # The five that own a child through `DeadlineStreamer` rather than a
    # raw `Popen`. Four of them are GENERATORS, which is the shape #326's
    # sweep found still open: a consumer walks away mid-yield and nothing
    # disposes. `undisposed_streamer_sites` is the rule; this row is what
    # makes a SIXTH adapter appear in a diff before it can repeat it.
    "agents/claude_code.py": ("DeadlineStreamer",),
    "agents/claude_sdk.py": ("DeadlineStreamer",),
    "agents/custom.py": ("DeadlineStreamer",),
    "agents/liveness.py": ("DeadlineStreamer",),
    # ProcessPoolExecutor workers. `terminate` and `kill` here are
    # `multiprocessing.Process` methods on pool workers the executor
    # owns, not a Popen this module spawned. Left alone by this PR
    # because `kstrl/factory.py` is held by another open PR; the site
    # inventory in that PR body records the verdict.
    "factory.py": ("ProcessPoolExecutor", "kill", "subprocess", "terminate"),
    # `subprocess.run` behind a timeout, and nothing else.
    "agents/codex.py": ("DeadlineStreamer", "subprocess"),
    "breaker.py": ("subprocess",),
    "contract.py": ("subprocess",),
    "fixtures.py": ("subprocess",),
    "git.py": ("subprocess",),
    "intake_github.py": ("subprocess",),
    "licensing.py": ("subprocess",),
    "observability.py": ("subprocess",),
    "pr.py": ("subprocess",),
    "retry_plan.py": ("subprocess",),
    "statedir.py": ("subprocess",),
    "timeout.py": ("subprocess",),
    "tui/screens/home.py": ("subprocess",),
    "tui/screens/retry.py": ("subprocess",),
    # The word, not the call: `actor: str = "system"`.
    "autonomy.py": ("system",),
    "inbox.py": ("system",),
}


@dataclass(frozen=True)
class SpawnerRules:
    """What one module that owns a child is pinned to.

    ONE row per module rather than the four parallel dicts this started
    as, each keyed on the same modules and two of them carrying a comment
    saying they were "keyed on ``EXPECTED_POPEN_SITES``" - prose doing
    what a shared row does structurally. A fifth spawner now cannot be
    added without answering all four questions about it.

    Zeros are WRITTEN OUT, and that is the half a dict-comprehension
    ``if count`` was hiding: a module that stops calling a disposal
    entirely used to vanish from the built dict rather than mismatch a
    row, which is the same failure mode as a switched-off detector.
    """

    #: ``subprocess.Popen(...)`` constructions.
    popen_sites: int
    #: ``communicate`` calls. ONE is the primary collection; a SECOND is
    #: a hand-rolled disposal, which is #326 exactly - ``verify.py`` and
    #: ``serve.py`` each had two, and the second dropped the child with
    #: no register and no close.
    communicate_calls: int
    #: ``except BaseException:`` disposal clauses. ``agents/proc.py`` is
    #: the zero and the reason is structural rather than an exemption:
    #: its ``Popen`` outlives the function that made it, is read by two
    #: threads, and is let go of through ``finish``/``close``/``kill``
    #: from wherever the caller decides, so there is no single ``try``
    #: around its collection for a clause to sit on.
    #: :func:`undisposed_streamer_sites` is the rule that covers it.
    base_disposal_guards: int
    #: Calls to a disposal. This is the rule that makes a disposal
    #: deletable only in a diff that says so. Without it the most obvious
    #: regression of the whole class is invisible: putting
    #: ``DeadlineStreamer.kill``'s third leg back to a bare
    #: ``wait(timeout=...)`` satisfies every other rule here and silently
    #: restores the abandoned child. Measured: that mutation left every
    #: other rule in this file green.
    disposal_calls: int


#: Every module that constructs a ``Popen`` or calls a disposal, and what
#: it is pinned to. A module absent from this table is pinned to zero on
#: all four columns, which is what makes a FIFTH spawner a failure rather
#: than a silent addition.
#:
#: ``serve`` and ``verify`` have two disposal calls each because both the
#: timeout path and the broad clause let go.
EXPECTED_SPAWNERS: dict[str, SpawnerRules] = {
    "procgroup.py": SpawnerRules(1, 1, 1, 1),
    "procdispose.py": SpawnerRules(0, 1, 0, 0),
    "agents/proc.py": SpawnerRules(1, 0, 0, 1),
    "serve.py": SpawnerRules(1, 1, 1, 2),
    "verify.py": SpawnerRules(1, 1, 1, 2),
}


@lru_cache(maxsize=1)
def _module_trees() -> tuple[tuple[str, ast.Module], ...]:
    """Every module in ``kstrl/``, parsed once for the whole file.

    Cached because the seven rules below each want the same 127 trees.
    ``parsed`` already caches the PARSE; what repeated was the I/O around
    it, measured at 4.9ms and 127 ``read_text`` calls per repeat.
    """
    return tuple((label(source), parsed(source)) for source in package_sources())


@cache
def _spellings(tree: ast.Module) -> frozenset[str]:
    """:func:`process_primitive_spellings`, computed once per tree.

    One full pass over the package measured 160ms, 14% of this file's
    wall clock, and two rules want the same answer.
    """
    return process_primitive_spellings(tree)


def _pinned(
    column: str, measure: Callable[[ast.Module], int]
) -> tuple[dict[str, int], dict[str, int]]:
    """``(what the tree does, what the table says)``, differences only.

    Measured over EVERY module, not just the pinned ones, so a sixth
    module that starts constructing a ``Popen`` is a mismatch against an
    implied zero rather than a row nobody wrote. Only the differing keys
    come back, because a 127-row assertion message says nothing.
    """
    measured = {name: measure(tree) for name, tree in _module_trees()}
    expected = {
        name: getattr(EXPECTED_SPAWNERS[name], column) if name in EXPECTED_SPAWNERS else 0
        for name in measured
    }
    differ = sorted(name for name in measured if measured[name] != expected[name])
    return ({n: measured[n] for n in differ}, {n: expected[n] for n in differ})


class TestProcessLifecycleHasOneHome:
    """The net and the message, one assertion each.

    Every assertion is a pinned inventory rather than "this list is
    empty" alone, because an empty list is also what a switched-off
    detector returns. ``tests/test_process_lifecycle_shapes.py`` is what
    makes the empty ones mean something: it plants each shape and fails
    if the matcher stops seeing it.

    TWO RULES OF THIS CLASS ARE DELIBERATELY NOT HERE, and saying so is
    the point rather than a hedge. "every spawn that waits carries a
    deadline" and "every wait inside a spawner carries one" are #309's
    rules, and ``tests/test_timeout_enforcement.py`` has held both since
    #309 round 2:
    ``TestSubprocessTimeoutAudit::test_every_subprocess_call_has_timeout``
    and ``::test_no_allowlisted_module_waits_without_a_deadline``. This
    file briefly carried a second, WEAKER copy of each - the copies read
    only ``not any(kw.arg == "timeout")``, so ``wait(timeout=None)`` and
    ``with Popen(...) as p:`` walked through, which are the two forms
    #309 round 1 recorded as having escaped its own first version, and
    they matched the LOCAL callee name, so ``from subprocess import run
    as _run`` walked through the alias fix too. Two nets on one rule,
    the newer one weaker, is how a rule ends up enforced by whichever
    copy is looked at. They were deleted rather than widened.
    """

    def test_no_new_module_reaches_for_a_process_primitive(self) -> None:
        """Layer 1, the net: pin who can touch a process at all."""
        built = {
            name: tuple(sorted(tokens))
            for name, tree in _module_trees()
            if (tokens := _spellings(tree))
        }
        assert built == EXPECTED_PROCESS_MODULES, (
            "The set of modules that spell a process primitive changed. If this is "
            "new code that creates, signals, waits on or abandons a child, call "
            "kstrl.procgroup instead: safe_pgid and signal_group decide whether a "
            "signal may be sent, signal_process_tree sends it, and drain_or_abandon "
            "or reap_or_abandon lets go of what survives. If the word is not the "
            f"call, add the row with a reason. Found: {built}"
        )

    def test_only_procgroup_makes_a_bare_process_syscall(self) -> None:
        """Layer 2: ``killpg``, ``getpgid``, ``system``, ``fork`` and friends."""
        found = {
            name: hits
            for name, tree in _module_trees()
            if name not in PROCESS_HOME
            if (hits := bare_syscall_calls(tree) + os_syscall_calls(tree, os_module_names(tree)))
        }
        assert found == {}, (
            "A process syscall is issued outside kstrl/procgroup.py. That module is "
            "where the pid and pgid guards live, and #329 is what a copy outside "
            "them costs: killpg(0, sig) broadcasts to the caller's whole group and "
            "killpg to our own group takes the daemon down. Call "
            f"procgroup.signal_group, signal_process_tree or pid_is_alive. Sites: {found}"
        )

    def test_the_spawner_table_names_the_same_modules_the_popen_allowlist_does(
        self,
    ) -> None:
        """The two lists that would otherwise have to move together.

        ``tests/test_timeout_enforcement.py`` has held the four paths
        allowed to construct a ``Popen`` since #309, and this file first
        wrote them out a second time. Two copies of one list is the
        defect this whole PR is about, one directory over. This asserts
        they are the same list instead, so a fifth spawner argued for in
        one place cannot be missing from the other.
        """
        allowlisted = {
            path.removeprefix("kstrl/") for path in TestSubprocessTimeoutAudit.POPEN_ALLOWLIST
        }
        spawners = {name for name, rules in EXPECTED_SPAWNERS.items() if rules.popen_sites}
        assert spawners == allowlisted, (
            "The set of modules that may construct a Popen disagrees between "
            "EXPECTED_SPAWNERS here and POPEN_ALLOWLIST in "
            f"tests/test_timeout_enforcement.py. Found {spawners} vs {allowlisted}"
        )

    def test_a_popen_is_constructed_only_where_it_is_argued_for(self) -> None:
        """Layer 2: the ``Popen`` inventory, which layer 1 cannot see move
        inside a module that already spawns.

        The allowlist next door says WHICH modules; this says how many
        times, which is the part a second ``Popen`` inside an already
        allowlisted module walks through.
        """
        found, pinned = _pinned("popen_sites", lambda tree: len(calls_named(tree, "Popen")))
        assert found == pinned, (
            "The set of places that construct a subprocess.Popen changed. A new one "
            "must say in its diff why it cannot use an existing site, and must "
            "dispose of its child through procdispose.drain_or_abandon (it owns its "
            "pipes) or procdispose.reap_or_abandon (other threads own them). "
            f"Found {found}, pinned {pinned}"
        )

    def test_nobody_outside_the_home_hand_rolls_a_disposal(self) -> None:
        """Layer 2: a SECOND ``communicate`` is an abandonment (#326)."""
        found, pinned = _pinned(
            "communicate_calls", lambda tree: len(calls_named(tree, "communicate"))
        )
        assert found == pinned, (
            "A module collects a child a different number of times than it is "
            "pinned to. A second collection is a disposal, and a hand-rolled one "
            "drops the child without closing its pipes or registering it, so under "
            "PYTHONWARNINGS=error it becomes a permanent zombie (#326). Call "
            f"procdispose.drain_or_abandon. Found {found}, pinned {pinned}"
        )

    def test_every_streamer_is_let_go_of_in_a_finally(self) -> None:
        """Layer 2: #326's second shape, an abandoned generator."""
        found = {
            name: hits
            for name, tree in _module_trees()
            if (hits := undisposed_streamer_sites(tree))
        }
        assert found == {}, (
            "A DeadlineStreamer is constructed in a scope with no `finally` that "
            "lets go of it. Every agent adapter's `run` is a generator and a "
            "generator can be abandoned mid-yield - decompose does it today - so a "
            "disposal on the straight-line path alone leaves the agent CLI running "
            "and spending after the caller gave up on it (#326). Wrap the streaming "
            f"in `try: ... finally: streamer.close()`. Sites: {found}"
        )

    def test_every_popen_module_says_how_it_guards_a_broad_exit(self) -> None:
        """Layer 2: ``except TimeoutExpired`` alone is the same hole."""
        found, pinned = _pinned("base_disposal_guards", base_disposal_guards)
        assert found == pinned, (
            "A module that owns a child changed how it guards a non-timeout exit "
            "from its collection. A timeout is not the only way out of a "
            "communicate: a KeyboardInterrupt or a MemoryError there leaves the "
            "child unsignalled, unreaped, unregistered and holding both pipe ends "
            "(#326). The clause is `except BaseException:`, a call to "
            "procdispose.drain_or_abandon or reap_or_abandon, and a bare `raise`. "
            f"Found {found}, pinned {pinned}"
        )

    def test_every_popen_module_still_calls_a_disposal(self) -> None:
        """Layer 2: a deleted disposal is invisible to every other rule."""
        found, pinned = _pinned(
            "disposal_calls",
            lambda tree: sum(len(calls_named(tree, d)) for d in sorted(POPEN_DISPOSALS)),
        )
        assert found == pinned, (
            "A module that owns a child changed how many times it lets go of one. "
            "Putting a disposal back to a bare bounded wait leaves every other rule "
            f"in this file green and silently restores it (#326). "
            f"Found {found}, pinned {pinned}"
        )


class TestWhatTheGuardCannotSee:
    """The disclosures on :func:`spelled_strings`, each pinned.

    A disclosure with no test behind it decays into a claim. These
    assert the MISS, so the docstring above fails with them if a future
    widening makes one of them false, and whoever widens it gets to
    delete the paragraph as well as the test.
    """

    @staticmethod
    def _tokens(source: str) -> frozenset[str]:
        return process_primitive_spellings(ast.parse(source))

    def test_a_module_name_the_interpreter_builds_is_invisible(self) -> None:
        """``"".join`` and ``%`` are not decidable, so nothing is spelled."""
        assert self._tokens('m = __import__("".join(["sub", "process"]))') == frozenset()
        assert self._tokens('m = __import__("sub%s" % "process")') == frozenset()

    def test_a_concatenation_that_folds_is_NOT_invisible(self) -> None:
        """The control for the test above: the miss is about foldability,
        not about concatenation, and without this the line above would
        pass for the wrong reason."""
        assert self._tokens('m = __import__("sub" + "process")') == frozenset({"subprocess"})

    def test_getattr_with_a_computed_attribute_is_invisible(self) -> None:
        """``getattr(os, name)`` spells no primitive."""
        assert self._tokens("name = choose()\ngetattr(os, name)(pid, 9)") == frozenset()

    def test_getattr_with_a_literal_attribute_is_NOT_invisible(self) -> None:
        """The control: the miss is the variable, not ``getattr``."""
        assert self._tokens('getattr(os, "kill")(pid, 9)') == frozenset({"kill"})

    def test_a_spawner_reached_through_a_parameter_is_invisible(self) -> None:
        """The callee module spells nothing; the caller has to.

        The parameter is called ``launch`` and not ``spawn`` on purpose:
        ``spawn`` is itself in the vocabulary, for ``pty.spawn``, so
        naming it that would make this test pass on the word rather than
        on the shape it is about.
        """
        callee = "def go(launch):\n    return launch(['sh', '-c', 'x'])\n"
        assert self._tokens(callee) == frozenset()
        caller = "import subprocess\ngo(subprocess.Popen)\n"
        assert self._tokens(caller) == frozenset({"subprocess", "Popen"})

    def test_a_shutil_helper_is_invisible(self) -> None:
        """``shutil`` is not in the vocabulary, and the reason is a
        measurement rather than a judgement.

        On the CPython 3.12.8 this tree runs,
        ``inspect.getsource(shutil)`` contains ``subprocess`` zero times
        and imports only ``os``, so no ``shutil`` call reachable from
        ``kstrl/`` starts a process. Enrolling the word would put eight
        modules in the inventory for ``rmtree``, ``copy2``, ``copyfile``
        and ``which``, none of which spawns.

        WHAT THAT COSTS, which is what this test pins: if a future
        interpreter reintroduces an external-tool path there, this net
        does not see it. Whoever enrols the word deletes this test and
        the paragraph on ``spelled_strings`` together.
        """
        assert self._tokens("import shutil\nshutil.which('git')") == frozenset()
        assert self._tokens("import shutil\nshutil.rmtree(p)") == frozenset()
