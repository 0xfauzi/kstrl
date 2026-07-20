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
