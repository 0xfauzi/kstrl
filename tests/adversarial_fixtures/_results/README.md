# Calibration baseline results

One `baseline-<UTC-timestamp>.json` per calibration run of
`tests/test_calibration.py` (opt-in via `KSTRL_RUN_CALIBRATION=1`).
Compare two files with:

```bash
uv run python -m kstrl.calibration compare <old.json> <new.json>
```

## Format v2 (R5.1, `"format_version": 2`)

Written by `build_report` in `kstrl/calibration.py`, which writes the
header and the fixture entries. Read back by `load_baseline` and
`partial_capture_reason` in `kstrl/calibration_baseline.py` (#406).

- Header: `model` (calibration model id - R5.5 warns when it drifts from
  the configured model), `timestamp`, `runs_per_fixture`
  (`KSTRL_CALIBRATION_RUNS`, default 3), `run_complete` (#398 - absent
  means complete, true for every baseline written before #398),
  `fixtures_attempted` and `fixtures_completed` (#398 - `role/fixture_id`,
  in run order).
- `fixtures[]`: one entry per fixture with `runs_total`, `runs_errored`
  (agent-infrastructure failures, excluded from the consistency
  denominator), `runs_detected`, `consistency` (= detected/completed),
  `detected` (consistency >= 0.5), `category`, `cwe` (security only),
  and the per-run `runs[]` detail.
- `summary`: per role - `fixtures_total`, `fixtures_detected`,
  `detection_rate` (mean per-fixture consistency), `by_category`, and
  `by_cwe` for security.

Since #398, a capture writes itself as each fixture's loop begins and
ends, so a killed run leaves one file holding the fixtures that
finished. `load_baseline` and the `compare` CLI REFUSE such a file,
naming the fixtures the run did not complete, and `newest_baseline_path`
skips it.

## Replies (#523)

Every agent reply a capture scores is kept beside its baseline, one file per
recorded run:

```
replies-<timestamp>/<role>/<fixture_id>/run-<n>.json
```

`<timestamp>` is the one in `baseline-<timestamp>.json`, and `run-<n>.json`
is that fixture's `runs[n - 1]` in the baseline (a negative fixture has no
`runs[]`; its `run-<n>.json` is the n-th of its `runs_total`). Each file holds
the run's record (`caught` or `false_positive`, `error`, `detail`), `run`, and
`calls`: one entry per agent call with the `streamed` lines and the
`final_message`. The text a matcher scored is one of the two, chosen by
`kstrl.decompose._select_agent_output`. Read one with
`jq -r '.calls[0].final_message' <file>`.

A run's reply is written before the run is recorded. A run with no reply to
keep, or whose reply cannot be written, is never recorded, its fixture never
completes, and the baseline is saved as a partial capture that `load_baseline`
refuses by name.

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

## Note on `baseline-20260901-203244.json` (#260, DECOMPOSE_PROMPT 3.0.0)

The H2 capture for the `2.0.0 -> 3.0.0` change, which replaced the halt
gate's count comparison with an identity join (`spec_issues[].id` plus
`decisions[].issue`) after review found that a disposition of
`"Escalated"` made both counts zero and the two zeros agree. Same shape
as the three above: architect only (`-k architect`), haiku, three runs
per fixture, twelve agent calls, 39m 47s, $1.3520.

| capture | architect | architect_allowed_paths | unparseable runs |
|---|---|---|---|
| `baseline-20260831-034641.json` | 1.00 | 1.00 | 0/12 |
| `baseline-20260901-113221.json` | 0.89 | 1.00 | 1/12 |
| `baseline-20260901-203244.json` | 0.89 | 1.00 | 1/12 |

Level with 2.0.0, fixture for fixture: `spec-01` 3/3, `spec-02` 2/3,
`spec-03` 3/3, `spec-04` 3/3 in both, and in both the single miss is
`spec-02-unspecified-auth` with `error=False` and
`json parse: No valid JSON found in output`, which is the single-shot
parse failure diagnosed in the note above. `compare` reports PASS both
against 1.4.2 (architect 1.00 -> 0.89, drop 0.11 against the 0.15
threshold and the 0.65 floor) and against 2.0.0 (0.89 -> 0.89).

