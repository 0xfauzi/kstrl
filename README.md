# Ralph

Ralph lets you hand a feature spec to an AI coding agent and walk away. It runs the agent in a loop - writing code, running tests, checking results - and keeps going until every requirement passes or it hits a retry limit.

The problem it solves: AI coding agents like Claude Code and Codex are powerful, but they work on a single prompt at a time. If the agent doesn't finish in one shot, you're back to manually re-prompting, checking progress, and deciding what to try next. Ralph automates that outer loop. You define the requirements up front as testable acceptance criteria, and Ralph handles the iteration, progress tracking, and guardrails.

## What a session looks like

You start with an idea. Ralph helps you turn it into a structured spec, then drives an agent to implement it:

```
$ ralph init .                         # scaffold ralph config in your project
$ ralph prd create                     # define what you want to build
$ ralph run 25 --agent claude          # let the agent work for up to 25 iterations
```

Behind the scenes, each iteration:

1. Ralph reads your requirements (a JSON file of user stories with acceptance criteria)
2. It builds a prompt that includes the requirements, the agent's instructions, and a log of what happened in previous iterations
3. It sends the prompt to the agent, which writes code, runs tests, and reports back
4. Ralph checks whether the agent signaled completion and whether any file changes violated path restrictions
5. If stories remain incomplete, it loops back to step 2 with updated context

The loop exits when the agent marks all stories as passing, or when the iteration limit is reached.

```mermaid
flowchart TD
    PRD["Requirements\n(user stories + acceptance criteria)"] --> Prompt["Build prompt\n(instructions + progress from prior iterations)"]
    Prompt --> Agent["AI agent writes code and runs tests\n(Claude Code, Codex, or custom)"]
    Agent --> Check{"All stories\npassing?"}
    Check -->|No| Prompt
    Check -->|Yes| Done["Done"]
    Check -->|Max iterations| Done
```

## Why not just use Claude Code directly?

You can, and for small tasks you should. Ralph is for when you want to:

