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
import os
import typing
from collections.abc import Iterable
from pathlib import Path

from kstrl.config_preflight import REJECTIONS, ConfigSection, config_sections

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


#: The values an environment variable is set to while the census looks
#: for the numeric field it lands in. Three, because a probe equal to a
#: field's default cannot show that it landed.
ENV_PROBES = ("1", "2", "3")


def _loaded_numbers(
    sections: list[ConfigSection],
    census: list[tuple[ConfigSection, dataclasses.Field[object]]],
    root: Path,
) -> dict[tuple[int, str], object]:
    """Each census field's value, keyed by (section index, field name), from
    every loader in ``sections`` that does not refuse the environment."""
    values: dict[tuple[int, str], object] = {}
    for index, section in enumerate(sections):
        try:
            config = section.loader(root)
        except REJECTIONS:
            continue
        for s, f in census:
            if s is section:
                values[(index, f.name)] = getattr(config, f.name)
    return values


def _probe_hits(
    name: str,
    sections: list[ConfigSection],
    census: list[tuple[ConfigSection, dataclasses.Field[object]]],
    root: Path,
    baseline: dict[tuple[int, str], object],
) -> set[tuple[int, str]]:
    """The (section index, field name) keys ``name`` sets: each key whose
    value equals a probe while ``name`` holds it, and differs without it."""
    hits: set[tuple[int, str]] = set()
    for probe in ENV_PROBES:
        os.environ[name] = probe
        try:
            loaded = _loaded_numbers(sections, census, root)
        finally:
            del os.environ[name]
        hits.update(
            key
            for key, value in loaded.items()
            if value == float(probe) and baseline.get(key) != value
        )
    return hits


def env_number_doors(
    names: Iterable[str],
    sections: list[ConfigSection],
    root: Path,
) -> list[tuple[str, ConfigSection, dataclasses.Field[object]]]:
    """(variable, section, field) for every variable in ``names`` that sets
    a numeric field of a loader in ``sections`` (#583).

    Found by setting it, not by reading the loader: a variable is a door
    to a field when some probe in :data:`ENV_PROBES` makes the loaded
    field equal that number and the field's value without the variable
    does not. The caller unsets every name in ``names`` first (through
    ``monkeypatch``, so an exported value is restored afterwards); each
    probe is removed again before the next is set.
    """
    census = [(s, f) for s in sections for f in numeric_fields(s.loader.__self__)]  # type: ignore[attr-defined]
    baseline = _loaded_numbers(sections, census, root)
    doors: list[tuple[str, ConfigSection, dataclasses.Field[object]]] = []
    for name in sorted(names):
        hits = _probe_hits(name, sections, census, root, baseline)
        doors.extend(
            (name, section, f)
            for index, section in enumerate(sections)
            for s, f in census
            if s is section and (index, f.name) in hits
        )
    return doors
