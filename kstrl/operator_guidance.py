"""Writing the memory file's ``## Guidance`` section (R10.10, #231).

Split out of ``kstrl/operator_context.py`` (#231's simplify pass, B1)
when that module crossed the 800-line ratchet: the format and the
truncation rule both belong to the module that owns the READER, but the
WRITE path - refusing a malformed ``/memory`` text, inserting a record
under the heading, and the retryable file append itself - is its own
concern with its own tests, and splitting by that boundary is what kept
the two modules' tests apart too.

``GUIDANCE_HEADING`` is the one constant the reader (via
``operator_context.MEMORY``'s ``anchor_heading``), the scaffold
(``init_cmd.DEFAULT_MEMORY``) and this writer all share, imported back
into ``operator_context`` and ``init_cmd`` rather than each spelling the
literal a second time.

No runtime dependency on ``kstrl.operator_context``: ``OperatorFile``
appears only in a type annotation, made lazy by ``from __future__ import
annotations``, so ``operator_context`` can import this module at load
time with nothing importing back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kstrl.atomicio import atomic_write_text

if TYPE_CHECKING:
    from kstrl.operator_context import OperatorFile

#: R10.10 (#231). The heading `/memory` appends new entries under, and
#: the last heading `init_cmd.DEFAULT_MEMORY` ships (that module
#: interpolates this constant rather than re-typing the literal, so the
#: writer and the scaffold cannot spell it two ways). The truncator in
#: `operator_context` keys its tail cut on this heading too
#: (`operator_context.MEMORY`'s `anchor_heading`), for the reason
#: `operator_context._cut_tail_at_anchor` states.
GUIDANCE_HEADING = "## Guidance"

#: Cap on one `/memory` text. A FORMAT guard, not a tuning parameter:
#: the record is one bullet in a file with its own character budget
#: (`operator_context.MEMORY`).
MAX_MEMORY_CHARS = 500


def _heading_span(lines: list[str], heading: str) -> tuple[int, int]:
    """(index of the LAST line equal to ``heading``, index just past its section).

    ``(-1, -1)`` when ``heading`` does not occur. Shared by the writer
    (:func:`insert_under_guidance`) and the truncator
    (``operator_context._cut_tail_at_anchor``) so both agree on which
    heading is "the" section when the file has more than one, and on
    where it ends: the next ``## `` line, or end of file. The LAST match
    wins because that is where an append lands.
    """
    start = -1
    for index, line in enumerate(lines):
        if line.rstrip() == heading:
            start = index
    if start < 0:
        return -1, -1
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def memory_text_refusal(text: str) -> str:
    """Why this text may not be appended under ``## Guidance``, or "" when it may.

    Three FORMAT guards, not tuning parameters: an empty record says
    nothing, an enormous one crowds out every other standing correction
    in a file with its own character budget (``operator_context.MEMORY``),
    and a heading restructures the file that ``## Guidance`` has to stay
    a real heading of. Indentation is stripped before the ``#`` test,
    because CommonMark treats up to three leading spaces as a heading
    too.
    """
    if not text:
        return "the command carried no text"
    if len(text) > MAX_MEMORY_CHARS:
        return f"the text is {len(text)} characters, over the {MAX_MEMORY_CHARS} character limit"
    if any(line.lstrip().startswith("#") for line in text.splitlines()):
        return "the text has a line starting with `#`, which would restructure the file"
    return ""


def insert_under_guidance(body: str, record: str) -> str:
    """``body`` with ``record`` at the END of its ``## Guidance`` section.

    Reading the headings is the point, and it is what
    ``tests/test_init_cmd.py::TestTheAppendLandsUnderGuidance`` measured
    the absence of: a tail append lands under whatever section the
    operator put last, so a ``## Notes`` they added takes every
    ``/memory`` from then on and no gate goes red.

    A file with no ``## Guidance`` heading GAINS one at the end rather
    than being refused. That deletes a refusal path, a second comment
    shape, and a terminal-versus-retryable distinction in the caller's
    return type, for a change to the operator's file that is small,
    visible and uncommitted.

    The LAST matching heading wins (:func:`_heading_span`), which is
    where a tail append would have gone, so duplicate headings do not
    change the answer. The truncator
    (``operator_context._cut_tail_at_anchor``) reads headings the same
    way, so the two cannot disagree about which section is "the" one.
    """
    lines = body.split("\n")
    heading, end = _heading_span(lines, GUIDANCE_HEADING)
    if heading < 0:
        text = body if body.endswith("\n") else body + "\n"
        return f"{text}\n{GUIDANCE_HEADING}\n{record}\n"
    while end > heading + 1 and lines[end - 1].strip() == "":
        end -= 1
    lines.insert(end, record)
    joined = "\n".join(lines)
    return joined if joined.endswith("\n") else joined + "\n"


def append_guidance_record(spec: OperatorFile, record: str) -> str:
    """Append ``record`` under ``## Guidance`` in ``spec``'s file. "" or a RETRYABLE error.

    ``FileNotFoundError`` first and ``(OSError, ValueError)`` last: the
    broad clause has to be last or the narrow one above it is
    unreachable, and ``ValueError`` travels with ``OSError`` because
    ``UnicodeDecodeError`` is a ``ValueError`` and escapes a fail-closed
    ``except OSError``.

    Through :func:`kstrl.atomicio.atomic_write_text`, never a hand-rolled
    ``mkstemp`` + ``os.replace``: ``mkstemp`` creates 0600 and
    ``os.replace`` carries that onto the destination, which would
    silently retighten a git-tracked operator file.
    ``tests/test_atomicio.py`` AST-walks ``kstrl/`` and fails on a new
    ``mkstemp``.
    """
    try:
        body = spec.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Deferred: a module-level import here would make init_cmd and
        # this module import each other, the same reason
        # `operator_context.read_operator_file` defers `shipped_label`.
        from kstrl.init_cmd import DEFAULT_MEMORY

        body = DEFAULT_MEMORY
    except (OSError, ValueError) as exc:
        return f"could not read {spec.display}: {exc}"
    try:
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(spec.path, insert_under_guidance(body, record))
    except OSError as exc:
        return f"could not write {spec.display}: {exc}"
    return ""
