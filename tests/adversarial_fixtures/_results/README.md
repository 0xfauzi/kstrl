# Calibration baseline results

One `baseline-<UTC-timestamp>.json` per calibration run of
`tests/test_calibration.py` (opt-in via `KSTRL_RUN_CALIBRATION=1`).
Compare two files with:

```bash
uv run python -m kstrl.calibration compare <old.json> <new.json>
```

## Format v2 (R5.1, `"format_version": 2`)

Defined by `kstrl/calibration.py` (`build_report` / `load_baseline`).

- Header: `model` (calibration model id - R5.5 warns when it drifts from
  the configured model), `timestamp`, `runs_per_fixture`
  (`KSTRL_CALIBRATION_RUNS`, default 3).
- `fixtures[]`: one entry per fixture with `runs_total`, `runs_errored`
  (agent-infrastructure failures, excluded from the consistency
  denominator), `runs_detected`, `consistency` (= detected/completed),
  `detected` (consistency >= 0.5), `category`, `cwe` (security only),
  and the per-run `runs[]` detail.
- `summary`: per role - `fixtures_total`, `fixtures_detected`,
  `detection_rate` (mean per-fixture consistency), `by_category`, and
  `by_cwe` for security.

## Format v1 (pre-R5.1, no `format_version` key)

Single run per fixture (`caught` boolean), no category metadata. The
three `baseline-20260527-*.json` files are v1; keep them - they are the
comparison anchor for the first v2 capture, and the tooling still loads
them (v1 normalizes to `runs_total=1`).

## Note on `baseline-20260729-154010.json` (R8, #183)

Captured for the H2 check on the REVIEWER_PROMPT / SECURITY_PROMPT
1.2.0 change. Its `architect_allowed_paths` figure of 0.00 is an
ARTIFACT, not a detection result: the fixture's forbidden-path check
used a substring match, and the kstrl rename (#122/#124/#172) changed
the forbidden entry from `ralph_py/` to `kstrl/`, which is a substring
of `scripts/kstrl/feature/<id>/` - the one subtree DECOMPOSE_PROMPT
instructs the architect to include. The architect emitted the same
paths the 20260720 baseline recorded as gate-clean; the checker had
been rejecting correct output ever since the rename, silently, because
calibration is opt-in.

`_is_within` in `tests/test_calibration.py` replaced the substring test
with a path-prefix one in the same PR, and the fixture was re-run
against the fix: PASSED. Treat that role's number in this file as void
and compare it against 20260720 or later, not this one.
