# Ralph

**Ralph** is a lightweight agentic loop harness for autonomous AI-driven development. It iteratively runs an AI agent against a set of user stories until all acceptance criteria pass - or a maximum iteration count is reached.

## Overview

Ralph operates on a simple loop:

```
prompt.md --> AI Agent --> Code Changes
     ^                        |
     |                        v
     +------ prd.json <-- Tests/Validation

Repeat until: <promise>COMPLETE</promise>
              or max iterations reached
```

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install from source
uv pip install -e .

# Verify
ralph --help
```

## Quick Start

### 1. Initialize a project

```bash
# Interactive TUI wizard
ralph init

# Or headless
ralph init /path/to/project
```

This scaffolds `scripts/ralph/` with template files and creates `ralph.toml` with auto-detected agent settings.

### 2. Create a PRD

```bash
# Interactive wizard
ralph prd create

# Or generate from a spec file
ralph prd import my-spec.md --agent claude
```

The PRD wizard walks you through: feature overview, branch name, user stories with acceptance criteria, tech stack, and verification commands.

### 3. Run the loop

```bash
# Default: 10 iterations
ralph run

# Specify iterations, agent, and model
ralph run 25 --agent claude --model sonnet

# Interactive mode (pause after each iteration)
ralph run 25 --interactive
```

### 4. Launch the full TUI

```bash
# Opens the interactive terminal UI
ralph
```

The TUI provides: main menu, live run dashboard with agent output + story progress, PRD wizard, settings editor, and status overview.

## Commands

```
ralph                    Launch interactive TUI
ralph init [DIR]         Initialize project scaffolding
ralph run [N]            Run feature loop (N = max iterations)
ralph understand [N]     Run codebase understanding loop (read-only)
ralph prd create         Interactive PRD creation wizard
ralph prd import FILE    Generate PRD from spec via LLM
ralph prd validate       Validate prd.json schema
ralph prd status         Show story summary table
ralph config show        Show current configuration
ralph config init        Create ralph.toml with defaults
ralph status             Project status overview
```

## Configuration

Ralph uses `ralph.toml` at the project root. Environment variables override file settings.

```bash
# Create config with defaults
ralph config init
```

See `ralph.toml.example` for all options with documentation.

### Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `agent.type` | `claude` | Agent: `claude`, `codex`, or `custom` |
| `agent.model` | `""` (default) | Model: `sonnet`/`opus`/`haiku` (Claude) or `o3`/`o4-mini` (Codex) |
| `agent.command` | `""` | Custom shell command (type=custom only) |
| `run.max_iterations` | `10` | Max loop iterations |
| `run.interactive` | `false` | Pause after each iteration |
| `paths.allowed` | `[]` | Restrict which files the agent can change |
| `git.branch` | `""` | Override branch (empty = use PRD branchName) |

### Environment Variable Overrides

| Env Var | Maps To |
|---------|---------|
| `AGENT_CMD` | `agent.command` (also sets type=custom) |
| `MODEL` | `agent.model` |
| `INTERACTIVE` | `run.interactive` |
| `SLEEP_SECONDS` | `run.sleep_seconds` |
| `ALLOWED_PATHS` | `paths.allowed` (comma-separated) |
| `RALPH_BRANCH` | `git.branch` |

## PRD Schema

The `prd.json` file must conform to this schema:

```json
{
  "branchName": "ralph/my-feature",
  "userStories": [
    {
      "id": "US-001",
      "title": "User can log in with email",
      "acceptanceCriteria": [
        "Login form accepts email and password",
        "Typecheck passes: uv run mypy src/",
        "Tests pass: uv run pytest"
      ],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}
```

## TUI Screens

- **Main Menu**: mode selection (init, run, understand, PRD, status, settings)
- **Run Dashboard**: split-pane with live agent output (left) and story progress table (right). Keybindings: `p` pause, `s` stop, `Esc` back.
- **PRD Wizard**: 5-step form for creating a PRD from scratch
- **Config Screen**: visual editor for all ralph.toml settings with model selector
- **Status Screen**: read-only project overview with story table
- **Init Wizard**: guided project setup with agent auto-detection

## Codebase Understanding Mode

For existing codebases, run a read-only mapping loop before implementing features:

```bash
ralph understand 10
```

This builds `scripts/ralph/codebase_map.md` with evidence-based findings about the codebase architecture, patterns, and conventions. Only the map file is modified.

## Directory Structure

```
your-project/
  ralph.toml                      # Configuration
  scripts/
    ralph/
      prompt.md                   # Agent instructions
      prd.json                    # User stories
      progress.txt                # Running log
      codebase_map.md             # Understanding output
      understand_prompt.md        # Understanding mode prompt
      prd_prompt.txt              # PRD generation template
```

## Requirements

- Python 3.11+
- An agent CLI:
  - Claude CLI (`claude`) - recommended
  - OpenAI Codex CLI (`codex`)
  - Or any command that reads from stdin (`agent.type = "custom"`)
- git (optional; enables branch management and path enforcement)

## Development

```bash
# Install with dev dependencies
uv sync

# Run tests
uv run pytest

# Lint
uv run ruff check src/ tests/

# Type check
uv run mypy src/
```

## Legacy

The original bash scripts are preserved in `legacy/` for reference:
- `legacy/ralph.sh` - original loop runner
- `legacy/ui.sh` - original gum-based UI toolkit
- `legacy/init.sh` - original initialization script

## License

MIT
