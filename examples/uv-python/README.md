# uv + kstrl example project

This directory is a **minimal uv-managed Python project** (via `pyproject.toml`) that includes kstrl prompt and PRD files under `scripts/kstrl/`.

## Requirements

- `uv` installed (see `https://docs.astral.sh/uv/`)
- Python 3.11+ (uv will manage it if configured)

## Setup

From this directory:

```bash
uv sync
```

Run tests:

```bash
uv run pytest
```

Run the example CLI:

```bash
uv run kstrl-uv-example Alice
```

## kstrl (agent loop) in this example

kstrl uses files in:
- `scripts/kstrl/` (prompt, PRD, progress, and understanding prompts)

Try a **dry run** (just echo the prompt through):

```bash
AGENT_CMD="cat" KSTRL_BRANCH="" uv run python -m kstrl run 1
```

Try a **fake agent** that immediately completes:

```bash
AGENT_CMD="printf 'hello\\n<promise>COMPLETE</promise>\\n'" KSTRL_BRANCH="" uv run python -m kstrl run 3
```

Notes:
- This example directory is inside the top-level git repo, so **branch checkout would affect the main repo**. Use `KSTRL_BRANCH=""` to disable branch checkout while you experiment.
- The agent instructions live in `scripts/kstrl/prompt.md`.
- The PRD lives in `scripts/kstrl/prd.json`.

## UI knobs

```bash
# Force plain output (no rich UI)
KSTRL_UI=plain uv run python -m kstrl run 1

# ASCII separators (no box-drawing chars)
KSTRL_ASCII=1 uv run python -m kstrl run 1

# Disable ANSI colors
NO_COLOR=1 uv run python -m kstrl run 1
```

## Codex knobs

```bash
# Lower reasoning effort (faster/cheaper; model-dependent)
MODEL_REASONING_EFFORT=low uv run python -m kstrl run 1
```
