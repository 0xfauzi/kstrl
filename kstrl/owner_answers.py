"""The owner's answers to a spec's escalations (#639, decision 8a).

``ks inbox approve <id> --comment ANSWER`` records an answer on a
spec_escalation item; the next decompose of that spec reads it here and
appends it after the spec pin, so ``specDigest`` does not move.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from kstrl.decisions import _escalation_key
from kstrl.inbox import Inbox, InboxItem, ItemKind, ItemStatus


class OwnerAnswerError(RuntimeError):
    """The inbox cannot say which owner answers bind this spec (#639); exit 2, before spend."""

    def artifact_lines(self) -> list[str]:
        """Nothing was written. Lets callers share SpecBlockerError's handler."""
        return []


OWNER_ANSWER_PROMPT_VERSION = "1.0.0"
#: H3 (#639): appended after the spec, inside the architect's SPECIFICATION block.
OWNER_ANSWER_PROMPT = """\
===== Owner answer: inbox item {item_id} =====

An earlier decompose of this specification escalated:

{asked}

The owner answered in the inbox, and the answer is part of this specification:

{answer}"""


def render_owner_answer(item: InboxItem) -> str:
    """One answer. ``asked`` is absent on an item opened before #639: its title stands in."""
    asked = str(item.evidence.get("asked") or item.title)
    return OWNER_ANSWER_PROMPT.format(item_id=item.id, asked=asked, answer=item.decision_comment)


@dataclass(frozen=True)
class OwnerAnswers:
    #: Answered item ids, the text appended after the spec, and its sha256 ("" when none).
    item_ids: tuple[str, ...]
    text: str
    digest: str


def read_owner_answers(root_dir: Path, project_name: str, spec_source: str) -> OwnerAnswers:
    """The owner's answers to this spec's escalations (#639, decision 8a): APPROVED
    spec_escalation items, with a comment, under the key ``resolve_escalation_items``
    selects by (``ks inbox approve <id> --comment ANSWER``). Never another project's
    or spec's. Raises :class:`OwnerAnswerError` when the inbox cannot say."""
    key = _escalation_key(project_name, spec_source)
    answered = [
        i
        for i in _escalation_items(root_dir, key, spec_source)
        if i.status is ItemStatus.APPROVED and i.decision_comment.strip()
    ]
    text = "".join("\n\n" + render_owner_answer(i) for i in answered)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    return OwnerAnswers(tuple(i.id for i in answered), text, digest)


def _escalation_items(root_dir: Path, key: str, spec_source: str) -> list[InboxItem]:
    """The items under ``key``, each in its latest state. An unreadable inbox, a line
    that is not a JSON object, or an escalation record the fold's own parser
    (``InboxItem.from_dict``) refuses is a refusal, never an empty read."""
    box = Inbox(root_dir)
    try:
        scan = box.scan()
    except Exception as exc:  # noqa: BLE001 - any fault is the refusal, as stack._refusal
        raise OwnerAnswerError(f"the inbox cannot be read for owner answers: {exc}") from exc
    if scan.unreadable or scan.skipped_lines:
        raise OwnerAnswerError(
            f"the inbox at {box.path} cannot be read, or holds a line that is not a JSON "
            f"object, and any line could be an owner answer to {spec_source}. Nothing was run."
        )
    folded: dict[str, InboxItem] = {}
    for number, record in enumerate(scan.records, start=1):
        if record.get("kind") != ItemKind.SPEC_ESCALATION and record.get("dedupe_key") != key:
            continue
        item = InboxItem.from_dict(record)
        if item is None or item.kind is not ItemKind.SPEC_ESCALATION:
            raise OwnerAnswerError(
                f"record {number} of {box.path} is a spec escalation kstrl cannot read, "
                f"so an owner answer to {spec_source} could be lost. Nothing was run."
            )
        if item.dedupe_key == key:
            folded[item.id] = item
    return list(folded.values())
