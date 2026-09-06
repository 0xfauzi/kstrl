"""Operator-authored context files read into the engineer's prompt (R10.8).

One loader for the files an operator writes by hand and the factory
reads verbatim. The first is ``scripts/kstrl/golden-patterns.md``: what a
good change looks like in this repository, stated before the run rather
than distilled after it.

TRUST. These files are trusted the way ``CLAUDE.md`` is trusted, which
``run_loop`` prepends verbatim (``kstrl/loop.py``). They are NOT passed
through the knowledge layer's injection filter
(``knowledge._is_injection_attempt``), and that is deliberate: the filter
exists because distilled facts are LLM output that a prior component's
agent could have influenced, whereas the operator authored this file.
Filtering it would mean the harness silently dropping instructions its
own operator wrote.

That trust is only as good as the file the loader actually opens, which
is why :func:`load_operator_file` is given a path under the REPO ROOT
and never a component worktree. Review round 1 (S3) is the reason: the
worktree is the tree the engineer has just been writing to, so reading
the operator's file from there let one component's agent choose what
the next component's agent is told, with no filter and a header
asserting the operator wrote it. The root copy is outside every
worktree and outside every component's ``allowedPaths``.

The residual, stated rather than implied: ``ks run`` forces
``use_worktrees=False``, so the root IS the tree the agent works in and
an agent there can edit this file. Nothing in the loader can prevent
that. What bounds it is that the edit is an ordinary working-tree change
the operator sees in ``git diff`` and in the run's own diff-scope check,
rather than a change made inside a throwaway worktree that is deleted
before anyone looks at it.

FORGERY. The block's delimiter lines carry a per-build random token from
``kstrl.delimiters`` (S4). A fixed marker is forgeable by the very
content it wraps: measured in review round 1, a file containing the line
``=== END GOLDEN PATTERNS (operator-authored) ===`` produced a block
with two closing delimiters and content sitting outside the first one,
where the engineer reads it as harness-level text. That happens whether
the operator is malicious or merely documenting the format in their own
notes. The neighbouring fixed markers (feedforward, retry context,
CLAUDE.md) wrap harness-COMPUTED text; this one wraps a file.

H3a. The delimiters and the truncation line in this module are label
glue, not instruction text: they name a block so the engineer can tell
where the operator's words start and stop, and they address no role.
Issue #303 records label glue as outside the enrolled-prompt set, with
the same treatment already given to the feedforward markers
(``=== CODEBASE CONTEXT (auto-generated) ===``), the retry-context
markers (``=== PREVIOUS ATTEMPT CONTEXT ===``) and the CLAUDE.md heading
in ``loop.py``. Nothing here is bound to a name ending in the enrolled
suffix, and nothing here is a sentence addressed to the engineer. Adding
a sentence that tells the engineer what to DO with the block would make
it a prompt body and would put it under H3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.delimiters import generate_data_delimiter
from kstrl.init_cmd import shipped_label

if TYPE_CHECKING:
    # kstrl.config imports this module inside KstrlConfig.validate, so the
    # type-only direction is the one that must not run at import time.
    from kstrl.config import KstrlConfig

logger = logging.getLogger(__name__)

#: The label the golden-patterns block carries in the engineer's prompt.
GOLDEN_PATTERNS_HEADER = "GOLDEN PATTERNS (operator-authored)"

#: Character budget for the golden-patterns file. The feedforward
#: convention is tokens times four (``FeedforwardConfig.max_context_tokens``
#: is spent as ``* 4`` in ``build_feedforward_context``), so 6000
#: characters is about 1500 tokens.
GOLDEN_PATTERNS_MAX_CHARS = 6000

#: The scaffolded filename whose digest history says "kstrl wrote this,
#: the operator has not filled it in yet". Matches a ``SCAFFOLDED_TEMPLATES``
#: row in ``kstrl/init_cmd.py``.
GOLDEN_PATTERNS_SCAFFOLD = "golden-patterns.md"


@dataclass(frozen=True)
class OperatorFile:
    """One operator-authored file and how it enters the prompt."""

    path: Path
    header: str
    max_chars: int
    #: The ``SCAFFOLDED_TEMPLATES`` filename this file is scaffolded
    #: from, when it is scaffolded at all. A body matching that
    #: template's digest history is an untouched skeleton and is treated
    #: as an empty file: see :func:`read_operator_file`.
    scaffold: str | None = None


@dataclass(frozen=True)
class OperatorText:
    """What one operator file amounts to on one read.

    ``body`` is "" whenever there is nothing to inject. ``notice`` is the
    one sentence the operator has to hear, and it is deliberately shared:
    the truncation case renders it into the prompt inside brackets AND
    reports it to the operator's UI, so the engineer and the operator
    cannot be told two different numbers.
    """

    body: str
    notice: str | None


def read_operator_file(spec: OperatorFile) -> OperatorText:
    """Read one operator file: what to inject, and what to say about it.

    "" body, no notice, for the four ordinary states: the file is
    absent, empty, whitespace-only, or byte-identical to a body
    ``ks init`` itself scaffolded. The last is the one review round 1
    measured: an untouched skeleton is the operator saying nothing yet,
    and injecting it put 479 characters of angle-bracket placeholders at
    the head of every engineer prompt of every component of every
    iteration, under a header asserting the operator authored them.
    ``init_cmd.shipped_label`` owns the digest history, so this decision
    and the staleness notice ``ks init`` prints agree by construction.

    An unreadable file (a directory in its place, mode 000, bytes that
    are not UTF-8) returns "" and a notice: a bad operator file must not
    fail a run, but it must not be silent either.

    Past ``spec.max_chars`` the text is cut at the last newline inside
    the budget, so the engineer reads whole lines rather than a sentence
    that stops mid-word. When the budget window holds no newline at all
    the hard cut stands. The character counts in the notice are the
    RENDERED body's, not the pre-strip window's (review round 1, nit 9):
    a file cut just after a blank line used to announce "12 of 254
    characters shown" over a rendered body of 10.
    """
    if not spec.path.exists():
        return OperatorText("", None)
    try:
        text = spec.path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        # ValueError alongside OSError: UnicodeDecodeError is a
        # ValueError and would escape a fail-closed `except OSError`.
        return OperatorText("", f"could not read {spec.path}: {exc}")
    if not text.strip():
        return OperatorText("", None)
    if spec.scaffold is not None and shipped_label(spec.scaffold, text) is not None:
        return OperatorText("", None)
    if len(text) <= spec.max_chars:
        return OperatorText(text.rstrip("\n"), None)

    window = text[: spec.max_chars]
    newline = window.rfind("\n")
    body = (window[:newline] if newline > 0 else window).rstrip("\n")
    return OperatorText(
        body,
        f"truncated: {len(body)} of {len(text)} characters shown; shorten {spec.path}",
    )


def operator_file_notice(spec: OperatorFile) -> str | None:
    """The one sentence about ``spec`` the operator has to hear, or None.

    Called from ``factory.run_factory``, in the parent process, where a
    ``ui`` exists and the operator is looking at it. The loader's own
    ``logger.warning`` goes to ``logging.lastResort`` on the worker's
    stderr, which ``factory`` dup2s into
    ``.kstrl/runs/<id>/components/<id>/engineer.log``: measured in
    review round 1 (S6), a truncated or unreadable file left no mark on
    the terminal, the TUI, the event stream or the PR body.

    The parent can ask this at all only because the loader reads the
    REPO ROOT and nothing else. While the worker resolved its own
    worktree first, the parent had no path to read that the worker was
    guaranteed to agree with.
    """
    return read_operator_file(spec).notice


def missing_configured_path(
    configured: Path,
    anchored_default: Path,
    root: Path,
    key: str,
) -> str | None:
    """The ``[paths]`` value someone set that names a file that is not there.

    Absent at the DEFAULT location is silent and stays silent: these
    files are optional, and most projects have none. A value that
    DIFFERS from the default is a statement that the file is there, so a
    typo in it (``gloden-patterns.md``, a stale exported
    ``KSTRL_GOLDEN_PATTERNS_FILE``) has to be named rather than silently
    omitting the content for the life of the project.

    BOTH SIDES ARE RESOLVED AGAINST ``root`` BEFORE THEY ARE COMPARED,
    and that is the whole of the correctness here. ``KstrlConfig`` field
    defaults are RELATIVE until ``anchored`` runs, and a config built
    programmatically (the SDK, an embedder, most of this suite) never
    anchors. Comparing a relative default against an absolute one made
    every such run report its own untouched default as a typo: measured
    once, in a factory run whose config was constructed by hand.
    """
    resolved = configured if configured.is_absolute() else root / configured
    if resolved == anchored_default or resolved.exists():
        return None
    return f"[paths] {key} is set to {configured}, which does not exist"


def configured_path_errors(config: KstrlConfig, anchored: KstrlConfig, root: Path) -> list[str]:
    """Every operator-file ``[paths]`` row of ``config`` that names nothing.

    One place, two callers: ``KstrlConfig.validate`` and the once-per-run
    warning in ``factory.run_factory``. R10.9 adds its memory file as a
    second row here and neither caller changes.
    """
    rows = ((config.golden_patterns_file, anchored.golden_patterns_file, "golden_patterns"),)
    return [
        e for e in (missing_configured_path(c, a, root, k) for c, a, k in rows) if e is not None
    ]


def load_operator_file(spec: OperatorFile) -> str:
    """The delimited block for the prompt, or "" when there is nothing to add.

    See :func:`read_operator_file` for which states produce "". The
    delimiter lines carry a fresh random token per build, so no line of
    the file's own content can close the block or open a new one.
    """
    result = read_operator_file(spec)
    if result.notice is not None:
        logger.warning("%s", result.notice)
    if not result.body:
        return ""

    token = generate_data_delimiter()
    lines = [f"=== {spec.header} {token} ===", result.body]
    if result.notice is not None:
        lines.append(f"[{result.notice}]")
    lines.append(f"=== END {spec.header} {token} ===")
    return "\n".join(lines)
