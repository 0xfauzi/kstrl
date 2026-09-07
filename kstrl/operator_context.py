"""Operator-authored context files read into the engineer's prompt (R10.8).

One loader for the files an operator writes by hand and the factory
reads verbatim. There are two, declared as :data:`OPERATOR_FILES`:
``scripts/kstrl/golden-patterns.md``, what a good change looks like in
this repository, stated before the run rather than distilled after it;
and ``scripts/kstrl/memory.md`` (R10.9), the operator's standing
corrections, read AFTER the retry context so an entry in it changes how
this attempt's failures are acted on rather than being framed by them.
The two differ only by their :class:`OperatorFileKind` row.

TRUST. These files are trusted the way ``CLAUDE.md`` is trusted, which
``run_loop`` prepends verbatim (``kstrl/loop.py``). They are NOT passed
through the knowledge layer's injection filter
(``knowledge._is_injection_attempt``), and that is deliberate: the filter
exists because distilled facts are LLM output that a prior component's
agent could have influenced, whereas the operator authored this file.
Filtering it would mean the harness silently dropping instructions its
own operator wrote.

That trust is only as good as the file the loader actually opens, which
is why :func:`operator_file_spec` is the ONE place a path is
resolved, against the REPO ROOT and never against a component worktree.
Review round 1 (S3) is the reason: the worktree is the tree the engineer
has just been writing to, so reading the operator's file from there let
one component's agent choose what the next component's agent is told,
with no filter and a header asserting the operator wrote it.

The residual, stated rather than implied: three shapes hand the agent
the repo root as its working tree, and in all three an agent there can
edit this file. ``ks run`` forces ``use_worktrees=False``
(``kstrl/cli.py``); so does ``ks factory --no-worktrees``; so does
``[factory] use_worktrees = false`` in ``kstrl.toml``. The last two are
the ones worth naming, because they are the MULTI-COMPONENT case:
``run_factory`` hands ``root_dir`` to every component as its worktree,
so component A's agent can write ``scripts/kstrl/golden-patterns.md`` in
the root and components B and C read it, unfiltered, under a header
saying the operator authored it. That applies to EVERY row, memory.md
included. Nothing in the loader can prevent it, and the bound is the
same one in every case and no stronger: the edit is an ordinary
working-tree change the operator sees in ``git diff`` and in the run's
own diff-scope check, rather than a change made inside a throwaway
worktree that is deleted before anyone looks at it.

memory.md raises the stake once #231 lands, because the daemon becomes a
SECOND writer of it: a ``/memory`` comment on a pull request appends a
line the next run reads. The bound above is what that rests on, so the
file stays a version-controlled file in the operator's own tree, written
where they can see the diff, rather than a store kstrl keeps out of
sight.

FORGERY. The block's delimiter lines carry a per-build random token from
``kstrl.delimiters`` (S4). A fixed marker is forgeable by the very
content it wraps: measured in review round 1, a file containing the line
``=== END GOLDEN PATTERNS (operator-authored) ===`` produced a block
with two closing delimiters and content sitting outside the first one,
where the engineer reads it as harness-level text. That happens whether
the operator is malicious or merely documenting the format in their own
notes. The neighbouring fixed markers (feedforward, retry context,
CLAUDE.md) wrap harness-COMPUTED text; this one wraps a file.

H3a. The delimiters and the truncation FACT in this module are label
glue, not instruction text: they name a block so the engineer can tell
where the operator's words start and stop, and they address no role.
Issue #303 records label glue as outside the enrolled-prompt set, with
the same treatment already given to the feedforward markers
(``=== CODEBASE CONTEXT (auto-generated) ===``), the retry-context
markers (``=== PREVIOUS ATTEMPT CONTEXT ===``) and the CLAUDE.md heading
in ``loop.py``. Nothing here is bound to a name ending in the enrolled
suffix, and nothing here is a sentence addressed to the engineer. Adding
a sentence that tells the engineer what to DO with the block would make
it a prompt body and would put it under H3, which is exactly the test the
round-1 notice failed: it rendered ``shorten <absolute path>`` inside the
delimiters, an imperative addressed to the prompt's reader naming a file
that reader can write to. :class:`OperatorText` now carries the fact and
the remedy as two fields, sharing their numbers by construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kstrl.config import KstrlConfig, relative_to_root
from kstrl.delimiters import generate_data_delimiter
from kstrl.init_cmd import shipped_label

logger = logging.getLogger(__name__)

#: The fraction of the budget a truncating cut must still deliver. See
#: :func:`read_operator_file` for what it is defending against.
CUT_FLOOR = 0.9

#: What the cut kept, said the same way to both audiences, AND the whole
#: vocabulary of ``keep``. ONE string per direction and ONE lookup of it,
#: beside the one ``shown`` string both notices carry the numbers in, so
#: the prompt's ``fact`` and the operator's ``message`` can disagree
#: about neither the amount nor the end. The operator needs the
#: direction to prune correctly: told only "shorten it", somebody
#: trimming a memory file from the bottom deletes exactly the entries the
#: cut was already keeping.
#:
#: The direction sits LAST in both, after the filename in ``fact``.
#: Written between the count and the filename it produced "dropping the
#: start from memory.md", where the closing phrase reads as part of the
#: direction rather than as the file the count is about.
#:
#: It is defined above the rows because :func:`_validate_cut_policy`
#: reads it while the rows are being CONSTRUCTED. That is round 2's nit
#: 6: ``_cut`` branched on ``keep == "tail"`` and fell through to head
#: for anything else while this lookup raised ``KeyError``, so
#: ``replace(spec, keep="middle")`` truncated one way and then died
#: naming the other. One vocabulary, checked where the row is built, is
#: what stops the two disagreeing.
_KEPT: dict[str, str] = {
    "head": "keeping the start of the file and dropping the end",
    "tail": "keeping the end of the file and dropping the start",
}


def _validate_cut_policy(keep: str, max_chars: int, where: str) -> None:
    """Refuse a row whose cut policy the cut cannot honour.

    Both dataclasses below call this from ``__post_init__``, so a value
    ``Literal`` describes but does not enforce - ``dataclasses.replace``,
    an untyped caller, a value read out of a config file some day - fails
    where it is written rather than deeper in, and fails the same way for
    both of them.

    ``max_chars`` must be at least 1. Round 2's nit 7: ``rendered[-0:]``
    is the WHOLE string, so a tail row with a zero budget injected 587 of
    600 characters where the head row injected none, and the value that
    reads as "inject nothing" produced the maximum of what the budget
    exists to bound. Refusing it at construction is narrower than
    ``max(1, ...)`` inside the cut, which would have made a zero mean
    "one character" silently.
    """
    if keep not in _KEPT:
        raise ValueError(
            f"{where}: keep={keep!r} is not one of {sorted(_KEPT)}. The cut and the "
            "notice both read this value, and a third value truncates one way while "
            "the notice for it does not exist."
        )
    if max_chars < 1:
        raise ValueError(
            f"{where}: max_chars={max_chars} is not a budget. A non-positive budget "
            "reads as 'inject nothing' and delivers the whole file, because "
            "rendered[-0:] is rendered."
        )


@dataclass(frozen=True)
class OperatorFileKind:
    """One operator-authored file, declared once and read in two places.

    Everything that differs between two such files sits on this row, and
    it reaches two of the three surfaces with no further edit: the
    parent's once-per-run notice (:func:`operator_file_notices`) and
    ``KstrlConfig.validate`` (:func:`configured_path_errors`) both walk
    :data:`OPERATOR_FILES` through :func:`_rows` and take the subject,
    the ``[paths]`` key and the budget off the row.

    THE WORKER'S PROMPT BLOCK IS THE THIRD AND IT IS HAND-ORDERED, on
    purpose. ``factory._run_component`` names each kind itself, and a new
    row costs SIX hand edits in ``kstrl/factory.py``, which is the list
    and not the number: the import of the kind constant, the
    ``_run_component`` parameter carrying the configured path, the
    ``load_operator_file`` call, THE ENTRY IN THE ``parts`` TUPLE that
    puts the block in front of the engineer, the
    ``_path_relative_to_root`` hoist in the parent, and the positional
    slot in ``_submit_args``. Round 1 of R10.9's review is the reason
    this paragraph exists: the earlier wording claimed a row reached the
    worker too, which would have made a third row validated, warned
    about, and injected into no prompt at all, with nothing failing.
    That is not a defect to loop away, because the ORDER is the feature
    (memory after the retry context, #230) and a loop over a declaration
    order cannot express a prompt order. It is a defect to CLOSE, and
    ``tests/test_operator_files_reach_the_prompt.py`` is the closure.

    ROUND 2 IS WHY THE ``parts`` ENTRY IS NAMED HERE. The first closure
    counted ``load_operator_file(operator_file_spec(<KIND>, ...))`` sites
    and called that proof of delivery, and this paragraph listed the
    other five edits without it. A row that pays those five is loaded and
    put in no prompt, which the reviewer planted and measured green on
    every gate once one ordinary use kept ruff's F841 quiet. So the guard
    reads the ``parts`` tuple as well: a declared row that is not an
    element of it fails ``test_every_declared_row_reaches_the_prompt_
    order``, naming the entry that is missing.
    """

    #: Its ``[paths]`` key, matching a ``config_keys.STRING_KEYS`` row.
    key: str
    #: The ``KstrlConfig`` field holding its path, read with ``getattr``.
    #: ``tests/test_operator_context.py`` ties every row's field to a real
    #: path field of the dataclass, because ``getattr`` on a typo raises
    #: at run time in the parent rather than at import.
    field: str
    #: The label the block carries in the engineer's prompt.
    header: str
    #: What the operator's terminal calls the file.
    subject: str
    #: Character budget. The feedforward convention is tokens times four
    #: (``FeedforwardConfig.max_context_tokens`` is spent as ``* 4`` in
    #: ``build_feedforward_context``).
    max_chars: int
    #: Which END of an over-budget file survives the cut. On the ROW
    #: because the two files grow at opposite ends, and review round 1
    #: (should-fix 2) measured what one shared direction costs: with 400
    #: appended rules at a 4000-character budget the memory block
    #: delivered rules 0000 to 0302 and dropped 0303 to 0399, so the 97
    #: newest standing corrections were the ones that reached no prompt.
    #: ``"head"`` for golden patterns, which an operator writes once and
    #: prunes by hand and whose sections are order-neutral; ``"tail"``
    #: for memory, which ``DEFAULT_MEMORY``, ``docs/runbook.md`` and
    #: #231's ``/memory`` append all grow at the END.
    keep: Literal["head", "tail"]
    #: The scaffolded filename whose digest history says "kstrl wrote
    #: this, the operator has not filled it in yet". Matches a
    #: ``SCAFFOLDED_TEMPLATES`` row in ``kstrl/init_cmd.py``.
    scaffold: str

    def __post_init__(self) -> None:
        """A row this module's own cut cannot honour is refused here.

        At import, since the two rows below are module constants: a bad
        one fails the process that declares it rather than the run that
        reads it. See :func:`_validate_cut_policy`.
        """
        _validate_cut_policy(self.keep, self.max_chars, f"OperatorFileKind {self.key!r}")


#: R10.8. 6000 characters is about 1500 tokens.
GOLDEN_PATTERNS = OperatorFileKind(
    key="golden_patterns",
    field="golden_patterns_file",
    header="GOLDEN PATTERNS (operator-authored)",
    subject="Golden patterns",
    max_chars=6000,
    keep="head",
    scaffold="golden-patterns.md",
)

#: R10.9, about 1000 tokens. A budget of its OWN and not a share of one:
#: the two files have different jobs and different growth rates. Golden
#: patterns is written once and pruned by hand, while #231 makes the
#: daemon a writer of this one, and under a shared budget a file that
#: grows by machine would starve a file that does not, silently.
MEMORY = OperatorFileKind(
    key="memory",
    field="memory_file",
    header="MEMORY (standing feedback)",
    subject="Memory",
    max_chars=4000,
    keep="tail",
    scaffold="memory.md",
)

#: Declaration order, which is what :func:`_rows` walks. NOT the prompt
#: order: ``factory._run_component`` owns that and pins it as one literal
#: tuple, because where memory sits relative to the retry context is the
#: mechanism R10.9 is (the operator's standing correction is read after
#: the controller's output for this attempt, not before it).
OPERATOR_FILES: tuple[OperatorFileKind, ...] = (GOLDEN_PATTERNS, MEMORY)


@dataclass(frozen=True)
class OperatorFile:
    """One operator-authored file and how it enters the prompt.

    Built by :func:`operator_file_spec` and by nothing else, so the
    path, the label the prompt sees and the name the terminal uses are
    decided once for every reader of the file. Every field but ``path``
    and ``display`` is copied off an :class:`OperatorFileKind`, which is
    where a second file is declared.
    """

    #: Absolute, resolved against the repo root.
    path: Path
    #: How the file is named in text the ENGINEER reads: root-relative,
    #: so the prompt does not carry an absolute path.
    display: str
    header: str
    #: How the file is named on the OPERATOR's terminal.
    subject: str
    #: Its ``[paths]`` key, for the message about a value naming nothing.
    key: str
    max_chars: int
    #: Which end of an over-budget file survives. Copied off the row; see
    #: :attr:`OperatorFileKind.keep` for why it is per file.
    keep: Literal["head", "tail"]
    #: The ``SCAFFOLDED_TEMPLATES`` filename this file is scaffolded
    #: from, when it is scaffolded at all. A body whose digest is in that
    #: template's history is an untouched skeleton and is treated as an
    #: empty file: see :func:`read_operator_file`.
    scaffold: str | None = None

    def __post_init__(self) -> None:
        """The same refusal the row makes, made again on the spec.

        Not redundant: ``dataclasses.replace`` builds one of these from a
        valid row with a field overridden, which is how
        ``tests/helpers/operatorfiles.py`` varies the budget, and it is
        the shape review round 2 measured both of nit 6 and nit 7
        through. Re-running the check here is what makes a derived spec
        obey the same rule as the row it came from.
        """
        _validate_cut_policy(self.keep, self.max_chars, f"OperatorFile {self.display!r}")


@dataclass(frozen=True)
class OperatorText:
    """What one operator file amounts to on one read.

    ``body`` is "" whenever there is nothing to inject. The two notice
    fields are the same measurement said to two audiences and are built
    from one string, so the engineer and the operator cannot be told
    different numbers:

    - ``fact`` goes INSIDE the prompt block. It states what happened and
      names the file relatively. It addresses nobody and asks for
      nothing, which is what keeps it out of H3 (see the module
      docstring).
    - ``message`` goes to the operator's terminal. It carries the
      absolute path and the remedy, because the operator is the one who
      can act on it.

    ``absent`` says the file is not there at all, as opposed to being
    there and unreadable. It is read off the SAME guarded read the body
    comes from, so nothing else has to stat the path.
    """

    body: str
    fact: str | None
    message: str | None
    absent: bool


def operator_file_spec(kind: OperatorFileKind, root: Path, configured: Path | str) -> OperatorFile:
    """The ONE resolution of an operator file's path, for every kind.

    Both the parent's once-per-run notice and every worker's prompt block
    come through here, so there is one answer to "which file is this".
    Review round 2 measured what a second answer costs: the parent read
    ``base_config.golden_patterns_file`` raw while the worker read
    ``root_dir / rel``, and ``KstrlConfig`` field defaults are RELATIVE
    until ``anchored`` runs (the SDK, an embedder, most of this suite
    never anchor). A 15000-character file at the repo root reached the
    engineer truncated to 6000 characters while the parent, stat'ing the
    same relative path against the process CWD, found nothing and said
    nothing.

    One function for every kind rather than one per file, so a second
    file inherits that resolution instead of restating it. Building an
    :class:`OperatorFile` literal anywhere else is how the parent and the
    worker came to read different files in the first place.

    An absolute ``configured`` is taken as it stands, which is what
    ``relative_to_root``'s fallback and an absolute ``[paths]`` value
    both produce.
    """
    path = Path(configured)
    resolved = path if path.is_absolute() else root / path
    return OperatorFile(
        path=resolved,
        display=relative_to_root(resolved, root),
        header=kind.header,
        subject=kind.subject,
        key=kind.key,
        max_chars=kind.max_chars,
        keep=kind.keep,
        scaffold=kind.scaffold,
    )


def read_operator_file(spec: OperatorFile) -> OperatorText:
    """Read one operator file: what to inject, and what to say about it.

    NOTHING ELSE ON THIS PATH TOUCHES THE FILESYSTEM. The read is the
    only I/O, it is inside the guard, and it cannot raise. Review round 2
    blocked on the shape this replaces: an ``exists()`` pre-check outside
    the guard. ``Path.exists`` does not swallow every ``OSError`` -
    CPython's ``pathlib._ignore_error`` ignores ENOENT, ENOTDIR, EBADF
    and ELOOP and re-raises the rest - so a golden-patterns file under a
    mode-000 parent directory (EACCES) or with a 400-character name
    (ENAMETOOLONG) killed the run with a traceback before any component
    started, which on ``ks factory`` is after decompose has been paid
    for. Measured both ways through a real ``ks init`` plus ``ks run``:
    exit 1, and the agent never ran. ``init_cmd._read_text_or_none`` is
    the shape CLAUDE.md names as the worked example, and it is this one:
    catch ``OSError`` and ``ValueError`` (``UnicodeDecodeError`` is a
    ``ValueError`` and escapes a fail-closed ``except OSError``), never
    raise.

    "" body, no notice, for the four ordinary states: the file is
    absent, empty, whitespace-only, or unchanged since ``ks init``
    scaffolded it. The last is the one review round 1 measured: an
    untouched skeleton is the operator saying nothing yet, and injecting
    it put 479 characters of angle-bracket placeholders at the head of
    every engineer prompt of every component of every ATTEMPT, under a
    header asserting the operator authored them. Attempt, not iteration:
    ``loop.run_loop`` builds the prompt once and reuses it for every
    iteration of that attempt (round 1, nit 11), which is also the
    latency #231 inherits, one attempt rather than one iteration.
    ``init_cmd.shipped_label`` owns the digest history, so this decision
    and the staleness notice ``ks init`` prints read one table. The
    digest is taken on the raw decoded text and AGAIN on that text with
    its trailing newlines collapsed to one, and this reader asks for the
    second try (``ignore_trailing_newlines=True``). The CRLF case is not
    either of those: ``Path.read_text`` applies universal newlines before
    anything is digested, which is what
    ``test_a_crlf_copy_of_the_scaffold_is_still_recognised`` measures.
    R10.9 round 1 (nit 3) is why the second try exists: digesting the raw
    text while rendering ``rstrip("\\n")`` meant ``DEFAULT_MEMORY + "\\n"``
    injected a placeholder block whose body was byte-identical to the one
    the unedited file suppresses, and #231 makes a daemon the writer of
    this file, where an editor normalising a trailing newline is an
    ordinary thing to happen. A leading newline or any interior edit is
    still a change, because the strip is at the end only.

    ``ks init --upgrade-prompts`` asks the SAME function for the RAW
    digest and gets a different answer, on purpose: that path replaces
    the operator's bytes, so it may only act where nothing of theirs can
    be in the file (round 2, should-fix 2). Two readers, two rules, one
    table, each rule stated where it applies. Injecting nothing is
    recoverable by editing the file; overwriting it is not.

    An unreadable file (a directory in its place, mode 000 on the file or
    on its parent, a name the filesystem will not take, bytes that are
    not UTF-8) returns "" and a ``message``: a bad operator file must not
    fail a run, but it must not be silent either.

    Past ``spec.max_chars`` the text is cut at a line boundary inside the
    budget window, but ONLY when that boundary still delivers
    ``CUT_FLOOR`` of the budget; otherwise the cut is at the budget
    boundary. Review round 2 measured the version without the floor:
    ordinary markdown written without hard wrapping is one long line per
    paragraph, and ``"# Golden patterns\\n" + "word " * 3000`` delivered
    17 of the 6000 budgeted characters. The engineer got a heading. WHICH
    END survives is ``spec.keep`` and belongs to the file, not to the
    cut: see :func:`_cut`. Both the prompt's ``fact`` and the operator's
    ``message`` name that end, out of one string.

    The character counts are the RENDERED body's, not the pre-strip
    window's (review round 1, nit 9), and the budget is compared against
    the rendered text too (review round 2, nit 10): a file of exactly
    ``max_chars`` characters plus one trailing newline used to announce
    "100 of 101 characters shown" over a body that had lost nothing.
    """
    try:
        text = spec.path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        # The two errnos that mean "there is no file here". Narrower than
        # the clause below ON PURPOSE and therefore FIRST: absence is the
        # ordinary state of an optional file and is silent, while every
        # other failure is something the operator has to hear about.
        return OperatorText("", None, None, absent=True)
    except (OSError, ValueError) as exc:
        return OperatorText("", None, f"could not read {spec.path}: {exc}", absent=False)
    if not text.strip():
        return OperatorText("", None, None, absent=False)
    if spec.scaffold is not None and (
        shipped_label(spec.scaffold, text, ignore_trailing_newlines=True) is not None
    ):
        return OperatorText("", None, None, absent=False)

    rendered = text.rstrip("\n")
    if len(rendered) <= spec.max_chars:
        return OperatorText(rendered, None, None, absent=False)

    body = _cut(rendered, spec)
    shown = f"truncated: {len(body)} of {len(text)} characters shown"
    kept = _KEPT[spec.keep]
    return OperatorText(
        body,
        f"{shown} from {spec.display}, {kept}",
        f"{shown}, {kept}; shorten {spec.path}",
        absent=False,
    )


def _cut(rendered: str, spec: OperatorFile) -> str:
    """The part of an over-budget file this kind keeps.

    The direction is ``spec.keep`` and it is a per-file property, not a
    property of truncation: review round 1 (should-fix 2) measured one
    shared "keep the head" against the file R10.9 documents in four
    places as growing at the END, and 97 of 400 appended rules reached no
    engineer prompt, the 97 newest ones. Both directions move the cut to
    a line boundary and both refuse that move below :data:`CUT_FLOOR` of
    the budget, for the reason ``read_operator_file`` records: unwrapped
    markdown is one long line per paragraph, and a line-boundary cut with
    no floor delivered 17 of 6000 characters.

    The ``strip`` at each end is the mirror of the other: it takes back
    the blank-line run the cut lands in, which is at most a few
    characters and never content.

    A WINDOW THAT IS ALREADY WHOLE LINES IS KEPT WHOLE, at either end.
    Round 2's nit 5 found this on the tail arm: the move ran
    unconditionally, so a window whose first character follows a newline
    gave up its own first complete line for nothing, measured at 3969 of
    4000 characters with that line absent from the body. The head arm has
    the same defect mirrored, and it is fixed here in the same change: a
    window whose last character is followed by a newline ends on a
    complete line, and ``rfind`` moved back past it. Neither is caught by
    the floor, because both give up ONE line rather than most of the
    budget. What the two probes read is the character OUTSIDE the window,
    which is the only one that says whether the boundary is already
    there.

    ``spec.max_chars`` is at least 1 (``_validate_cut_policy``) and
    ``rendered`` is longer than it (the only caller checks), so both
    probes are in range.
    """
    if spec.keep == "tail":
        window = rendered[-spec.max_chars :]
        if rendered[-spec.max_chars - 1] == "\n":
            return window.lstrip("\n")
        newline = window.find("\n")
        moved = window[newline + 1 :] if newline >= 0 else window
        return (moved if len(moved) >= int(spec.max_chars * CUT_FLOOR) else window).lstrip("\n")
    window = rendered[: spec.max_chars]
    if rendered[spec.max_chars] == "\n":
        return window.rstrip("\n")
    newline = window.rfind("\n")
    return (window[:newline] if newline >= int(spec.max_chars * CUT_FLOOR) else window).rstrip("\n")


def _rows(
    config: KstrlConfig,
    anchored: KstrlConfig,
    root: Path,
) -> tuple[tuple[OperatorFile, Path], ...]:
    """Every operator-authored file, paired with its anchored default.

    One loop over :data:`OPERATOR_FILES`, so a row added there reaches
    the two surfaces below this line with no edit: the parent's notice
    (:func:`operator_file_notices`) and ``KstrlConfig.validate``
    (:func:`configured_path_errors`). Both take the subject and the
    ``[paths]`` key off the row rather than spelling either one, and
    R10.9 added the memory row without changing either function.

    IT DOES NOT REACH THE ENGINEER'S PROMPT. That is
    ``factory._run_component``, which names each kind by hand and costs
    SIX edits per row, one of them the entry in the ``parts`` tuple that
    is the prompt order; :class:`OperatorFileKind` lists all six and
    names the test that refuses a row which has not paid them. An earlier
    wording of this sentence claimed the worker too, which is a guard
    closed over one surface reading as closed over all of them, and the
    wording after that listed five of the six edits, leaving out the only
    one that puts the block in front of the engineer.

    Both paths come out of ``getattr(..., kind.field)`` and no branch
    names a field. That is what makes pairing one row with another row's
    field impossible here rather than merely unlikely: it is the
    one-field-over defect #260 round 2 and #229's P5 plant both paid
    for, and the LOOP is the thing that prevents it.
    """
    return tuple(
        (
            operator_file_spec(kind, root, getattr(config, kind.field)),
            getattr(anchored, kind.field),
        )
        for kind in OPERATOR_FILES
    )


def _missing_message(spec: OperatorFile, anchored_default: Path, absent: bool) -> str | None:
    """The ``[paths]`` value someone set that names a file that is not there.

    Absent at the DEFAULT location is silent and stays silent: these
    files are optional, and most projects have none. A value that
    DIFFERS from the default is a statement that the file is there, so a
    typo in it (``gloden-patterns.md``, a stale exported
    ``KSTRL_GOLDEN_PATTERNS_FILE``) has to be named rather than silently
    omitting the content for the life of the project.

    ``absent`` is read off :func:`read_operator_file`, never off a second
    ``exists()``: the stat that used to be here is half of the blocker
    that function's docstring records. Comparing ``spec.path`` against
    the ANCHORED default is a comparison in one path domain, which is the
    whole of the correctness here: ``operator_file_spec`` resolves the
    configured value against the root, and a config built
    programmatically never anchors, so comparing raw values made every
    such run report its own untouched default as a typo.
    """
    if not absent or spec.path == anchored_default:
        return None
    return f"[paths] {spec.key} is set to {spec.path}, which does not exist"


def operator_file_notices(
    config: KstrlConfig,
    anchored: KstrlConfig,
    root: Path,
) -> list[tuple[str, str]]:
    """``(subject, message)`` for everything the operator has to hear.

    Derived in the PARENT, once per run (review round 1, S6 and S7). The
    loader's ``logger.warning`` runs inside a pool worker whose stderr is
    dup2'd into ``.kstrl/runs/<id>/components/<id>/engineer.log``, so a
    truncated, unreadable or misconfigured golden-patterns file left no
    mark on the terminal, the TUI, the event stream or the PR body.

    Once per run and not once per component, which the parent can only
    do because :func:`operator_file_spec` is also what every worker
    reads through.

    ONE READ PER ROW, AND AT MOST ONE MESSAGE FROM IT. The read answers
    both questions - does the configured value name anything, and can
    what it names be read - so a path that cannot even be stat'd
    produces one message rather than a second stat and a traceback.
    """
    notices: list[tuple[str, str]] = []
    for spec, default in _rows(config, anchored, root):
        result = read_operator_file(spec)
        message = _missing_message(spec, default, result.absent)
        if message is None:
            message = result.message
        if message is not None:
            notices.append((spec.subject, message))
    return notices


def configured_path_errors(config: KstrlConfig, anchored: KstrlConfig, root: Path) -> list[str]:
    """Every operator-file ``[paths]`` row of ``config`` that names nothing.

    ``KstrlConfig.validate``'s half of :func:`operator_file_notices`. A
    value that names no file is a configuration ERROR; a file that is
    there but unreadable or over budget is a warning the run reports and
    not something ``validate`` can call an error. Both halves ask
    :func:`_missing_message`, so there is one definition of the rule and
    one wording of the message.
    """
    return [
        message
        for spec, default in _rows(config, anchored, root)
        if (message := _missing_message(spec, default, read_operator_file(spec).absent)) is not None
    ]


def load_operator_file(spec: OperatorFile) -> str:
    """The delimited block for the prompt, or "" when there is nothing to add.

    See :func:`read_operator_file` for which states produce "". The
    delimiter lines carry a fresh random token per build, so no line of
    the file's own content can close the block or open a new one. What
    goes INSIDE the block is ``fact`` and never ``message``: the block is
    read by the engineer, and the remedy is addressed to the operator.
    """
    result = read_operator_file(spec)
    if result.message is not None:
        logger.warning("%s", result.message)
    if not result.body:
        return ""

    token = generate_data_delimiter()
    lines = [f"=== {spec.header} {token} ===", result.body]
    if result.fact is not None:
        lines.append(f"[{result.fact}]")
    lines.append(f"=== END {spec.header} {token} ===")
    return "\n".join(lines)
