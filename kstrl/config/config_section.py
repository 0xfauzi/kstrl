"""One kstrl.toml section and how it is loaded (#863 C11).

The module that owns a section declares a :class:`ConfigSection` constant
beside its loader. ``config_preflight.config_sections`` lists the
constants, and ``config_resolve.resolve_or_report`` takes one. The
dataclass is here, in a module with no kstrl import, so that
``kstrl.config.config`` can declare the constant of ``KstrlConfig`` without a
cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ConfigSection(Generic[T]):
    """One kstrl.toml section (or group of them) and how it is loaded.

    ``sections`` is a tuple because ``KstrlConfig`` fans out over five
    toml tables; every other entry names exactly one. ``fatal`` records
    the classification argued in the docstring of ``config_preflight``.

    ``owner`` is the config class, and :attr:`loader` reads its ``load``
    when it is asked for, not when the constant is built. A bound
    classmethod captured at import would keep calling the original after
    a test replaced ``load`` on the class, and the replacement would never
    be reached.
    """

    sections: tuple[str, ...]
    owner: type[T]
    #: False for a section whose failure degrades rather than stops the
    #: command. Exactly one entry sets it; see the docstring of
    #: ``config_preflight``.
    fatal: bool = True

    @property
    def loader(self) -> Callable[[Path], T]:
        loader: Callable[[Path], T] = self.owner.load  # type: ignore[attr-defined]
        return loader

    @property
    def label(self) -> str:
        return "/".join(f"[{name}]" for name in self.sections)
