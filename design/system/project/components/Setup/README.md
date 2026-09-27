Proposal: the first run. Pointing kstrl at a repository: how it will check the work, which agent builds, what it will write, and whether the repository is ready. This is `ks init` and `ks doctor` on one screen.

**Why this shape.** The first thing kstrl needs from a builder is not a preference but a definition: the commands that decide whether a part works. Verify runs them on every part before any reviewer reads it, so the screen leads with them and says so. Each command shows where it came from. For Python, kstrl's own defaults (`uv run pytest`, `uv run mypy` with no path when `pyproject.toml` configures mypy, `uv run ruff check .`) are already right, so `ks init` seeds nothing for them. It seeds commented commands for Rust, Go, TypeScript, JavaScript and Java or Kotlin.

**What it writes** is `ks init`'s scaffold plan, each file marked create, append or keep (the wizard's preview stage): `kstrl.toml`, seven files in `scripts/kstrl/` (prompt.md, prd.json, progress.txt, codebase_map.md, golden-patterns.md, memory.md, understand_prompt.md), a block appended to CLAUDE.md and AGENTS.md, and `.gitignore`. Nothing that exists is overwritten.

**Is it ready** is `ks doctor`'s ten static checks. Each names what in kstrl reads it, runs nothing and spends nothing. Two are marked as written by Set up rather than failed, so the count reads 8 of 10 ready now.

The header says what is true before the first run: "no runs yet", "no budget is set".

**Built on**: `init_cmd.py` (`plan_scaffold`, `_LANGUAGE_VERIFY_COMMANDS`), `verify.py` defaults, `doctor.py` (`CHECKS`), the TUI init wizard's form, preview and result stages.
