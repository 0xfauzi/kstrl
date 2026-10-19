"""CLI entry point for kstrl."""

from __future__ import annotations

import kstrl.cli.cli_architect as cli_architect
import kstrl.cli.cli_check as cli_check
import kstrl.cli.cli_factory as cli_factory
import kstrl.cli.cli_feature as cli_feature
import kstrl.cli.cli_inbox as cli_inbox
import kstrl.cli.cli_learning as cli_learning
import kstrl.cli.cli_loop as cli_loop
import kstrl.cli.cli_queue as cli_queue
import kstrl.cli.cli_report as cli_report
import kstrl.cli.cli_serve as cli_serve
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
