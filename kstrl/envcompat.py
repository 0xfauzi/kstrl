"""Environment compatibility layer for the Ralph -> kstrl rename.

``KSTRL_*`` is the primary namespace. For one release the legacy
``RALPH_*`` spellings are honored with a once-per-variable
DeprecationWarning so existing setups keep working while announcing the
rename. ``get``/``contains``/``require`` mirror ``os.environ.get``,
``name in os.environ`` and ``os.environ[name]`` respectively - call
sites read exactly like before, just through this module.

The bare ``FACTORY_*`` family is NOT aliased here: it predates the
rename and its call sites dual-read explicitly (factory config).
"""

from __future__ import annotations

import os
import warnings
from typing import overload

_KSTRL_PREFIX = "KSTRL_"
_LEGACY_PREFIX = "RALPH_"

_warned: set[str] = set()


def _legacy_name(name: str) -> str | None:
    if name.startswith(_KSTRL_PREFIX):
        return _LEGACY_PREFIX + name[len(_KSTRL_PREFIX):]
    return None


def _warn_once(legacy: str, name: str) -> None:
    if legacy in _warned:
        return
    _warned.add(legacy)
    warnings.warn(
        f"environment variable {legacy} is deprecated; use {name}",
        DeprecationWarning,
        stacklevel=3,
    )


@overload
def get(name: str) -> str | None: ...


@overload
def get(name: str, default: str) -> str: ...


def get(name: str, default: str | None = None) -> str | None:
    """``os.environ.get`` with legacy-name fallback."""
    if name in os.environ:
        return os.environ[name]
    legacy = _legacy_name(name)
    if legacy is not None and legacy in os.environ:
        _warn_once(legacy, name)
        return os.environ[legacy]
    return default


def contains(name: str) -> bool:
    """``name in os.environ`` with legacy-name fallback."""
    if name in os.environ:
        return True
    legacy = _legacy_name(name)
    if legacy is not None and legacy in os.environ:
        _warn_once(legacy, name)
        return True
    return False


def require(name: str) -> str:
    """``os.environ[name]`` with legacy-name fallback (KeyError if absent)."""
    value = get(name)
    if value is None:
        raise KeyError(name)
    return value
