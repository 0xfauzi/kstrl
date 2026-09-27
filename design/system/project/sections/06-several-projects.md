# Several projects

Each repository is its own factory. Two projects share nothing but the operator and one global learning store.

| what | where | holds |
|---|---|---|
| inputs | `<repo>/scripts/kstrl/` | `manifest.json`, `feature/<component>/prd.json`, `prompt.md` (the engineer prompt), `decisions.json` (the architect's closed decisions), `memory.md` (standing guidance), `golden-patterns.md`, `codebase_map.md` |
| artifacts | `<repo>/.kstrl/` | `runs/<run_id>/` (events, per-component logs and usage, integration reviews, launch record), `queue/` with `serve.lock`, `knowledge/`, worktrees, `evolution.jsonl` |
| control state | `${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/` | `autonomy.json`, `inbox.jsonl`, `spend.json`, `pause.json`. Clones that share an `origin` share this directory. |
| shared | `$XDG_STATE_HOME/kstrl/global/playbook/ops.jsonl` | the global learning playbook (`ks learn`) |

There is no registry of projects and no cross-project view. The TUI home reads only the repository it was opened in. The control-state directories could be enumerated by `<repo-id>`, but nothing does so today.

## For design

- A multi-project view needs a registry that does not exist yet. Designing one is designing that registry too.
- Within one project, the inbox is already the cross-run list of what needs the operator (the TUI home's *needs you*).
