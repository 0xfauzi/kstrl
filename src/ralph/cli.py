"""Ralph CLI - Click-based command-line interface.

Bare `ralph` launches the TUI. Subcommands provide headless access for CI/scripting.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ralph.agent import AgentOutput, LineRole
from ralph.config import RalphConfig, config_to_display, load_config, save_config
from ralph.loop import run_loop_sync
from ralph.models import detect_installed_agents, get_models_for_agent
from ralph.prd import (
    PRD,
    load_prd,
    validate_prd,
)
from ralph.prompt import scaffold_project

console = Console(stderr=True)

# Role -> Rich style mapping
ROLE_STYLES: dict[LineRole, str] = {
    LineRole.AI: "bold magenta",
    LineRole.THINK: "dim magenta",
    LineRole.TOOL: "bold yellow",
    LineRole.SYS: "bold dim",
    LineRole.PROMPT: "bold cyan",
    LineRole.GIT: "bold blue",
    LineRole.GUARD: "bold red",
    LineRole.USER: "bold cyan",
    LineRole.UNKNOWN: "",
}


class RichCallbacks:
    """LoopCallbacks implementation that prints to Rich console."""

    def on_loop_start(self, config: RalphConfig, prd: PRD | None) -> None:
        console.print()
        console.print(Panel("[bold]Ralph[/bold]", style="magenta", expand=False))
        console.print()

        table = Table(show_header=False, box=None, padding=(0, 2))
        for key, value in config_to_display(config).items():
            table.add_row(f"[bold]{key}[/bold]", value)
        if prd:
            table.add_row(
                "[bold]Stories[/bold]",
                f"total={prd.total_stories}  failing={prd.failing_stories}",
            )
        console.print(Panel(table, title="Configuration", expand=False))

    def on_branch_status(self, message: str) -> None:
        console.print(f"  [blue]GIT[/blue] | {message}")

    def on_iteration_start(self, iteration: int, max_iterations: int) -> None:
        console.print()
        console.rule(f"[bold]Iteration {iteration} / {max_iterations}[/bold]")

    def on_agent_line(self, output: AgentOutput) -> None:
        style = ROLE_STYLES.get(output.role, "")
        tag = output.role.value
        console.print(f"  [{style}]{tag:>6}[/{style}] | {output.line}")

    def on_iteration_end(self, iteration: int, elapsed_seconds: float) -> None:
        console.print(f"  [dim]Iteration {iteration} completed in {elapsed_seconds:.1f}s[/dim]")

    def on_guard_violation(self, disallowed: list[str]) -> None:
        console.print()
        console.print("[bold red]GUARD[/bold red] | Disallowed changes detected:")
        for f in disallowed:
            console.print(f"  - {f}")

    def on_guard_reverted(self, messages: list[str]) -> None:
        for msg in messages:
            console.print(f"  [yellow]{msg}[/yellow]")

    def on_complete(self, success: bool, iterations_used: int) -> None:
        console.print()
        if success:
            console.print(
                f"[bold green]Done[/bold green] - completed in {iterations_used} iterations"
            )
        else:
            console.print(
                f"[bold yellow]Max iterations reached[/bold yellow] ({iterations_used})"
            )

    def on_info(self, message: str) -> None:
        console.print(f"  [dim]{message}[/dim]")

    def on_error(self, message: str) -> None:
        console.print(f"  [bold red]ERROR[/bold red] {message}")


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Ralph - Agentic loop harness for autonomous AI-driven development.

    Run without a subcommand to launch the interactive TUI.
    """
    if ctx.invoked_subcommand is None:
        # Launch TUI
        from ralph.tui.app import RalphApp

        app = RalphApp()
        app.run()


@main.command()
@click.argument("directory", default=".", type=click.Path(exists=True))
def init(directory: str) -> None:
    """Initialize a project with Ralph scaffolding."""
    target = Path(directory).resolve()
    console.print(Panel(f"[bold]Ralph init[/bold]\nTarget: {target}", expand=False))

    messages = scaffold_project(target)
    for msg in messages:
        if msg.startswith("Created"):
            console.print(f"  [green]{msg}[/green]")
        else:
            console.print(f"  [dim]{msg}[/dim]")

    # Validate PRD if it exists
    prd_path = target / "scripts" / "ralph" / "prd.json"
    if prd_path.exists():
        try:
            prd = load_prd(prd_path)
            console.print(
                f"  [green]PRD valid[/green]: "
                f"{prd.total_stories} stories, {prd.failing_stories} failing"
            )
        except ValueError as e:
            console.print(f"  [red]PRD invalid[/red]: {e}")

    # Create ralph.toml if missing
    toml_path = target / "ralph.toml"
    if not toml_path.exists():
        config = RalphConfig()
        # Auto-detect agent
        installed = detect_installed_agents()
        if "claude" in installed:
            config.agent.type = "claude"
        elif "codex" in installed:
            config.agent.type = "codex"
        save_config(config, toml_path)
        console.print("  [green]Created[/green]: ralph.toml")

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  ralph run 25          # Run feature loop")
    console.print("  ralph understand 10   # Run codebase understanding")
    console.print("  ralph prd create      # Create PRD with wizard")


