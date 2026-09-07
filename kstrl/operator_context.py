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

from kstrl.config import KstrlConfig, relative_to_root
from kstrl.delimiters import generate_data_delimiter
from kstrl.init_cmd import shipped_label

logger = logging.getLogger(__name__)

#: The fraction of the budget a truncating cut must still deliver. See
#: :func:`read_operator_file` for what it is defending against.
CUT_FLOOR = 0.9


@dataclass(frozen=True)
class OperatorFileKind:
    """One operator-authored file, declared once and read everywhere.

    Everything that differs between two such files sits on this row, so
    nothing below it spells any of them: the parent's once-per-run
    notice, ``KstrlConfig.validate`` and every worker's prompt block all
    follow the same declaration rather than a parallel one. R10.9 is the
    case that claim was made about (review round 2, should-fix 5, and
    ``_rows``' own docstring): the memory file is a second row, and no
    function in this module or in ``kstrl/factory.py`` learned its name.
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
    #: The scaffolded filename whose digest history says "kstrl wrote
    #: this, the operator has not filled it in yet". Matches a
    #: ``SCAFFOLDED_TEMPLATES`` row in ``kstrl/init_cmd.py``.
    scaffold: str


#: R10.8. 6000 characters is about 1500 tokens.
GOLDEN_PATTERNS = OperatorFileKind(
    key="golden_patterns",
    field="golden_patterns_file",
    header="GOLDEN PATTERNS (operator-authored)",
    subject="Golden patterns",
    max_chars=6000,
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
    #: The ``SCAFFOLDED_TEMPLATES`` filename this file is scaffolded
    #: from, when it is scaffolded at all. A body whose digest is in that
    #: template's history is an untouched skeleton and is treated as an
    #: empty file: see :func:`read_operator_file`.
    scaffold: str | None = None


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
    every engineer prompt of every component of every iteration, under a
    header asserting the operator authored them.
    ``init_cmd.shipped_label`` owns the digest history, so this decision
    and the staleness notice ``ks init`` prints agree by construction.
    The digest is taken on the DECODED text, so a CRLF copy of the
    scaffold is still recognised and one appended newline is not: the
    word for that is "unchanged", not "byte-identical" (review round 2,
    nit 15).

    An unreadable file (a directory in its place, mode 000 on the file or
    on its parent, a name the filesystem will not take, bytes that are
    not UTF-8) returns "" and a ``message``: a bad operator file must not
    fail a run, but it must not be silent either.

    Past ``spec.max_chars`` the text is cut at the last newline in the
    budget window, but ONLY when that newline is at or past
    ``CUT_FLOOR`` of the budget; otherwise the cut is at the budget
    boundary. Review round 2 measured the version without the floor:
    ordinary markdown written without hard wrapping is one long line per
    paragraph, and ``"# Golden patterns\\n" + "word " * 3000`` delivered
    17 of the 6000 budgeted characters. The engineer got a heading. The
    floor is on the CUT POINT; the trailing ``rstrip("\\n")`` can take
    back the blank-line run the cut lands in, which is at most a few
    characters and never content.

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
    if spec.scaffold is not None and shipped_label(spec.scaffold, text) is not None:
        return OperatorText("", None, None, absent=False)

    rendered = text.rstrip("\n")
    if len(rendered) <= spec.max_chars:
        return OperatorText(rendered, None, None, absent=False)

    window = rendered[: spec.max_chars]
    newline = window.rfind("\n")
    body = (window[:newline] if newline >= int(spec.max_chars * CUT_FLOOR) else window).rstrip("\n")
    shown = f"truncated: {len(body)} of {len(text)} characters shown"
    return OperatorText(
        body,
        f"{shown} from {spec.display}",
        f"{shown}; shorten {spec.path}",
        absent=False,
    )


def _configured(config: KstrlConfig, kind: OperatorFileKind) -> Path:
    """The path ``config`` holds for one kind, read off the row's field.

    ``getattr`` on ``kind.field`` and not a per-kind branch. Pairing one
    row with another row's field is the one-field-over defect #260 round
    2 and #229's P5 plant both paid for, and a loop over the table cannot
    commit it: there is one expression, and it names no field.
    """
    value: Path = getattr(config, kind.field)
    return value


def _rows(
    config: KstrlConfig,
    anchored: KstrlConfig,
    root: Path,
) -> tuple[tuple[OperatorFile, Path], ...]:
    """Every operator-authored file, paired with its anchored default.

    One loop over :data:`OPERATOR_FILES`, so a row added there reaches
    the parent's notice, ``KstrlConfig.validate`` and the worker's block
    with no edit below this line. R10.9 added the memory row and neither
    function below changed, because both take the subject and the
    ``[paths]`` key off the row rather than spelling either one.
    """
    return tuple(
        (operator_file_spec(kind, root, _configured(config, kind)), _configured(anchored, kind))
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
