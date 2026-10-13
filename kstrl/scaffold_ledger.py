"""The scaffold ledger: how kstrl recognizes a scaffolded file on disk and upgrades it."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from kstrl.atomicio import atomic_write_text
from kstrl.scaffold_templates import (
    DEFAULT_CODEBASE_MAP,
    DEFAULT_FEATURE_UNDERSTAND_PROMPT,
    DEFAULT_GOLDEN_PATTERNS,
    DEFAULT_MEMORY,
    DEFAULT_PROMPT,
    DEFAULT_UNDERSTAND_PROMPT,
)

if TYPE_CHECKING:
    from kstrl.ui.base import UI

# ---------------------------------------------------------------------------
# #286 / H3b: telling an operator their scaffolded prompt is an older
# harness template than the one this kstrl ships. The policy and the
# argument for it live in docs/adversarial-roadmap.md H3b.
#
# The key is the file's own SHA-256, not a version stamped into it,
# because a scaffolded file is byte-identical to the constant it came
# from: `_create_if_missing` writes the body verbatim and loop.run_loop
# substitutes $prd_path and friends at READ time. So a digest lookup
# classifies files written long before this mechanism existed, separates
# a pristine older scaffold from an edited one exactly, and writes
# nothing into a file the operator owns.
#
# "Exactly" is the RAW digest, which is what `_classify` and therefore
# `ks init --upgrade-prompts` ask for: a copy with a newline appended is
# not a pristine scaffold to the path that overwrites bytes.
# `shipped_label` also offers a widened rule, and R10.9's operator-file
# loader is the caller that takes it, because its decision is whether to
# inject rather than whether to replace. The keyword at the call site is
# which rule a reader is asking for.
#
# MAINTENANCE. Each tuple is (sha256, label) oldest first, CURRENT LAST.
# When a template body changes, APPEND its new row; never edit or drop
# an old one, because an old row is the only record that can recognise a
# file already on someone's disk. ``label`` is the identifier that body
# shipped under: the ``*_VERSION`` constant where the template has one,
# otherwise the date it first appeared. Enforced by
# tests/test_prompt_versions.py and tests/test_prompt_staleness.py.


@dataclass(frozen=True)
class ScaffoldedTemplate:
    """A file `ks init` writes from a harness constant, plus the digest
    of every body that constant has ever had."""

    filename: str
    constant_name: str
    body: str
    history: tuple[tuple[str, str], ...]

    @property
    def current_label(self) -> str:
        """The identifier of the body this kstrl ships."""
        return self.history[-1][1]


# Harvested from this repository's full history by hashing the constant
# at every commit that changed it, including the commits before the
# rename to kstrl, whose bodies can still be on disk in a checkout.
SCAFFOLDED_TEMPLATES: tuple[ScaffoldedTemplate, ...] = (
    ScaffoldedTemplate(
        filename="prompt.md",
        constant_name="DEFAULT_PROMPT",
        body=DEFAULT_PROMPT,
        history=(
            # No DEFAULT_PROMPT_VERSION constant existed yet; labelled by
            # the date the body first shipped.
            (
                "15810563f3843b6634f6207d052710d72aa4fda0aa32ac86aa7718de86d34140",
                "pre-1.0.0 (2026-01-15)",
            ),
            (
                "9eb6d8f4c956d6fcacf2f39eed4a696e2755ce2ae0e24f53d08e98227fc37fc3",
                "pre-1.0.0 (2026-01-28)",
            ),
            (
                "5ec3e510a0dbd6ff41b181259b707f33a715715648cee0607ae5db6cf9992046",
                "pre-1.0.0 (2026-05-27)",
            ),
            ("a4a3a090139c370d7eecd12e3ef98055352110722750bb7b4cbf9bc50b1b9125", "1.0.0"),
            ("aa7fa6acb045dc6105d1a4c4ce8b687e1e04289c7b751eb0373b7c59dca3f7ae", "1.1.0"),
            ("4f7370f5f4efb2d9b89ce6ae09fcbf7e5c3c8fb3db22cdeb07a9221ccbc638dc", "1.1.1"),
            # Never merged: an in-review draft of #276 revised before it
            # landed. Recorded because a checkout of that branch could
            # have scaffolded from it.
            ("9bde9b20785f3740396906d1d199c2228c553c11ae956dc2f85d8aa2439fb49b", "1.2.0"),
            ("392eb698daf71d486a9d4573698df3bb2b3ca4be87c178657accc8a66c54f384", "1.3.0"),
            ("f5349c9c2fb1ac1b9bfba54c2fde3cbc266f6a8a59deaf355707504273ddc124", "1.4.0"),
            ("a11b4209e38feac0361176f4897b357ed675cb0b2fd4545f648d83560ae81dd6", "1.5.0"),
            ("d422d3757f428a3daf19e0f1bc1da1b243ae6ed6b349b7aa2ea03a834745cce3", "1.6.0"),
        ),
    ),
    # The understand templates were H3-exempt until #654 slice 8, so their
    # labels are dates. Each now has a *_PROMPT_VERSION, 1.0.0 for the body
    # in the newest row: no row was appended, because a body is recorded
    # once. The next body appends a row labelled with its version.
    ScaffoldedTemplate(
        filename="understand_prompt.md",
        constant_name="DEFAULT_UNDERSTAND_PROMPT",
        body=DEFAULT_UNDERSTAND_PROMPT,
        history=(
            ("5514376b0beeb484755d2d7d5effbe9a749b2d0972ddd30e7911e47bcf73e4ff", "2026-01-14"),
            ("1e700b55db8316392de146c549ef9fe9acf503af5c6ba2780f9d341728ac39c4", "2026-01-15"),
            ("fd02d9e3f2e559db5625c4db2d81ef0d24df481a4f4d4f5506fddd9b0962c53a", "2026-07-20"),
            ("cfd43bfeb80eaaf559ccb32d993fc2c5b2471ff90c7816648743135c2aa29688", "2026-07-21"),
        ),
    ),
    ScaffoldedTemplate(
        filename="feature_understand_prompt.md",
        constant_name="DEFAULT_FEATURE_UNDERSTAND_PROMPT",
        body=DEFAULT_FEATURE_UNDERSTAND_PROMPT,
        history=(
            ("5096447a6228e93d7d824ff5e1a334ef3eaf9edc9314a3fb7c6f7f04936cf06f", "2026-01-28"),
            ("e05fedd0ea1aff624966f4ee1e572c1af6f3926dd1b38b64678fdd6525a6f31a", "2026-07-20"),
            ("eb3637acf1918da23e27ad3f4d30bab32b1edd797b4bd1b5587b82b656affb09", "2026-07-21"),
        ),
    ),
    # R10.8. The first ledger row whose reader is the RUN and not just
    # the operator: load_operator_file suppresses a body listed here, so
    # a missing row costs an injected placeholder block rather than an
    # unshown staleness notice.
    ScaffoldedTemplate(
        filename="golden-patterns.md",
        constant_name="DEFAULT_GOLDEN_PATTERNS",
        body=DEFAULT_GOLDEN_PATTERNS,
        history=(
            # Never merged: the #229 body as it stood while the PR was in
            # review. Recorded because a checkout of that branch could
            # have scaffolded from it, and a copy on disk that kstrl does
            # not recognise is a copy it injects.
            ("b8e9cd9725308cfce280d05c26033d25cb16f51ec42af2ac4a2bb05c601e48cf", "2026-09-06a"),
            ("5f00b030f0a6e6cad4a56b678fa657ebce1a2d734e465ef82a8ca6df0638ca8a", "2026-09-06"),
            # Also never merged. Review round 2 (nit 15) measured the
            # word "byte-identical" being wrong in both directions: a
            # CRLF copy of this body IS recognised, because `read_text`
            # decodes before the digest, and a copy with one newline
            # appended is NOT. The body now says "unchanged".
            ("2dab640523bd4082e323a7cf6d13a9fae6e2a6a2886473f584cabf3b883a0300", "2026-09-07"),
            # R10.9 review round 1, should-fix 1. The preamble's two
            # lifecycle paragraphs were prompt text from the operator's
            # first edit onward: one of them ("nothing is injected") is
            # false at the point the engineer reads it, and the other two
            # sentences are imperatives addressed to the operator. 715
            # characters down to 313. Appended, never edited: an operator
            # whose disk holds the 715-character body must still be
            # recognised, or it starts being injected.
            ("103b4b2cf78aaf55163a9f10fd1c90193ba2fc05b16489687ac427b5c5d23c87", "2026-09-07b"),
        ),
    ),
    # R10.9. Same reader as the row above: a body listed here is
    # suppressed by load_operator_file, so a missing row costs an
    # injected placeholder block on every engineer prompt rather than an
    # unshown staleness notice.
    ScaffoldedTemplate(
        filename="memory.md",
        constant_name="DEFAULT_MEMORY",
        body=DEFAULT_MEMORY,
        history=(
            # Never merged outside this PR: the nine-line preamble as it
            # stood when review round 1 read it.
            ("8146096422efcb9b4196b76711fc44c80e2c7b5b920771f8a1eddcb4ba5a81c8", "2026-09-07"),
            # Round 1, should-fix 1: same defect as the row above. 354
            # characters down to 148, and "Nothing is injected while this
            # file is unchanged" is gone because the engineer only ever
            # reads it while it is being injected.
            ("07c55e3e12be359ea12cbe55f1c3296098a24c3468d6e62b66f42b0d2e1ffb83", "2026-09-07b"),
        ),
    ),
    # #199. `ks init` has always written this file; it never had a ledger
    # row, so nothing could tell an untouched scaffold from a real map.
    # The digests were re-derived by parsing the constant out of the
    # ancestor commits of main, not copied from anywhere. APPEND only.
    ScaffoldedTemplate(
        filename="codebase_map.md",
        constant_name="DEFAULT_CODEBASE_MAP",
        body=DEFAULT_CODEBASE_MAP,
        history=(
            ("48ede9d3d9ce78ebcc0e0b2b7e030cc3be3e58894627a875a4c29420b541126a", "2026-01-14"),
            ("382a66611762f9dba9098cad4720fdc4641e36e86b55865e56879aea7e7edea3", "2026-01-15"),
            ("664063e1929c7a50fef5e95e6d275c6502711ba2bcc249545f84bdc9a7160c51", "2026-07-20"),
        ),
    ),
)


def shipped_label(
    filename: str,
    text: str,
    *,
    ignore_trailing_newlines: bool = False,
) -> str | None:
    """The label of the shipped body ``text`` is, or None for anything else.

    ONE table, read by two callers that need two different answers, and
    the keyword is where the difference is written rather than in a
    second copy of the lookup. A second copy would be a second definition
    of "kstrl wrote this file", and the weaker one is the one whichever
    caller reaches it consults.

    Keyed on the file's own SHA-256, so it classifies files written long
    before this mechanism existed and writes nothing into a file the
    operator owns. Labels are unique per template
    (``test_history_rows_are_unique_and_non_empty``), so comparing the
    returned label against ``current_label`` is exactly comparing
    digests.

    THE DEFAULT IS THE RAW DIGEST, which is byte identity against a body
    kstrl shipped. :func:`_classify` takes it, and through
    ``_upgrade_scaffolded_templates`` that is the one path in `ks init`
    that REPLACES an operator's bytes: it may act only where nothing of
    theirs can be in the file, so a file that is a shipped body plus a
    trailing newline is EDITED for that path and is left alone. Review
    round 2 (should-fix 2) measured the alternative: such a file
    classified ``stale``, was overwritten 4446 bytes to 4468, and the
    operator was told "it held no local edits" about a file holding two
    newlines they may have typed.

    ``ignore_trailing_newlines=True`` widens it to a second digest, the
    text with its trailing newlines collapsed to one.
    ``operator_context.read_operator_file`` asks for that, because its
    decision is whether to INJECT a scaffold nobody has filled in: it
    renders ``text.rstrip("\\n")``, so under the raw rule
    ``DEFAULT_MEMORY + "\\n"`` injected a placeholder block whose body was
    byte-identical to the one the unedited file suppresses (round 1, nit
    3), and #231 makes a daemon the writer of that file. The asymmetry is
    the consequence of being wrong: injecting nothing is undone by
    editing the file, overwriting it is not.

    Raw first and widened second, so the keyword can only ADD matches. A
    historical body that ended in two newlines is still found by its own
    digest, which a normalise-then-look-up form would have lost, and the
    bodies for those rows no longer exist to re-derive.
    """
    template = next((t for t in SCAFFOLDED_TEMPLATES if t.filename == filename), None)
    if template is None:
        return None
    digests = dict(template.history)
    candidates = (text, text.rstrip("\n") + "\n") if ignore_trailing_newlines else (text,)
    for candidate in candidates:
        label = digests.get(hashlib.sha256(candidate.encode("utf-8")).hexdigest())
        if label is not None:
            return label
    return None


# absent        - no file there; run_loop falls back to the constant and
#                 says so itself.
# current       - byte-for-byte the body this kstrl ships.
# stale         - byte-for-byte an OLDER body this kstrl once shipped.
# unrecognised  - matches nothing kstrl has ever shipped, a copy with a
#                 trailing newline added included. Says nothing about who
#                 wrote it, which is the point: an edited file and a file
#                 from a build outside this history are indistinguishable,
#                 so neither is claimed to be stale, and neither is
#                 overwritten by `ks init --upgrade-prompts`.
TemplateStatus = Literal["absent", "current", "stale", "unrecognised"]


@dataclass(frozen=True)
class TemplateState:
    """What a scaffolded file on disk is, against the shipped history."""

    template: ScaffoldedTemplate
    path: Path
    status: TemplateStatus
    shipped_label: str | None = None


def _classify(template: ScaffoldedTemplate, path: Path) -> TemplateState:
    """Classify ``path`` as a copy of ``template``, on the RAW digest.

    Raw because of who reads the answer: ``_upgrade_scaffolded_templates``
    turns ``stale`` into an overwrite of the operator's file, and the
    argument that authorises it is that the bytes on disk are bytes kstrl
    itself wrote. A file that is a shipped body plus a trailing newline
    is therefore ``unrecognised`` here - reported, and left alone - even
    though ``operator_context`` treats the same file as an unedited
    scaffold and injects nothing from it. Two readers, two rules; see
    :func:`shipped_label`, which holds both.

    An unreadable file is ``unrecognised`` for the same reason: we cannot
    prove it is a body we shipped, so we make no claim about it (the same
    reason ``_gitignore_state`` leaves an unreadable .gitignore alone).

    The staleness notice and the wizard's preview read this too, and both
    inherit the raw rule. That is the conservative direction for them as
    well: neither says anything about a file it cannot prove kstrl wrote,
    where the widened rule would have had the notice call a file
    "unedited" that is not byte-identical to anything shipped.
    """
    if not path.exists():
        return TemplateState(template=template, path=path, status="absent")
    text = _read_text_or_none(path)
    if text is None:
        return TemplateState(template=template, path=path, status="unrecognised")
    label = shipped_label(template.filename, text)
    if label is None:
        return TemplateState(template=template, path=path, status="unrecognised")
    return TemplateState(
        template=template,
        path=path,
        status="current" if label == template.current_label else "stale",
        shipped_label=label,
    )


def classify_scaffolded_path(path: Path) -> TemplateState | None:
    """Classify ``path``, or None when kstrl does not scaffold that name.

    Matching on the basename rather than the full path is deliberate:
    ``--prompt`` and ``PROMPT_FILE`` can point anywhere, and a file the
    operator named something else is not a scaffold we can speak about.
    """
    template = next((t for t in SCAFFOLDED_TEMPLATES if t.filename == path.name), None)
    return None if template is None else _classify(template, path)


def classify_scaffold(root: Path) -> list[TemplateState]:
    """Classify every template `ks init` scaffolds under ``root``."""
    kstrl_dir = root / "scripts" / "kstrl"
    return [_classify(t, kstrl_dir / t.filename) for t in SCAFFOLDED_TEMPLATES]


def _rewrite_blockers(path: Path, root: Path | None = None) -> list[str]:
    """Every reason `ks init --upgrade-prompts` would not rewrite ``path``.

    The upgrade replaces bytes with ``os.replace``, which swaps the
    directory entry. Wherever that entry is shared, replacing it
    silently un-shares it: the project moves to the new body, the source
    every other project reads stays on the old one, and nobody is told.
    That is the outcome #286 exists to prevent, produced by the fix for
    it. Measured on this tree before the guard: a symlinked prompt.md
    became a regular file with the shared source untouched, and a
    hard-linked one had its inode replaced with the same result.

    Following the link instead of refusing is the other option, and is
    rejected on scope: `ks init <dir>` must not rewrite a file outside
    <dir>, and a shared prompt is by definition read by projects this
    invocation was not pointed at. Naming the target costs the operator
    one command and keeps the decision theirs.

    ``root``, when given, adds the check only a caller that knows the
    project can make: a symlinked ``scripts/kstrl`` directory puts the
    file outside <dir> with no link on the leaf at all. (`ks init` has
    always CREATED straight through such a link; this only refuses to
    REWRITE through it.) Reasons compose rather than shadowing each
    other, so an operator with two of them is told about both.
    """
    blockers: list[str] = []
    if path.is_symlink():
        blockers.append(f"a symlink to {path.resolve()}")
    else:
        try:
            links = path.stat().st_nlink
        except OSError:
            links = 1
        if links > 1:
            blockers.append(f"a hard link shared with {links - 1} other name(s)")
    if root is not None and root.resolve() not in path.resolve().parents:
        blockers.append(f"outside this project, at {path.resolve()}")
    if not path.parent.match("scripts/kstrl"):
        blockers.append("not the copy under scripts/kstrl/ that `ks init` upgrades")
    return blockers


def _upgrade_remedy(path: Path, root: Path | None = None) -> str:
    """The one sentence that says what to do about a stale ``path``.

    Shared by the run-time warning and by `ks init --upgrade-prompts`
    itself, so the advice an operator reads and the behaviour they then
    get cannot be worded into disagreement.
    """
    blockers = _rewrite_blockers(path, root)
    if not blockers:
        return "Run `ks init --upgrade-prompts` to take the current one."
    return (
        "`ks init --upgrade-prompts` will not touch this one: it is "
        + ", and ".join(blockers)
        + ". Update the file it shares, or replace this one by hand."
    )


class StalenessNotice(NamedTuple):
    """The two operator-facing lines about one stale scaffold."""

    headline: str
    advice: str


def staleness_notice(path: Path) -> StalenessNotice | None:
    """What to tell an operator about ``path``, or None to say nothing.

    None whenever there is nothing TRUE to say: the file is current, it
    is missing, it is not a template kstrl scaffolds, or it matches no
    body kstrl ever shipped (see ``unrecognised``).
    """
    state = classify_scaffolded_path(path)
    if state is None or state.status != "stale":
        return None
    # No count of how far behind: the ledger records bodies, not
    # releases (one row never reached main), so "N revisions behind" is
    # a number an operator cannot reconcile with the changelog. The two
    # labels are the actionable fact and both are exact.
    #
    # The remedy is only offered where it works, and _upgrade_remedy is
    # the single place that decides that, so this warning cannot promise
    # something `ks init --upgrade-prompts` then refuses to do.
    remedy = _upgrade_remedy(path)
    return StalenessNotice(
        headline=(
            f"{path} is the {state.template.filename} kstrl shipped at "
            f"{state.shipped_label}, unedited; this kstrl ships "
            f"{state.template.current_label}."
        ),
        advice=(
            "`ks init` never overwrites it, so every change to that "
            f"template since {state.shipped_label} has missed this "
            f"project. {remedy}"
        ),
    )


def _atomic_replace(target: Path, content: str) -> None:
    """Replace an EXISTING file's bytes atomically, keeping its mode.

    The write is ``atomicio.atomic_write_text``, which since #291 is the
    one owner of the mode and encoding rules this used to spell out for
    itself. ``atomicio`` is a stdlib-only leaf, so importing it here does
    NOT put ``kstrl.workqueue`` in ``kstrl.loop``'s static import
    closure via loop's deferred ``init_cmd`` import, which is what #274's
    ``tests/test_state_dir_scope.py`` refuses and what kept this copy
    hand-rolled until now.

    REPLACE, not create, is the part that stays here, because it is this
    caller's contract rather than the writer's, and a ``must_exist=``
    flag on the shared writer would be a special case carried by nine
    callers that do not want it. The one caller only ever rewrites a file
    the ledger already classified, so a missing target is a bug rather
    than a case to handle: the run stops instead of quietly doing a
    non-atomic create under a name that promises otherwise, which is what
    ``atomic_write_text`` on its own would do.
    """
    if not target.is_file():
        raise FileNotFoundError(
            f"_atomic_replace expects an existing file, got {target}: this "
            f"function only rewrites templates the scaffold ledger already "
            f"classified, so a missing target means the caller is wrong"
        )
    atomic_write_text(target, content)


def _upgrade_scaffolded_templates(root: Path, ui: UI) -> None:
    """Rewrite pristine older scaffolds with the body kstrl ships now.

    Safe to overwrite precisely because the RAW digest proved the file is
    byte-identical to a template kstrl itself wrote: there is nothing of
    the operator's in it to lose, not even a trailing newline. That is
    ``_classify``'s rule and not the loader's, which is widened; review
    round 2 (should-fix 2) is why the two are written apart, because for
    one release the widened rule reached this path and the sentence above
    was false about the file it authorised replacing. Anything the
    history does not recognise is left alone and reported, never merged
    or guessed at.

    "Nothing of the operator's to lose" is about the BYTES. The
    directory entry is a second thing they own, so a prompt they share
    by link, or one that resolves outside this project, is reported and
    left alone: see ``_rewrite_blockers``.

    The write is atomic (``_atomic_replace``) rather than a plain
    ``Path.write_text``: this is the one path in `ks init` that REPLACES
    bytes rather than creating a file, and an interruption mid-write
    would truncate the very prompt this feature exists to protect, into
    a body no ledger row recognises and so nothing will offer to fix.
    """
    for state in classify_scaffold(root):
        name = state.path.name
        if state.status == "stale":
            blockers = _rewrite_blockers(state.path, root)
            if blockers:
                ui.warn(f"  {name} is {blockers[0]}; left alone")
                ui.info(f"    {_upgrade_remedy(state.path, root)}")
                continue
            _atomic_replace(state.path, state.template.body)
            ui.ok(
                f"  {name}: {state.shipped_label} -> "
                f"{state.template.current_label} (it held no local edits)"
            )
        elif state.status == "current":
            ui.info(f"  {name} is already at {state.template.current_label}")
        elif state.status == "unrecognised":
            ui.warn(f"  {name} matches no template kstrl has shipped; left alone")
            ui.info(
                "    Either you edited it or it came from a build outside "
                "this history, and nothing here can tell those apart. "
                f"Diff it against {state.template.constant_name} in "
                "kstrl/init_cmd.py yourself."
            )
        else:  # absent: the scaffold pass below writes it
            ui.info(f"  {name} is missing; the scaffold below creates it")


def _read_text_or_none(path: Path) -> str | None:
    """``path``'s text, or None when it cannot be read as text.

    Catches ValueError alongside OSError because UnicodeDecodeError is a
    ValueError: a .gitignore that is not UTF-8 crashed `ks init` and
    the TUI scaffold preview with a traceback (#201 review). A
    directory in the file's place raises IsADirectoryError, an OSError.

    UTF-8 is pinned rather than left to the locale because #286's
    classification is byte equality against a template `ks init` wrote:
    if the read and the write can disagree about the encoding, then on a
    non-UTF-8 locale a template carrying one non-ASCII character would
    scaffold and immediately classify as ``unrecognised``, and so would
    never be reported or upgraded. Every shipped body is ASCII today,
    which is exactly why this is cheap to pin now. It also makes the
    .gitignore read deterministic across machines; a file that is not
    UTF-8 still degrades to None and is still left alone, as before.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
