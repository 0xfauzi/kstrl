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

## Note on the three `baseline-20260901-*.json` captures (#260, DECOMPOSE_PROMPT 2.0.0)

H2 captures for the `DECOMPOSE_PROMPT` 1.4.2 -> 2.0.0 change, which adds
the four dispositions and moves the halt onto an escalation.

All three are PARTIAL: they exercise the architect only (`-k architect`),
so `reviewer`, `security` and `security_hard` carry no figure and the
compare tool warns about all three. Nothing outside `DECOMPOSE_PROMPT`
changed in that PR, and those three roles read prompts it did not touch.
Compare them against `baseline-20260831-034641.json`, not against these.

Detection rate is the mean per-fixture consistency; the previous
baseline scored 1.00 on both architect roles, and the codified floors
are 0.65 and 0.50.

| capture | architect | architect_allowed_paths | unparseable runs |
|---|---|---|---|
| `baseline-20260901-100256.json` | 0.89 | 1.00 | 1/12 |
| `baseline-20260901-104226.json` | 0.78 | 0.67 | 2/12 |
| `baseline-20260901-113221.json` | 0.89 | 1.00 | 1/12 |

The first two were taken with the 300s per-run cap that `_collect` used
to apply. The claude-code adapter yields the whole JSON as one line at
the end of a run, so a call killed at that deadline yielded no JSON and
was scored as a completed behavioural MISS rather than as an
infrastructure error. Measured on the fixture that failed
(03_ambiguous_perf, haiku, three runs of each prompt): 1.4.2 finishes in
37.8 / 41.3 / 44.8s emitting 5.7 to 6.2 KB, 2.0.0 in 188.0 / 215.1 /
226.9s emitting 17.1 to 19.9 KB. Production caps this at nothing at all.

`AGENT_RUN_TIMEOUT_S` (900.0) and the timeout check in `_collect`
landed in the same PR. The third capture is the one taken after that
fix and is the comparable number; treat the unparseable runs in the
first two as killed processes, not as detection misses.

The third capture's single unparseable run is a different thing and was
diagnosed rather than rounded off. It has `error=False`, so nothing was
killed. Reproduced once in three probe calls on the same fixture: the
output is structurally complete JSON inside a fence, carrying 44
occurrences of `\T`, an escape JSON does not define, in acceptance
criteria the model wrote as `WHEN ... \THEN ...`. Removing the stray
backslash makes the same bytes parse into 13 spec issues, 13 decisions
and 9 components. The `WHEN` / `THE SYSTEM SHALL` scaffold is identical
in both prompt versions; 1.4.2 simply never reached it, because on
these fixtures it returned `"components": []` and emitted no acceptance
criteria at all. Note also that calibration measures SINGLE-SHOT
parseability while `_decompose_spec_impl` retries with the parse error
appended, so this figure is an upper bound on what a real run sees.