@main.command()
@click.argument("max_iterations", default=10, type=int)
@click.option("--interactive", "-i", is_flag=True, help="Pause after each iteration")
@click.option("--agent", "-a", type=click.Choice(["claude", "codex", "custom"]))
@click.option("--model", "-m", help="Model to use (e.g., sonnet, o3)")
@click.option("--config-file", "-c", type=click.Path(), default="ralph.toml")
def run(
    max_iterations: int,
    interactive: bool,
    agent: str | None,
    model: str | None,
    config_file: str,
) -> None:
    """Run the agentic feature loop."""
    config = load_config(Path(config_file))
    config.run.max_iterations = max_iterations
    if interactive:
        config.run.interactive = True
    if agent:
        config.agent.type = agent
    if model:
        config.agent.model = model

    cwd = Path.cwd()
    callbacks = RichCallbacks()
    result = run_loop_sync(config, cwd, callbacks)
    sys.exit(result.exit_code)


@main.command()
@click.argument("max_iterations", default=10, type=int)
@click.option("--config-file", "-c", type=click.Path(), default="ralph.toml")
def understand(max_iterations: int, config_file: str) -> None:
    """Run codebase understanding loop (read-only)."""
    config = load_config(Path(config_file))
    config.run.max_iterations = max_iterations
    config.paths.prompt = "scripts/ralph/understand_prompt.md"
    config.paths.allowed = ["scripts/ralph/codebase_map.md", "scripts/ralph/progress.txt"]
    if not config.git.branch:
        config.git.branch = "ralph/understanding"

    cwd = Path.cwd()
    callbacks = RichCallbacks()
    result = run_loop_sync(config, cwd, callbacks)
    sys.exit(result.exit_code)


@main.group()
def prd() -> None:
    """PRD management commands."""
    pass


@prd.command("create")
def prd_create() -> None:
    """Launch interactive PRD creation wizard."""
    from ralph.tui.app import RalphApp

    app = RalphApp(start_screen="prd_wizard")
    app.run()


@prd.command("validate")
@click.option("--prd-file", "-f", default="scripts/ralph/prd.json")
def prd_validate(prd_file: str) -> None:
    """Validate an existing PRD file."""
    path = Path(prd_file)
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        sys.exit(1)

    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_prd(data)
    if errors:
        console.print(f"[red]PRD validation failed ({path}):[/red]")
        for e in errors:
            console.print(f"  - {e}")
        sys.exit(1)
    else:
        console.print(f"[green]PRD is valid: {path}[/green]")


@prd.command("status")
@click.option("--prd-file", "-f", default="scripts/ralph/prd.json")
def prd_status(prd_file: str) -> None:
    """Show PRD summary and story status."""
    path = Path(prd_file)
    try:
        prd_data = load_prd(path)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    console.print(Panel(f"[bold]PRD Status[/bold]\nBranch: {prd_data.branch_name}", expand=False))

    table = Table()
    table.add_column("ID", style="bold")
    table.add_column("Title")
    table.add_column("Priority", justify="right")
    table.add_column("Status")
    table.add_column("Notes", max_width=40)

    for story in sorted(prd_data.user_stories, key=lambda s: s.priority):
        status = "[green]PASS[/green]" if story.passes else "[red]FAIL[/red]"
        table.add_row(story.id, story.title, str(story.priority), status, story.notes or "")

    console.print(table)
    console.print(
        f"\nTotal: {prd_data.total_stories}  "
        f"Passing: {prd_data.passing_stories}  "
        f"Failing: {prd_data.failing_stories}"
    )


