"""New dependencies, read from each lockfile's documents (#630).

The ``[policy]`` rules ``deps_allow_new`` and the license gate need the
packages a change ADDS. ``uv.lock`` is read from the diff's added lines
by :func:`parse_new_dependencies`, unchanged since R8.1. Every other
lockfile kstrl has a reader for is read whole, at the merge base and at
HEAD, and a package is new when its name is at HEAD and not at the base.
Comparing documents rather than added lines is what makes a reformatted
``package-lock.json`` read as no change, and a lockfile a ``-diff``
attribute hides from ``git diff`` read at all.

A new name only: a second version of a package the base already has is
not new, and every transitive package counts.

Pure: the blobs are read by ``kstrl.verify`` through ``git.read_blob``
and handed in as :class:`LockfileDocument`. A lockfile this module cannot
read is returned with its reason, never as "no new dependencies" (#619).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from kstrl.config_toml import parse_toml_bytes
from kstrl.jsonread import read_json

#: The package registry a lockfile's names belong to. A license is looked
#: up in its own ecosystem only (``licensing.resolve_license``).
Ecosystem = Literal["pypi", "cargo", "npm", "go"]


@dataclass(frozen=True)
class NewDependency:
    """One package a change adds, and the lockfile that pins it."""

    lockfile: str
    name: str
    version: str
    ecosystem: Ecosystem


@dataclass(frozen=True)
class LockfileDocument:
    """One lockfile at the merge base and at HEAD.

    ``None`` means the file is absent at that revision. A non-empty
    ``error`` means it could not be read from git, and the lockfile is
    reported unread with that reason.
    """

    base: bytes | None
    head: bytes | None
    error: str = ""


class LockfileShapeError(ValueError):
    """A lockfile that parsed but is not a shape this module reads."""


#: ``(name, version)`` for every package a lockfile document pins.
Reader = Callable[[bytes], set[tuple[str, str]]]

_UVLOCK_NAME_RE = re.compile(r'^name = "([^"]+)"')
_UVLOCK_VERSION_RE = re.compile(r'^version = "([^"]+)"')


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def parse_new_dependencies(
    added_lines: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """``(name, version)`` for packages newly added to ``uv.lock``.

    A new ``[[package]]`` stanza adds a column-0 ``name = "..."`` line
    immediately followed by ``version = "..."``; a version bump of an
    existing package adds only the ``version`` line (its name line is
    unchanged context), so pairing an added name with the next added
    version isolates genuinely new packages. Inline dependency refs
    (``{ name = "x" }``) are indented and never match the column-0 anchor.
    """
    deps: list[tuple[str, str]] = []
    pending: str | None = None
    for path, line in added_lines:
        if _basename(path) != "uv.lock":
            continue
        name_match = _UVLOCK_NAME_RE.match(line)
        if name_match:
            pending = name_match.group(1)
            continue
        version_match = _UVLOCK_VERSION_RE.match(line)
        if version_match and pending is not None:
            deps.append((pending, version_match.group(1)))
            pending = None
    return deps


def uv_lock_dependencies(added_lines: Sequence[tuple[str, str]]) -> list[NewDependency]:
    """:func:`parse_new_dependencies` as :class:`NewDependency` rows."""
    return [
        NewDependency("uv.lock", name, version, "pypi")
        for name, version in parse_new_dependencies(added_lines)
    ]


def _toml_packages(raw: bytes, label: str, *, registry_only: bool) -> set[tuple[str, str]]:
    """The ``[[package]]`` stanzas of a Cargo.lock or poetry.lock."""
    stanzas = parse_toml_bytes(raw, label).get("package", [])
    if not isinstance(stanzas, list):
        raise LockfileShapeError("[[package]] is not an array of tables")
    found: set[tuple[str, str]] = set()
    for index, stanza in enumerate(stanzas):
        if not isinstance(stanza, dict):
            raise LockfileShapeError(f"package[{index}] is not a table")
        name, version = stanza.get("name"), stanza.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise LockfileShapeError(f"package[{index}] has no string name and version")
        if registry_only and "source" not in stanza:
            continue  # the root crate, a workspace member or a path crate
        found.add((name, version))
    return found


def read_cargo_lock(raw: bytes) -> set[tuple[str, str]]:
    return _toml_packages(raw, "Cargo.lock", registry_only=True)


def read_poetry_lock(raw: bytes) -> set[tuple[str, str]]:
    return _toml_packages(raw, "poetry.lock", registry_only=False)


def read_package_lock(raw: bytes) -> set[tuple[str, str]]:
    """``package-lock.json`` lockfileVersion 2 or 3: the ``packages`` map."""
    document = read_json(raw)
    if not isinstance(document, dict):
        raise LockfileShapeError("the top level is not an object")
    version = document.get("lockfileVersion")
    if version not in (2, 3):
        raise LockfileShapeError(f"lockfileVersion {version!r} is not read; kstrl reads 2 and 3")
    packages = document.get("packages")
    if not isinstance(packages, dict):
        raise LockfileShapeError("packages is not an object")
    found: set[tuple[str, str]] = set()
    for key, entry in packages.items():
        if not isinstance(entry, dict):
            raise LockfileShapeError(f"packages[{key!r}] is not an object")
        if "node_modules/" not in key or entry.get("link") is True:
            continue  # the root project, a workspace member, or a link to one
        name = entry.get("name", key.rsplit("node_modules/", 1)[1])
        version = entry.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise LockfileShapeError(f"packages[{key!r}] has no string name and version")
        found.add((name, version))
    return found


def _yarn_entry_name(line: str, number: int) -> str:
    """``left-pad`` from ``left-pad@1.3.0:`` or ``"@s/is@^4.6.0", "@s/is@^4":``."""
    first = line.removesuffix(":").split(",", 1)[0].strip().strip('"')
    at = first.rfind("@")
    if not line.endswith(":") or at <= 0:
        raise LockfileShapeError(f"line {number} is not a 'name@range:' entry")
    return first[:at]


def read_yarn_lock(raw: bytes) -> set[tuple[str, str]]:
    """``yarn.lock`` v1. A yarn 2+ (Berry) lockfile is YAML and refused."""
    text = raw.decode("utf-8")
    if "__metadata:" in text or "# yarn lockfile v1" not in text:
        raise LockfileShapeError("not a '# yarn lockfile v1' file; kstrl reads yarn v1 only")
    found: set[tuple[str, str]] = set()
    name: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" "):
            if name is not None:
                raise LockfileShapeError(f"the entry before line {number} has no version")
            name = _yarn_entry_name(line, number)
        elif line.startswith("  version ") and name is not None:
            found.add((name, line.split(None, 1)[1].strip('"')))
            name = None
    if name is not None:
        raise LockfileShapeError("the last entry has no version")
    return found


def read_go_sum(raw: bytes) -> set[tuple[str, str]]:
    """``go.sum``: a module counts once per version, ``/go.mod`` line or not."""
    found: set[tuple[str, str]] = set()
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3 or not fields[2].startswith("h1:"):
            raise LockfileShapeError(f"line {number} is not 'module version h1:hash'")
        found.add((fields[0], fields[1].removesuffix("/go.mod")))
    return found


#: Every lockfile basename but ``uv.lock``, which :func:`parse_new_dependencies`
#: reads. ``None`` means kstrl has no reader, and a changed lockfile of that
#: name is reported unread. ``kstrl.policy`` refuses to import unless these
#: keys and ``uv.lock`` are exactly ``LOCKFILE_MANIFESTS``' keys, so a new
#: lockfile row cannot arrive without a reader decision.
LOCKFILE_READERS: dict[str, tuple[Ecosystem, Reader] | None] = {
    "poetry.lock": ("pypi", read_poetry_lock),
    "Pipfile.lock": None,
    "package-lock.json": ("npm", read_package_lock),
    "yarn.lock": ("npm", read_yarn_lock),
    "pnpm-lock.yaml": None,
    "Cargo.lock": ("cargo", read_cargo_lock),
    "go.sum": ("go", read_go_sum),
    "composer.lock": None,
    "Gemfile.lock": None,
}


#: A changed lockfile the verifier handed no document for.
_NOT_READ = LockfileDocument(None, None, "the verifier did not read it")


def _packages(reader: Reader, raw: bytes | None, side: str) -> set[tuple[str, str]]:
    """Read one side; an absent file pins nothing."""
    if raw is None:
        return set()
    try:
        return reader(raw)
    except Exception as exc:
        # Exception exactly, broad clause last (#318, #427): the parsers'
        # taxonomy is theirs, and anything they raise makes this lockfile
        # unread, never a lockfile with no packages in it.
        raise LockfileShapeError(
            f"the {side} copy could not be read ({type(exc).__name__}: {exc})"
        ) from exc


def _new_in(path: str, document: LockfileDocument) -> list[NewDependency]:
    """The packages ``path`` names at HEAD and not at the base.

    Raises :class:`LockfileShapeError` carrying the reason when the
    lockfile is unread. An absent base makes every package new; an
    absent HEAD makes none new.
    """
    row = LOCKFILE_READERS[_basename(path)]
    if row is None:
        raise LockfileShapeError(f"kstrl has no reader for {_basename(path)}")
    if document.error:
        raise LockfileShapeError(document.error)
    ecosystem, reader = row
    head = _packages(reader, document.head, "HEAD")
    base = {name for name, _version in _packages(reader, document.base, "merge base")}
    return [
        NewDependency(path, name, version, ecosystem)
        for name, version in sorted(head)
        if name not in base
    ]


def read_new_dependencies(
    changed_files: Sequence[str],
    documents: Mapping[str, LockfileDocument],
) -> tuple[list[NewDependency], dict[str, str]]:
    """``(new dependencies, {unread lockfile: reason})`` over every changed
    lockfile but ``uv.lock``.

    Selected from ``changed_files``, never from the diff's added lines, so
    a lockfile ``git diff`` shows as binary is still read. A changed
    lockfile missing from ``documents`` is unread, never clean.
    """
    new: list[NewDependency] = []
    unread: dict[str, str] = {}
    for path in sorted({p for p in changed_files if _basename(p) in LOCKFILE_READERS}):
        try:
            new += _new_in(path, documents.get(path, _NOT_READ))
        except LockfileShapeError as exc:
            unread[path] = str(exc)
    return new, unread
