"""Every numeric configuration field, and where kstrl.toml sets it (#571).

The census is closed by construction over TYPES: :func:`numeric_fields`
enumerates every field of every class ``config_sections()`` loads whose
annotation can hold an ``int`` or a ``float`` (a bool is not a number
here), so a field added later is driven through the refusal tests in
``tests/test_numeric_config.py`` with no edit to this file.

The only hand-written table is :data:`TOML_KEYS`: ``KstrlConfig`` fans out
over five kstrl.toml sections and names one field differently from its
key. Every other class reads its one section with the field name as the
key, and the tests prove that by requiring each refusal to name the key
and never to be the "no kstrl setting reads" refusal.
"""

from __future__ import annotations

import dataclasses
import typing

from kstrl.config_preflight import ConfigSection, config_sections

#: (class, field) -> (section, key), where the key is not the field name
#: in the class's one section.
TOML_KEYS: dict[tuple[str, str], tuple[str, str]] = {
    ("KstrlConfig", "max_iterations"): ("run", "max_iterations"),
    ("KstrlConfig", "sleep_seconds"): ("run", "sleep_seconds"),
    ("KstrlConfig", "agent_budget_usd"): ("agent", "budget_usd"),
}


def _holds_number(annotation: object) -> bool:
    if annotation is bool:
        return False
    if annotation in (int, float):
        return True
    return any(_holds_number(arg) for arg in typing.get_args(annotation) if arg is not Ellipsis)


def numeric_fields(cls: type) -> list[dataclasses.Field[object]]:
    """Every field of ``cls`` whose annotation can hold an int or a float.

    Only a bare number or an optional one counts: ``_holds_number`` looks
    through ``X | None`` and would also look through ``list[int]``, which
    no config field uses today. Provenance fields have no config door.
    """
    hints = typing.get_type_hints(cls)
    return [
        f
        for f in dataclasses.fields(cls)
        if not f.metadata.get("provenance") and _holds_number(hints[f.name])
    ]


def numeric_field_census() -> list[tuple[ConfigSection, dataclasses.Field[object]]]:
    """(section, field) for every numeric field of every loaded section."""
    return [
        (section, f)
        for section in config_sections()
        for f in numeric_fields(section.loader.__self__)  # type: ignore[attr-defined]
    ]


def class_name(section: ConfigSection) -> str:
    return str(section.loader.__self__.__name__)  # type: ignore[attr-defined]


def toml_door(section: ConfigSection, name: str) -> tuple[str, str]:
    """The kstrl.toml (section, key) that sets field ``name``."""
    return TOML_KEYS.get((class_name(section), name), (section.sections[0], name))
