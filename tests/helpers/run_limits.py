"""Every run limit, spelled as `ks retry` options (#526).

A retry of a run that left no launch record, or a record written before
#526, cannot tell which run limits that run ran under, so it refuses
unless each one is stated on its command line. The tests that drive such
a retry state them through this module, so a limit added to
``launch_record.run_limits`` reaches every one of them unedited.
"""

from __future__ import annotations

from collections.abc import Collection

from kstrl.factory import FactoryConfig
from kstrl.launch_record import run_limits
from kstrl.timeout import TimeoutConfig


def limit_names() -> list[str]:
    """Every run limit ``ks factory`` records, by option name."""
    return list(run_limits(FactoryConfig(), TimeoutConfig()))


def limit_option(name: str) -> str:
    """The `ks factory` / `ks retry` option spelling of a run limit."""
    return "--" + name.replace("_", "-")


def every_limit_argv(value: str = "0", *, skip: Collection[str] = ()) -> list[str]:
    """``--<limit> <value>`` for every run limit except those in ``skip``."""
    return [
        arg for name in limit_names() if name not in skip for arg in (limit_option(name), value)
    ]
