"""One owner for the secret rule (#646 slice 4).

With ``[policy] enabled`` the envelope and ``bad_patterns`` both scanned a
diff's added lines for secrets, and only the envelope reads approvals, so an
approved ``policy_secret_pattern`` still failed the retry on ``bad_patterns``.
The envelope, when it runs, now owns the rule; with it off, ``bad_patterns``
keeps it, with no approval path.

Every test drives the real ``ks factory``, ``ks inbox`` and ``ks retry`` in a
subprocess with a shell engineer, through the harness of
``tests/test_inbox_waivers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from kstrl.inbox import ItemKind
from kstrl.manifest import Manifest
from tests.test_inbox_waivers import (
    ONE_SECRET,
    SECRET_TOML,
    _component,
    _decide,
    _failed_run,
    _gated,
    _manifest_path,
    _open,
    _retry,
)

#: The envelope off: bad_patterns is then the only secret rule.
NO_POLICY_TOML = "[policy]\nenabled = false\n"


def _verification_field(root: Path, field: str) -> list[str]:
    """``field`` of the last VerificationResultEvent in the latest run."""
    run_id = Manifest.load(_manifest_path(root)).run_id
    events = root / ".kstrl" / "runs" / run_id / "events.jsonl"
    values: list[str] = []
    for line in events.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("event") == "verification_result":
            values = list(record["data"][field])
    return values


def test_a_secret_fails_on_the_envelope_alone_and_its_approval_passes_the_retry(
    tmp_path: Path,
) -> None:
    """With ``[policy] enabled`` the run reports the secret once, under
    ``policy_envelope``, and approving that one finding passes a retry that
    writes the same change."""
    root, env = _failed_run(tmp_path, SECRET_TOML, ONE_SECRET)

    assert _component(root).failed_check == "policy_envelope"
    assert _verification_field(root, "failures") == ["1 policy violation(s)"]
    # bad_patterns still ran; it no longer reports the secret.
    assert "bad_patterns" in _verification_field(root, "checks")
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert item.evidence["category"] == "policy_secret_pattern"
    _decide(root, env, "approve", item.id)

    code, out = _retry(root, env)

    assert code == 0, out
    assert _component(root).status == "completed", out
    (finding,) = _gated(root, "policy_")
    assert (finding.category, finding.severity) == ("policy_secret_pattern", "advisory")
    assert f"waiver:{item.id}" in finding.tags
    assert _verification_field(root, "failures") == []


def test_with_the_envelope_off_bad_patterns_still_blocks_a_secret(tmp_path: Path) -> None:
    """With ``[policy] enabled = false`` nothing else reads secrets, so
    ``bad_patterns`` keeps the rule, and no item offers an approval."""
    root, _env_unused = _failed_run(tmp_path, NO_POLICY_TOML, ONE_SECRET)

    assert _component(root).failed_check == "bad_patterns"
    assert _verification_field(root, "failures") == ["1 issues found in changed files"]
    assert "policy_envelope" not in _verification_field(root, "checks")
    assert _open(root, ItemKind.POLICY_EXCEPTION) == []
