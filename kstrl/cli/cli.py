"""CLI entry point for kstrl."""

from __future__ import annotations

from kstrl.cli import (
    cli_architect,
    cli_check,
    cli_factory,
    cli_feature,
    cli_inbox,
    cli_learning,
    cli_loop,
    cli_queue,
    cli_report,
    cli_serve,
)
from kstrl.cli.cli_seam import cli

#: The modules whose import registers their commands on ``cli``. Named
#: so that each import is read, and so that a module left off this
#: tuple is a visible omission rather than a silently missing command.
COMMAND_MODULES = (
    cli_architect,
    cli_check,
    cli_factory,
    cli_feature,
    cli_inbox,
    cli_learning,
    cli_loop,
    cli_queue,
    cli_report,
    cli_serve,
)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