@prd.command("import")
@click.argument("spec_file", type=click.Path(exists=True))
@click.option("--agent", "-a", type=click.Choice(["claude", "codex"]), default="claude")
@click.option("--output", "-o", default="scripts/ralph/prd.json")
def prd_import(spec_file: str, agent: str, output: str) -> None:
    """Generate a PRD from a spec file using an LLM."""
    from ralph.prompt import get_template

    spec_content = Path(spec_file).read_text(encoding="utf-8")
    template = get_template("prd_prompt.txt")

    # Combine the PRD generation prompt with the spec content
    full_prompt = template + "\n" + spec_content

    console.print(f"Generating PRD from {spec_file} using {agent}...")

    import subprocess

    from ralph.models import build_agent_command

    cmd = build_agent_command(agent)
    result = subprocess.run(
        cmd,
        shell=True,
        input=full_prompt,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        console.print(f"[red]Agent failed: {result.stderr}[/red]")
        sys.exit(1)

    # Try to parse the output as JSON
    import json

    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        # Try to extract JSON from the output (agent may have wrapped it)
        import re

        match = re.search(r"\{[\s\S]*\}", result.stdout)
        if match:
            data = json.loads(match.group())
        else:
            console.print("[red]Could not parse agent output as JSON[/red]")
            console.print(result.stdout[:500])
            sys.exit(1)

    errors = validate_prd(data)
    if errors:
        console.print("[red]Generated PRD has validation errors:[/red]")
        for e in errors:
            console.print(f"  - {e}")
        sys.exit(1)

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]PRD saved to {output}[/green]")

    prd_obj = PRD.from_dict(data)
    console.print(
        f"  Branch: {prd_obj.branch_name}  "
        f"Stories: {prd_obj.total_stories}"
    )


@main.command("config")
@click.argument("action", default="show", type=click.Choice(["show", "init"]))
@click.option("--config-file", "-c", type=click.Path(), default="ralph.toml")
def config_cmd(action: str, config_file: str) -> None:
    """Show or initialize ralph.toml configuration."""
    path = Path(config_file)

    if action == "init":
        if path.exists():
            console.print(f"[yellow]{path} already exists[/yellow]")
            return
        config = RalphConfig()
        installed = detect_installed_agents()
        if "claude" in installed:
            config.agent.type = "claude"
        elif "codex" in installed:
            config.agent.type = "codex"
        save_config(config, path)
        console.print(f"[green]Created {path}[/green]")
        return

    # Show current config
    config = load_config(path)
    table = Table(show_header=False, box=None, padding=(0, 2))
    for key, value in config_to_display(config).items():
        table.add_row(f"[bold]{key}[/bold]", value)
    console.print(Panel(table, title=f"Configuration ({path})", expand=False))

    # Show installed agents
    installed = detect_installed_agents()
    if installed:
        console.print(f"\n  Installed agents: {', '.join(installed)}")
    else:
        console.print("\n  [yellow]No known agents found in PATH[/yellow]")

    for aid in installed:
        models = get_models_for_agent(aid)
        model_names = ", ".join(m.id for m in models)
        console.print(f"    {aid}: {model_names}")


@main.command()
def status() -> None:
    """Show project status overview."""
    cwd = Path.cwd()
    config = load_config()

    console.print(Panel("[bold]Ralph Status[/bold]", expand=False))

    # Config
    table = Table(show_header=False, box=None, padding=(0, 2))
    for key, value in config_to_display(config).items():
        table.add_row(f"[bold]{key}[/bold]", value)
    console.print(table)

    # PRD
    prd_path = cwd / config.paths.prd
    if prd_path.exists():
        try:
            prd_data = load_prd(prd_path)
            console.print(
                f"\n  PRD: {prd_data.total_stories} stories "
                f"({prd_data.passing_stories} passing, {prd_data.failing_stories} failing)"
            )
            next_story = prd_data.next_story()
            if next_story:
                console.print(f"  Next: {next_story.id} - {next_story.title}")
        except ValueError as e:
            console.print(f"\n  [red]PRD error: {e}[/red]")
    else:
        console.print(f"\n  [dim]No PRD found at {config.paths.prd}[/dim]")

    # Agents
    installed = detect_installed_agents()
    console.print(
        f"\n  Agents: {', '.join(installed) if installed else '[yellow]none found[/yellow]'}"
    )


@main.command("interactive")
@click.option("--prompt", "-p", default="", help="Initial feature description")
@click.option(
    "--file", "-f", "spec_file", default="",
    type=click.Path(), help="Markdown spec file",
)
@click.option("--model", "-m", default="sonnet", help="Model to use")
def interactive_cmd(prompt: str, spec_file: str, model: str) -> None:
    """Launch interactive feature planning session."""
    from ralph.tui.screens.feature_conversation import FeatureConversationScreen

    screen = FeatureConversationScreen(
        initial_prompt=prompt,
        initial_file=spec_file,
        model=model,
    )

    from ralph.tui.app import RalphApp

    app = RalphApp(start_screen="main_menu")
    app._start_screen = ""

    def _mount() -> None:
        app.push_screen(screen)

    app.on_mount = _mount  # type: ignore[method-assign]
    app.run()