## Which `DECOMPOSE_PROMPT` each #260 capture measured

A capture records `model`, `timestamp` and `runs_per_fixture`, and
nothing about the prompt it scored. That is a real gap: three of the
five files below carry architect numbers, and only the commit messages
say which prompt produced them. Until a capture can carry it, the
mapping is reconstructed from git by extracting the body at each
revision and hashing it.

| revision | version | bytes | sha256 |
|---|---|---|---|
| `6a422ad` (where `baseline-20260831-034641` was recorded) | 1.4.2 | 7787 | `8bce50b09f19220e58d941fe0b99a0f45d0c4e003d90a40c7570a4af542b1452` |
| `cbdff7c` (origin/main, #260's base) | 1.4.2 | 7787 | `8bce50b09f19220e58d941fe0b99a0f45d0c4e003d90a40c7570a4af542b1452` |
| `0d936c9` (#260 step 3, first commit) | 2.0.0 | 11694 | `3b7b4008023cf5bf4d496927bf9a1ca01498993f8b085eeea83f56e875b425d3` |
| `6332471` (#260 step 3, after two review rounds) | 3.0.0 | 12236 | `3632c88ab7813319ec3ccc76139ecb253c300bbea509b838436b1947bb50f147` |

The first two rows are the same bytes, which is the point of listing
both: `#266` landed between them and could have moved the body, so
`baseline-20260831-034641.json` measuring 1.4.2 exactly as `cbdff7c`
carries it is a checked fact rather than an assumption.

## The 2026-09-25 captures (#480, #401)

Two files, both haiku, three runs per fixture, owner-authorised.

`baseline-20260925-120951.json` is a full capture of every role at
`f2d9d2d`: DECOMPOSE_PROMPT 3.1.0, ARCHITECT_REPO_SOURCE_PROMPT and
ARCHITECT_NO_REPO_SOURCE_PROMPT 1.0.0, REVIEWER_PROMPT 2.0.0,
SECURITY_PROMPT 2.0.0, INTEGRATION_CRITERIA_PROMPT 1.0.0. It is the
first file to carry both arm ids of the #401 reuse fixture
(`architect_reuse` 0.67: 1/3 with the repository, 3/3 without) and the
first to carry the #482 integration ids.

| role | rate |
|---|---|
| security | 1.00 |
| security_hard | 0.92 (`sec-08-toctou-race` 2/3) |
| reviewer | 1.00 |
| architect | 1.00 |
| architect_allowed_paths | 1.00 |
| architect_reuse | 0.67 |
| security_negative, reviewer_negative | 0 of 4 fixtures a false positive |

Treat its `integration` (0.07) and `integration_clean` (0.25) figures
as VOID, like the 20260729 `architect_allowed_paths` figure above. The
tree predates #500: `integration_outcome` could not resolve a bare file
name such as `storage.py`, so a finding naming one could not be
credited, and IC5's criterion carried a `<component>` placeholder.

`baseline-20260925-211436.json` recaptures only the two integration
roles at `5f476ed` (after #500, INTEGRATION_CRITERIA_PROMPT 1.1.0):
`integration` 0.07, `integration_clean` 0.25. These are real
measurements of that tree, and they measure the output contract, not
detection: of 27 runs, 17 were refused because the reply's criterion
text differed from the PRD's and 6 because the five stories came back
as one, and all 4 runs that were scored were correct. #518 tracks the
join; re-capture both roles after it lands.

## The 2026-09-26 integration capture (#518)

`baseline-20260925-235341.json` recaptures only the two integration
roles at `d6a2a68`, after #521 joined a verdict to its story by id
(INTEGRATION_CRITERIA_PROMPT 1.1.0, haiku, three runs per fixture).

| role | rate | floor (#480 section 8) |
|---|---|---|
| integration | 0.47 | 0.65 |
| integration_clean | 0.75 | 1.0 |

Both roles are below their floors, so `integration_blocking` stays
false. Of 27 runs, 7 were still refused on the reply's shape: 6
returned no verdict for some story ids (`int-d4` in all three runs
with all five ids missing, `int-d5` once, `int-d5-clean` twice), and 1
returned two verdicts for IC1 (`int-d2-clean`). The 20 runs that were
scored:

- clean twins: 9 scored, 0 opened a finding.
- planted defects: 11 scored, 7 caught. `int-d1-stored-rows` was
  scored in all three runs and missed in all three (IC2 did not
  fail). One `int-d5` run failed IC4 but cited only a test file.

Against the 20260925-211436 capture of the same prompt, refusals fell
from 23 of 27 to 7 of 27. Every clean-twin failure in this file is a
refusal, not a false positive.

## The 2026-09-26 integration captures (#480, INTEGRATION_CRITERIA_PROMPT 1.1.0 and 1.2.0)

`baseline-20260926-065920.json` recaptures the two integration roles at `34a72b8`
(INTEGRATION_CRITERIA_PROMPT 1.1.0, haiku, three runs per fixture): `integration`
0.33, `integration_clean` 0.58. It is the same prompt and code as
`baseline-20260925-235341.json` (0.47, 0.75), so the difference is run-to-run
variance. 13 of 27 runs were refused on the reply's shape: in 5 the reviewer
judged the repository's prd.json stories or specification rules instead of
IC1 to IC5, in 3 it folded the five into one story, in 3 it returned only some
of them, in 1 it returned none, and in 1 it split IC1 into two criteria. Of
the 14 scored runs one was wrong (`int-d1-stored-rows`: IC2 passed because no
rule had been tightened yet).

`baseline-20260926-090303.json` is the same capture at INTEGRATION_CRITERIA_PROMPT 1.2.0:
`integration` 0.73, `integration_clean` 0.92, 2 of 27 refused on the reply's
shape (`int-d1-stored-rows` once with no verdicts, `int-d4-decision-criterion`
once judging the prd.json stories US-001 and US-002). `integration` meets its
floor of 0.65. `integration_clean` does not meet its floor of 1.0: one
`int-d5-predates-feature-clean` run opened a `test_quality` concern.
`int-d1-stored-rows` failed IC2 in 2 of its 3 runs, one of them citing only
`snippets.py`, so it scored 1 of 3.

The replies were kept outside the repository: they are model output, and
the repository's hooks reject some of their characters.

## The 2026-09-26 reviewer and integration captures (#480, REVIEWER_PROMPT 2.1.0)

`baseline-20260926-124722.json` is the reviewer role at REVIEWER_PROMPT 2.1.0
(haiku, three runs per fixture): `reviewer` 1.00 on the four planted
concerns, and 0 of 12 runs flagged on the four negatives. `calibration
compare` against `baseline-20260925-120951.json` passes.

`baseline-20260926-131430.json` is the integration capture at the same
prompt: `integration` 0.67, `integration_clean` 1.00, 2 of 27 refused.
Against `baseline-20260926-090303.json` (1.2.0 alone):
- no run folded IC1 to IC5 into one story (1 before);
- one `int-d4-decision-criterion` run still judged the prd.json stories
  US-001 and US-002 instead of IC1 to IC5 (1 before);
- one `int-d2-docstring-caller` run returned JSON that does not parse
  (`Expecting ',' delimiter`), a mode the earlier captures did not show;
- `int-d1-stored-rows` scored 3 of 3 (1 of 3 before);
- `int-d5-predates-feature` scored 1 of 3 (2 of 3 before): two runs failed
  the right story but named `parse_bearer` or `test_tokens.py` instead of
  `src/pastebin/tokens.py`, which the 2.1.0 citation rule forbids.
`int-d5-predates-feature` is below its floor of 0.65 in this capture.
