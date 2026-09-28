# Provenance of the kept integration reviewer replies

Each file holds the `final_message` of one integration calibration run, copied
verbatim from the replies a capture kept (#523), with the source path in each
file's `capture` field. Three come from the 2026-09-26 capture saved as
`tests/adversarial_fixtures/_results/baseline-20260926-131430.json`
(REVIEWER_PROMPT 2.1.0, INTEGRATION_CRITERIA_PROMPT 1.2.0, claude-code haiku).
One comes from the earlier capture saved as
`tests/adversarial_fixtures/_results/baseline-20260926-065920.json`.

| file | capture run | how the factory read it |
|---|---|---|
| int-d2-unparseable.json | 131430 integration/int-d2-docstring-caller/run-3 | refused: "Failed to parse reviewer output as JSON"; the invalid JSON holds `"verdict": "fail"` on IC1, the planted story |
| int-d2-scored.json | 131430 integration/int-d2-docstring-caller/run-1 | scored: IC1 failed citing src/pastebin/api.py and src/pastebin/tokens.py |
| int-d4-other-stories.json | 131430 integration/int-d4-decision-criterion/run-1 | refused: no verdict for IC1 to IC5; it judged the prd.json stories US-001 and US-002 and states one fail concern |
| int-d2-no-stories.json | 065920 integration/int-d2-docstring-caller/run-1 | refused: no verdict for IC1 to IC5; it holds no stories and one advisory concern |

`tests/test_integration_reask.py` replays them through the real review of the
fixture repository they were written about.
