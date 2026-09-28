"""What a fixture's ``expected`` block may say, and how each key is judged (#632).

One table, :data:`JUDGED_KEYS`: fixture type, then expected key, then the
observed stream the key reads and the kind of comparison it makes.
``kstrl/prd.py`` derives the accepted expected keys of each type in the table
from it and checks each value with :func:`value_errors`; ``kstrl/fixtures.py``
judges a run with :func:`judge`. So a key the validator accepts always has an
evaluator, and the check at the bottom of this module refuses to import a
table naming a kind that has no validator or no comparator.

``function`` and ``file`` fixtures are not in the table: they keep their own
hand-written checks.

This module imports only ``kstrl.jsonread``, the one module that may parse
JSON, so ``kstrl/prd.py`` can import it: prd.py cannot import
``kstrl/verify.py``, which imports prd.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kstrl.jsonread import read_json

#: fixture type -> expected key -> (observed stream, comparison kind).
#: The observed streams of a ``cli`` run are ``exit code``, ``stdout`` and
#: ``stderr``; each failure line names its stream. The order is the order
#: failures are reported in.
JUDGED_KEYS: dict[str, dict[str, tuple[str, str]]] = {
    "cli": {
        "exit_code": ("exit code", "integer"),
        "stdout_contains": ("stdout", "contains"),
        "stdout_not_contains": ("stdout", "not_contains"),
        "stdout_json": ("stdout", "json"),
        "stderr_contains": ("stderr", "contains"),
    },
}


def string_list_error(value: Any) -> str | None:
    """Why ``value`` is not a non-empty array of strings, or None.

    Empty is refused: an empty list of expected strings checks nothing, so a
    fixture holding only that would pass whatever the program printed. The
    ``file`` checks in ``kstrl/prd.py`` use this too, so both types refuse
    the same lists.
    """
    if not isinstance(value, list) or not value or not all(isinstance(s, str) for s in value):
        return "must be a non-empty array of strings"
    return None


def _integer_error(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return "must be an integer"
    return None


def _any_json_error(value: Any) -> str | None:
    """Every expected value is JSON already: it was parsed out of the PRD."""
    return None


def canonical(value: Any) -> str:
    """``value`` as one line of JSON with sorted keys and no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_text(text: str) -> str:
    """``text`` as :func:`canonical` JSON, or unchanged when it is not JSON.

    A fixture snapshot compares ``actual`` byte for byte, so two runs whose
    output differs only in key order or whitespace must record the same text.
    """
    try:
        return canonical(read_json(text))
    except json.JSONDecodeError:
        return text


def _scalar_equal(want: Any, got: Any) -> bool:
    # A bool equals only a bool: Python says True == 1. Any two numbers
    # compare by value, because JSON has one number type: 1 equals 1.0.
    if isinstance(want, bool) or isinstance(got, bool):
        return want is got
    if isinstance(want, int | float) and isinstance(got, int | float):
        return want == got
    return type(want) is type(got) and want == got


def _pairs(left: Any, right: Any) -> list[tuple[Any, Any]] | None:
    """What is left to compare of ``left`` and ``right``; None when they differ.

    Two objects with the same keys give their values paired by key, two
    arrays of one length their items paired by position, and two equal
    scalars nothing.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        return [(left[key], right[key]) for key in left] if left.keys() == right.keys() else None
    if isinstance(left, list) and isinstance(right, list):
        return list(zip(left, right, strict=True)) if len(left) == len(right) else None
    return [] if _scalar_equal(left, right) else None


def json_equal(want: Any, got: Any) -> bool:
    """Whether two parsed JSON values are the same JSON value.

    Objects compare by key set and value, whatever the key order; arrays by
    length and position. A loop over pending pairs rather than recursion, so
    any depth ``read_json`` accepts can be compared.
    """
    pending = [(want, got)]
    while pending:
        pairs = _pairs(*pending.pop())
        if pairs is None:
            return False
        pending.extend(pairs)
    return True


def _integer_equal(stream: str, want: int, got: int) -> list[str]:
    return [] if got == want else [f"{stream}: expected {want}, got {got}"]


def _contains(stream: str, want: list[str], got: str) -> list[str]:
    return [f"{stream} missing expected string: {s!r}" for s in want if s not in got]


def _not_contains(stream: str, want: list[str], got: str) -> list[str]:
    return [f"{stream} contains forbidden string: {s!r}" for s in want if s in got]


def _json(stream: str, want: Any, got: str) -> list[str]:
    try:
        parsed = read_json(got)
    except json.JSONDecodeError as exc:
        return [f"{stream} is not JSON: {exc}"]
    if json_equal(want, parsed):
        return []
    return [f"{stream} is not the expected JSON: expected {canonical(want)[:200]}"]


#: kind -> the value check the PRD validator runs on the expected value.
VALIDATORS: dict[str, Callable[[Any], str | None]] = {
    "integer": _integer_error,
    "contains": string_list_error,
    "not_contains": string_list_error,
    "json": _any_json_error,
}

#: kind -> (stream name, expected value, observed value) -> failure lines.
COMPARATORS: dict[str, Callable[[str, Any, Any], list[str]]] = {
    "integer": _integer_equal,
    "contains": _contains,
    "not_contains": _not_contains,
    "json": _json,
}

_KINDS = {kind for rows in JUDGED_KEYS.values() for _stream, kind in rows.values()}
if not _KINDS == set(VALIDATORS) == set(COMPARATORS):
    raise RuntimeError(
        "every comparison kind needs a validator and a comparator: "
        f"table {sorted(_KINDS)}, validators {sorted(VALIDATORS)}, "
        f"comparators {sorted(COMPARATORS)}"
    )


def value_errors(fixture_type: str, prefix: str, expected: dict[str, Any]) -> list[str]:
    """One indexed line per expected value of ``fixture_type`` its kind refuses."""
    errors: list[str] = []
    for key, (_stream, kind) in JUDGED_KEYS[fixture_type].items():
        error = VALIDATORS[kind](expected[key]) if key in expected else None
        if error is not None:
            errors.append(f"{prefix}.expected.{key}: {error}")
    return errors


def judge(fixture_type: str, expected: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Every way ``observed`` misses ``expected``, in table order; empty is a pass.

    ``observed`` maps each stream the type's keys read to what the run
    produced. Keys outside the table are not judged here: the PRD validator
    refuses them before anything runs.
    """
    failures: list[str] = []
    for key, (stream, kind) in JUDGED_KEYS[fixture_type].items():
        if key in expected:
            failures.extend(COMPARATORS[kind](stream, expected[key], observed[stream]))
    return failures
