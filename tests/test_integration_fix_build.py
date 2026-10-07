"""End to end: a fix component an earlier kstrl recorded, and the blocking key (#483, #696).

kstrl builds no fix since #696 decision 6. A state file an earlier kstrl
wrote can still hold one, and the review refuses an incomplete one. The
run tests drive the real ``run_factory`` through
``tests/helpers/integration_loop``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kstrl.factory import FactoryConfig
from kstrl.integration_state import state_payload_errors
from tests.helpers import integration_loop as lp


@pytest.mark.parametrize(
    ("prd", "on_disk"),
    [
        (False, "state entry yes, PRD no, manifest component no"),
        (True, "state entry yes, PRD yes, manifest component no"),
    ],
    ids=["stage-1", "stage-2"],
)
def test_a_fix_an_earlier_kstrl_left_incomplete_is_refused(
    tmp_path: Path, prd: bool, on_disk: str
) -> None:
    """kstrl builds no fix since #696, but a state file an earlier kstrl
    wrote can still hold one it did not finish. The review refuses to run
    over it, naming the stages on disk, and the run fails."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    first, _out = lp.run_loop(root, lp.Rig(root, lp.ScriptedReviewer(base, [{}])))
    assert first.exit_code == 0
    lp.record_earlier_fix(root, base, ["IF-1"], prd=prd, component=False)
    reviewer = lp.ScriptedReviewer(base, [{}])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    assert reviewer.calls == 0
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "not_run"
    assert stop["reason"] == f"integration fix {lp.FIX_1} is incomplete: {on_disk}"
    assert result.exit_code == 1
    assert len(lp.run_halts(root)) == 1


def test_the_state_accepts_a_slice_2_file_and_refuses_a_malformed_fix() -> None:
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "manifestPath": "m",
        "project": "p",
        "specFile": "",
        "featureBaseSha": "b",
        "lastReviewedSha": "",
        "findings": [],
        "stops": [],
    }
    assert state_payload_errors(base) == []
    assert state_payload_errors({**base, "fixes": [{"id": "x"}]}) == [
        "fixes[0].prdPath: must be a string, got NoneType",
        "fixes[0].findings must be a list of strings",
        "fixes[0].scope must be a list of strings",
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [("", False), ("integration_blocking = true\n", True)],
    ids=["default", "set"],
)
def test_integration_blocking_loads(tmp_path: Path, body: str, expected: bool) -> None:
    assert _toml(tmp_path, body).integration_blocking is expected


def _toml(root: Path, body: str) -> FactoryConfig:
    (root / "kstrl.toml").write_text(f"[factory]\n{body}", encoding="utf-8")
    return FactoryConfig.load(root)
