"""A number kstrl reads from configuration is finite and not negative (#571).

Before #571 only ``max_cost_usd`` and ``max_total_tokens`` were checked.
``ks factory --agent-timeout nan`` ran the engineer and wrote ``NaN`` into
the launch record, which ``ks retry`` then refused as unreadable; ``inf``
crashed the run after it launched; ``-1`` read as "no limit" without
saying so. Every numeric setting now goes through :func:`check_number`,
at each of the three doors:

- kstrl.toml: :func:`refuse_non_finite`, from ``config_toml.section_table``,
  refuses ``nan`` and ``inf`` in any section before a loader coerces them.
  ``int(inf)`` raises ``OverflowError``, which no config catcher expects,
  so the refusal has to happen before the coercion.
- kstrl.toml and the environment: :func:`check_numbers`, at the return
  of every config loader, refuses a non-finite or negative value in any
  ``int`` or ``float`` field.
- the command line: :class:`LimitNumber` is the click type of every
  numeric option of ``ks factory`` and ``ks retry``.

A field whose negative values mean something (a policy cap, where a
negative value disables the cap and 0 allows nothing) carries
:data:`SIGNED` as its dataclass metadata and is checked for finiteness only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields
from types import MappingProxyType
from typing import Any, TypeVar

import click

N = TypeVar("N", int, float)
C = TypeVar("C")

#: Dataclass field metadata for a number whose negative values are meaningful.
SIGNED: Mapping[str, bool] = MappingProxyType({"signed": True})


class BudgetConfigError(ValueError):
    """A configured number cannot bound anything.

    Raised rather than coerced because every bad value fails in a
    different silent direction: ``nan`` makes ``limit > 0`` false, so the
    limit disables itself while reading as configured; a negative value
    disables it the same way; ``inf`` produces a limit that is enabled and
    can never be reached. All three are indistinguishable from "off" at
    the moment they matter.
    """


def check_number(value: N, source: str, *, signed: bool = False) -> N:
    """Return ``value`` when it is finite and, unless ``signed``, at least 0."""
    if not math.isfinite(value):
        raise BudgetConfigError(f"{source} must be a finite number, got {value!r}")
    if value < 0 and not signed:
        raise BudgetConfigError(f"{source} must be >= 0, got {value!r}")
    return value


def check_numbers(config: C) -> C:
    """Return ``config`` when every ``int`` and ``float`` field passes :func:`check_number`.

    Every config loader returns through this, so a field added later is
    checked with no other edit. A bool is not a number here, and neither
    is None (an unset optional number).
    """
    instance: Any = config
    for field in fields(instance):
        value = getattr(instance, field.name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        check_number(value, field.name, signed=bool(field.metadata.get("signed")))
    return config


def refuse_non_finite(value: object, source: str) -> None:
    """Raise when ``value``, or anything a table or array holds, is nan or inf."""
    if isinstance(value, float):
        check_number(value, source, signed=True)
    elif isinstance(value, dict):
        for key, item in value.items():
            refuse_non_finite(item, f"{source}.{key}")
    elif isinstance(value, list):
        for item in value:
            refuse_non_finite(item, source)


class LimitNumber(click.ParamType[Any, Any]):
    """A command-line number that :func:`check_number` accepts.

    Parsing is click's own ``INT`` or ``FLOAT``, so the metavar, the help
    text and the message for a value that is not a number are unchanged.
    """

    def __init__(self, base: click.types.IntParamType | click.types.FloatParamType) -> None:
        self.base = base
        self.name = base.name

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        number = self.base.convert(value, param, ctx)
        source = next((o for o in param.opts if o.startswith("--")), "value") if param else "value"
        try:
            return check_number(number, source)
        except BudgetConfigError as exc:
            self.fail(str(exc), param, ctx)


#: The click types of every numeric option of `ks factory` and `ks retry`.
LIMIT_INT = LimitNumber(click.INT)
LIMIT_FLOAT = LimitNumber(click.FLOAT)