- **Define success criteria before starting** - "tests pass, types check, login form works" - and have the agent keep trying until they're met
- **Walk away** - Ralph runs unattended. You come back to a branch with the work done (or a progress log showing what was attempted)
- **Constrain the agent** - restrict which files it can touch, automatically revert out-of-scope changes
- **Track progress across iterations** - each run builds on the last, with full context injection so the agent knows what it already tried
- **Plan before building** - Ralph's interactive mode lets you have a conversation with an AI PM that stress-tests your spec before any code is written

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install ralph-cli
```

You also need at least one AI coding agent CLI:

| Agent | Install | Models |
|-------|---------|--------|
| Claude Code (recommended) | [claude.ai/code](https://claude.ai/code) | sonnet, opus, haiku |
| OpenAI Codex | [github.com/openai/codex](https://github.com/openai/codex) | o3, o4-mini |
| Custom | Any command that reads stdin | - |

## Quick start

### 1. Set up

```bash
cd your-project
ralph init .
```

This creates `ralph.toml` (configuration) and `scripts/ralph/` (prompt templates, progress log). Ralph auto-detects which agent CLIs you have installed.

### 2. Define what to build

Three options depending on how fleshed out your idea is:

**You have a rough idea** - talk it through with an AI PM first:
```bash
ralph                    # launch TUI, select "Interactive Feature"
```
Ralph starts a conversation where an AI reviewer asks probing questions from product, engineering, and reliability perspectives. When the spec is tight enough, it generates a structured PRD automatically.

**You have a spec document** - import and convert it:
```bash
ralph prd import spec.md --agent claude
```

**You want to define stories manually** - use the step-by-step wizard:
```bash
ralph prd create
```

All three produce the same output: a `prd.json` file with user stories, acceptance criteria, priorities, and a branch name.

### 3. Run

```bash
ralph run 25
```

Ralph checks out (or creates) the branch from the PRD, then starts iterating. You'll see the agent reading files, writing code, running tests. Progress is logged between iterations so each run builds on the last.

```bash
ralph run                  # 10 iterations (default)
ralph run 50 --model opus  # 50 iterations with a specific model
ralph run --interactive    # pause after each iteration for review
```

### 4. (Optional) Understand an unfamiliar codebase first

Before building features on a codebase you don't know well:

```bash
ralph understand 10
```

This runs the agent in read-only mode for 10 iterations, producing `scripts/ralph/codebase_map.md` - an evidence-based document about the architecture, patterns, and conventions. The agent reads but does not modify source files.

## Features

- **Autonomous iteration** - runs the agent in a loop until acceptance criteria pass, with progress context injected between iterations
- **Interactive feature planning** - AI PM conversation that stress-tests your spec before generating a PRD
- **Codebase understanding** - read-only mode that maps architecture before you start building
- **Guard rails** - path restrictions auto-revert out-of-scope changes; infrastructure files are protected; 3 consecutive errors trigger bail-out
- **Git integration** - auto branch creation/checkout, change tracking, per-iteration reversion of disallowed files
- **Multi-agent** - Claude Code (streaming with classified output), OpenAI Codex, or any stdin/stdout command
- **Terminal UI** - live dashboard with agent output and story progress, PRD wizard, config editor, status view
- **Headless CLI** - every TUI feature available as a command for scripting and CI

## Terminal UI

```bash
ralph                    # launch TUI
```

| Screen | What it does |
|--------|-------------|
| Run dashboard | Live agent output alongside story progress table. `p` pause, `s` stop |
| Interactive feature | Chat with an AI PM to refine your spec and generate a PRD |
| PRD wizard | Step-by-step story creation form |
| Config | Visual editor for ralph.toml |
| Status | Project overview with story table |

## CLI reference

```
ralph                     Launch TUI
ralph init [DIR]          Set up Ralph in a project
ralph run [N]             Run the agent loop (N = max iterations, default 10)
ralph understand [N]      Run read-only codebase mapping
ralph prd create          PRD creation wizard
ralph prd import FILE     Generate PRD from a spec document
ralph prd validate        Check prd.json schema
ralph prd status          Story summary table
ralph config show         Print current config
ralph config init         Create ralph.toml with defaults
ralph status              Project overview
```

## Configuration

Ralph uses `ralph.toml` at the project root:

```toml
[agent]
type = "claude"           # "claude", "codex", or "custom"
model = ""                # model override (empty = agent default)
command = ""              # shell command for custom agents

[run]
max_iterations = 10
sleep_seconds = 2
interactive = false

[paths]
allowed = []              # restrict which files the agent can change

