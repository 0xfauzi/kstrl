"""Every environment variable kstrl reads is in docs/env-vars.md (#452).

Two layers, because an environment variable reaches the code two ways:

1. By NAME CONSTANT. Every string constant in ``kstrl/`` that looks
   like one of kstrl's own variables (``KSTRL_*`` or ``FACTORY_*``),
   wherever it sits: a literal read, a table row, a default such as
   ``token_env = "KSTRL_SIGNALS_TOKEN"``, a module constant such as
   ``REQUIRE_TIMEOUT_ENV``. Over-matching here costs a doc row.
2. By LITERAL READ. The first argument of ``os.environ.get``,
   ``os.getenv``, ``os.environ[...]`` and ``... in os.environ``, which
   is what catches an unprefixed name such as ``MODEL`` or ``PROMPT_FILE``.

Blind spot, stated: an unprefixed name read through a variable (the
``STRING_KEYS`` table in ``kstrl/config/config_keys.py`` is the one case
today, and its names are documented). Retired names
(``config_keys.RETIRED_ENV_VARS``) are refused when set, so they are
not settings and are not required here.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from kstrl.config.config_keys import RETIRED_ENV_VARS

REPO_ROOT = Path(__file__).resolve().parents[1]
KSTRL_DIR = REPO_ROOT / "kstrl"
ENV_DOC = REPO_ROOT / "docs" / "env-vars.md"

_KSTRL_NAME = re.compile(r"^(?:KSTRL|FACTORY)_[A-Z0-9_]+$")

#: Variables kstrl reads that are not kstrl settings, and why.
#: The inbox actor is ``getpass.getuser()`` now, which reads the login
#: variables itself, so kstrl reads none through ``os.environ``.
NOT_KSTRL_SETTINGS: dict[str, str] = {}


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _read_key(node: ast.AST) -> ast.AST | None:
    """The key expression of an environment read, or None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and _is_environ(node.func.value) and node.args:
            return node.args[0]
        if node.func.attr == "getenv" and node.args:
            return node.args[0]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_environ(node.value)
    ):
        return node.slice
    if isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
        return node.left
    return None


def literal_reads(source: str) -> Iterator[str]:
    """Layer 2: names read from the environment as string literals."""
    for node in ast.walk(ast.parse(source)):
        key = _read_key(node)
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            yield key.value


def _all_entries(tree: ast.AST) -> set[int]:
    """The ids of the string constants in an ``__all__`` assignment.

    A package ``__init__.py`` (#863) lists the Python names it re-exports,
    and ``config.KSTRL_SECTION`` is a constant, not a variable. Only the
    elements of ``__all__`` are left out: the same text anywhere else
    still counts."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                out |= {id(c) for c in ast.walk(node.value) if isinstance(c, ast.Constant)}
    return out


def kstrl_name_constants(source: str) -> Iterator[str]:
    """Layer 1: every string constant shaped like a kstrl variable."""
    tree = ast.parse(source)
    skip = _all_entries(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _KSTRL_NAME.match(node.value) and id(node) not in skip:
                yield node.value


def names_read_by_kstrl() -> dict[str, str]:
    """Every name either layer finds, mapped to its first site."""
    found: dict[str, str] = {}
    for path in sorted(KSTRL_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        site = str(path.relative_to(REPO_ROOT))
        for name in (*kstrl_name_constants(source), *literal_reads(source)):
            found.setdefault(name, site)
    return found


def test_the_literal_read_layer_sees_every_read_shape() -> None:
    source = (
        "import os\n"
        "from os import environ\n"
        "a = os.environ.get('ZZ_GET')\n"
        "b = os.getenv('ZZ_GETENV')\n"
        "c = os.environ['ZZ_SUBSCRIPT']\n"
        "d = 'ZZ_IN' in os.environ\n"
        "e = environ.get('ZZ_BARE')\n"
        "os.environ['ZZ_WRITE'] = '1'\n"
    )
    assert set(literal_reads(source)) == {
        "ZZ_GET",
        "ZZ_GETENV",
        "ZZ_SUBSCRIPT",
        "ZZ_IN",
        "ZZ_BARE",
    }


def test_the_name_layer_sees_a_constant_wherever_it_sits() -> None:
    source = "X = 'KSTRL_ZZ_TABLE'\ndef f(v: str = 'FACTORY_ZZ') -> None: ...\nY = 'kstrl_zz'\n"
    assert set(kstrl_name_constants(source)) == {"KSTRL_ZZ_TABLE", "FACTORY_ZZ"}


def test_the_name_layer_skips_only_the_entries_of_all() -> None:
    """An ``__all__`` entry is a Python name. The same text in a
    different statement of the same file still counts."""
    source = "__all__ = ['KSTRL_ZZ_NAME']\n__all__ += ['KSTRL_ZZ_MORE']\nX = 'KSTRL_ZZ_NAME'\n"
    assert list(kstrl_name_constants(source)) == ["KSTRL_ZZ_NAME"]


def test_the_walk_reaches_the_real_tree() -> None:
    """Both layers run on kstrl/: a prefixed name read through a table,
    an unprefixed literal read, and every listed exception."""
    found = names_read_by_kstrl()
    assert {"KSTRL_SERVE_POLL_INTERVAL", "KSTRL_SERVE_REQUIRE_TIMEOUT", "MAX_ITERATIONS"} <= set(
        found
    )
    assert set(NOT_KSTRL_SETTINGS) <= set(found), "an exception no longer read; drop it"


def test_every_variable_kstrl_reads_is_documented() -> None:
    doc = ENV_DOC.read_text(encoding="utf-8")
    missing = {
        name: site
        for name, site in names_read_by_kstrl().items()
        if name not in NOT_KSTRL_SETTINGS
        and name not in RETIRED_ENV_VARS
        and f"`{name}`" not in doc
    }
    assert missing == {}, f"add these to docs/env-vars.md: {missing}"
