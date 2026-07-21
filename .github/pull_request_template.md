<!-- Thanks for contributing to kstrl. Keep this PR to one coherent change. -->

## Summary

<!-- What does this change do, and why? -->

Closes #<!-- issue number, if any -->

## What was tested vs assumed (H4)

<!-- State what you actually exercised versus what you are assuming.
     "Ran the full suite" / "drove the factory on examples/uv-python" beats
     "should work". -->

## Checklist

- [ ] `uv run pytest tests/` passes
- [ ] `uv run mypy kstrl/ --strict` passes
- [ ] `uv run ruff check kstrl/ tests/` passes
- [ ] `uv run python scripts/gen_docs.py --check` passes (if CLI/config changed)
- [ ] If this maps to a roadmap item, the tracker doc's status is updated in this PR

## Provenance

- [ ] This change was written or substantially assisted by an AI agent
- [ ] It has been reviewed by a human (AI self-review does not count - H1)

## Prompt changes (only if an adversarial prompt body changed)

- [ ] Calibration re-run and the detection delta recorded (H2)
- [ ] `*_PROMPT_VERSION` bumped and the snapshot tuple in
      `tests/test_prompt_versions.py` updated in this diff (H3)