[git]
branch = ""               # override branch (empty = use PRD branch name)
auto_checkout = true
```

Environment variables override ralph.toml: `AGENT_CMD`, `MODEL`, `INTERACTIVE`, `SLEEP_SECONDS`, `ALLOWED_PATHS`, `RALPH_BRANCH`.

## How the PRD works

The PRD (`prd.json`) is a list of user stories with testable acceptance criteria:

```json
{
  "branchName": "ralph/login-feature",
  "userStories": [
    {
      "id": "US-001",
      "title": "User can log in with email",
      "acceptanceCriteria": [
        "Login form accepts email and password",
        "Invalid credentials show error message",
        "Tests pass: uv run pytest tests/test_auth.py"
      ],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}
```

The agent updates `passes` and `notes` as it works. Ralph reads these between iterations to decide whether to continue or stop. Acceptance criteria should be concrete and testable - commands the agent can run, behavior it can verify.

## Architecture

### System overview

```mermaid
flowchart TB
    subgraph Interface["Interface layer"]
        CLI["cli.py\nClick commands\n+ RichCallbacks"]
        TUI["tui/app.py\nTextual app"]
        subgraph TUI_Screens["TUI screens"]
            Dashboard["Run dashboard\nDashboardCallbacks"]
            FeatureConv["Feature conversation"]
            PRDWiz["PRD wizard"]
            ConfigScr["Config editor"]
        end
        TUI --> TUI_Screens
    end

    subgraph Core["Core engine"]
        Loop["loop.py\nrun_loop()"]
        Agent["agent.py\nAgent abstraction"]
        Conv["conversation.py\nInteractive planning"]
    end

    subgraph Data["Data and state"]
        Config["config.py\nralph.toml + env vars"]
        PRD["prd.py\nSchema + validation"]
        Git["git_ops.py\nBranch + path enforcement"]
        Models["models.py\nAgent registry + detection"]
        Templates["templates/\nPrompt templates"]
    end

    subgraph External["External"]
        Claude["Claude Code CLI"]
        Codex["Codex CLI"]
        Custom["Custom command"]
        Repo["Git repository"]
    end

    CLI -->|LoopCallbacks protocol| Loop
    Dashboard -->|LoopCallbacks protocol| Loop

    Loop --> Agent
    Loop --> PRD
    Loop --> Git
    Loop --> Config
    Loop --> Templates

    FeatureConv --> Conv
    Conv --> Agent

    Agent --> Models
    Agent --> Claude
    Agent --> Codex
    Agent --> Custom

    Git --> Repo

    Models -.->|auto-detect| Claude
    Models -.->|auto-detect| Codex
```

### Iteration lifecycle

This is what happens inside `run_loop()` on each iteration:

```mermaid
flowchart TD
    subgraph Init["Initialization (once)"]
        A1["load_config()\ntoml + env vars + CLI flags"] --> A2["load_prd()"]
        A2 --> A3["checkout_branch(prd.branch_name)"]
        A3 --> A4["on_loop_start(config, prd)"]
    end

    subgraph Iteration["Iteration (repeats up to N times)"]
        B1["on_iteration_start(i, max)"] --> B2["run_agent_async()\nBuild prompt with progress context"]
        B2 --> B3["Spawn agent subprocess\nStream AgentOutput lines"]
        B3 --> B4["on_agent_line(output)\nfor each streamed line"]
        B4 --> B5["detect_completion()\nCheck for COMPLETE marker"]
        B5 --> B6{"Guard rails\nenabled?"}
        B6 -->|Yes| B7["find_disallowed_files()\nrevert_files()\non_guard_violation()"]
        B6 -->|No| B8["on_iteration_end(i, elapsed)"]
        B7 --> B8
    end

    subgraph Exit["Exit conditions"]
        C1["All stories pass"] --> C4["on_complete(success)"]
        C2["Max iterations reached"] --> C4
        C3["3 consecutive errors"] --> C4
    end

    A4 --> B1
    B8 --> C1
    B8 -->|"Stories incomplete"| B1
```

### Key design decisions

**Callback protocol**: The loop (`loop.py`) knows nothing about how output is displayed. It fires events via `LoopCallbacks` - a protocol that both the CLI (`RichCallbacks`) and TUI (`DashboardCallbacks`) implement. This means the same loop drives headless CLI runs and the live TUI dashboard.

**Agent abstraction**: `agent.py` dispatches to three implementations: Claude (stream-json parsing with deduplication), Codex (line-by-line transcript parsing), or a generic stdin/stdout command. All three yield the same `AgentOutput(line, role)` stream.

**Progress injection**: Between iterations, `agent.py` reads the last 5 entries from `progress.txt` and appends them to the prompt as handoff context. This gives the agent memory of what it already tried.

**Guard rails**: After each iteration, `git_ops.py` checks all changed files against `paths.allowed`. Files outside the allowed set are automatically reverted (tracked files restored, untracked files deleted). Ralph's own infrastructure files are always protected.

## Development

```bash
git clone https://github.com/0xfauzi/ralph-loop.git
cd ralph-loop
uv sync
uv tool install -e .
uv run pytest
```

## License

MIT
