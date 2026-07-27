"""R8.1 license resolution for the policy envelope's license gate.

Measured constraint (do not assume otherwise): under this project's uv
toolchain the INSTALLED venv carries no ``METADATA`` file - dist-info
dirs hold only an empty ``licenses/`` folder, and both
``importlib.metadata`` and ``uv pip show`` return nothing. So the
standard "read installed metadata / pip-licenses" approach cannot
resolve a dependency's license here.

License data does live in two places, used in this order:

1. **uv's cache** (``<uv cache>/**/<name>-<version>.dist-info/METADATA``),
   which carries the full core metadata (``License-Expression`` for
   modern PEP 639 wheels, else ``License`` / ``Classifier`` lines).
   Offline, doctrine-aligned.
2. **PyPI's JSON API** (``/pypi/<name>/<version>/json``) as a fallback
   on a cache miss. Network, short-timeout, best-effort; a failure here
   degrades to "unresolved" (advisory), never a hard error.

Resolution returns a best-effort SPDX-ish string; the pure allow/deny
classification lives in :mod:`kstrl.policy`. Everything here is I/O and
is injectable (``uv_cache``, ``http_get``) so tests need neither
network nor a real uv cache.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
from collections.abc import Callable
from email.parser import Parser
from pathlib import Path

# The default network fetcher's timeout (seconds). PyPI is a fallback, so
# keep it short: a slow/unreachable index must not stall the verifier.
_PYPI_TIMEOUT = 8.0

# Trailing classifier name -> SPDX id. Older wheels express their license
# only through ``Classifier: License :: OSI Approved :: <name>`` lines.
# GPL/LGPL/AGPL map to tokens that still contain "GPL", so a
# ``license_deny_partial`` of ["GPL", ...] catches them via substring.
CLASSIFIER_SPDX: dict[str, str] = {
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT-0",
    "BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Mozilla Public License 1.1 (MPL 1.1)": "MPL-1.1",
    "GNU General Public License (GPL)": "GPL-1.0-or-later",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
    "GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.0-only",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "GNU Affero General Public License v3": "AGPL-3.0-only",
    "GNU Affero General Public License v3 or later (AGPL v3+)": "AGPL-3.0-or-later",
}

HttpGet = Callable[[str, float], bytes]


def _map_classifiers(classifiers: list[str]) -> str | None:
    """Map ``License ::`` classifier lines to a best-effort SPDX string.

    Multiple distinct licenses join with ``OR`` (the common dual-license
    intent). Returns None when nothing maps.
    """
    mapped: list[str] = []
    for line in classifiers:
        name = line.split("::")[-1].strip()
        spdx = CLASSIFIER_SPDX.get(name)
        if spdx and spdx not in mapped:
            mapped.append(spdx)
    if not mapped:
        return None
    return " OR ".join(mapped)


def license_from_metadata_text(text: str) -> str | None:
    """Extract a best-effort SPDX-ish license from core-metadata text.

    Priority: ``License-Expression`` (exact SPDX) > mapped classifiers >
    the raw ``License`` field. Returns None when none are present.
    """
    md = Parser().parsestr(text, headersonly=True)
    expr = md.get("License-Expression")
    if expr and expr.strip():
        return expr.strip()
    classifiers = [
        c for c in (md.get_all("Classifier") or []) if c.startswith("License ::")
    ]
    mapped = _map_classifiers(classifiers)
    if mapped:
        return mapped
    field = md.get("License")
    if field and field.strip():
        # Single-line SPDX-ish values only ("MIT", "MPL-2.0"); a multi-line
        # License field is the full license TEXT, which is not an id.
        cleaned = field.strip()
        if "\n" not in cleaned and len(cleaned) <= 60:
            return cleaned
    return None


def _name_variants(name: str) -> list[str]:
    """Distribution-name spellings a dist-info dir might use."""
    low = name.lower()
    return list(dict.fromkeys([name, low, low.replace("-", "_"), low.replace("_", "-")]))


def uv_cache_dir() -> Path | None:
    """Locate uv's cache dir: ``UV_CACHE_DIR``, then ``uv cache dir``."""
    env = os.environ.get("UV_CACHE_DIR")
    if env:
        return Path(env)
    try:
        result = subprocess.run(
            ["uv", "cache", "dir"],
            capture_output=True, text=True, timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


def resolve_from_uv_cache(
    name: str, version: str, cache_dir: Path | None,
) -> str | None:
    """Read ``<name>-<version>.dist-info/METADATA`` from uv's cache."""
    if cache_dir is None or not cache_dir.exists():
        return None
    for variant in _name_variants(name):
        pattern = str(cache_dir / "**" / f"{variant}-{version}.dist-info" / "METADATA")
        for match in glob.iglob(pattern, recursive=True):
            try:
                text = Path(match).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            resolved = license_from_metadata_text(text)
            if resolved:
                return resolved
    return None


def _default_http_get(url: str, timeout: float) -> bytes:
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        data: bytes = resp.read()
        return data


def resolve_from_pypi(
    name: str, version: str, http_get: HttpGet | None = None,
) -> str | None:
    """Resolve a license from PyPI's JSON API (best-effort, network)."""
    fetch = http_get or _default_http_get
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        raw = fetch(url, _PYPI_TIMEOUT)
        info = json.loads(raw).get("info", {})
    except Exception:  # noqa: BLE001 - network/parse failure degrades to unresolved
        return None
    expr = info.get("license_expression")
    if expr and str(expr).strip():
        return str(expr).strip()
    classifiers = [
        c for c in info.get("classifiers", []) if str(c).startswith("License ::")
    ]
    mapped = _map_classifiers(classifiers)
    if mapped:
        return mapped
    field = info.get("license")
    if field and str(field).strip():
        cleaned = str(field).strip()
        if "\n" not in cleaned and len(cleaned) <= 60:
            return cleaned
    return None


def resolve_license(
    name: str,
    version: str,
    *,
    uv_cache: Path | None = None,
    use_pypi: bool = True,
    http_get: HttpGet | None = None,
) -> str | None:
    """Best-effort SPDX license for ``name==version``: uv cache, then PyPI.

    Returns None ("unresolved") when neither source yields a license -
    the caller treats that as advisory, never a hard failure.
    """
    resolved = resolve_from_uv_cache(name, version, uv_cache)
    if resolved:
        return resolved
    if use_pypi:
        return resolve_from_pypi(name, version, http_get)
    return None
