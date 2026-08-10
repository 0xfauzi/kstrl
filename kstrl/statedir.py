"""Runtime state-dir resolution: artifacts in-tree, control state outside.

Journals, worktrees, queue items, locks, and evidence stay under
``<root>/.kstrl/`` (the agent-reachable audit/artifact tree). Control-plane
files that govern what the factory may do without a human - autonomy level,
spend ledger, pause marker, inbox, GitHub processed-ids - live under the
XDG state directory:

    ${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/

Clones that share the same ``origin`` remote share one control directory
(deliberate: one autonomy level and one daily spend ledger per project).
Do not run two ``ks serve`` daemons against that shared ledger concurrently
without holding ``control.lock``. Checkouts with no ``origin`` remote get a
path-hashed id and stay checkout-local.

R8.9 / #194.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

STATE_DIR_NAME = ".kstrl"
CONTROL_APP_NAME = "kstrl"
CONTROL_LOCK_FILENAME = "control.lock"
CONTROL_RELOCATED_MARKER = "control_relocated"

#: Flat filenames under the XDG control directory (not nested under queue/).
CONTROL_AUTONOMY = "autonomy.json"
CONTROL_INBOX = "inbox.jsonl"
CONTROL_SPEND = "spend.json"
CONTROL_PAUSE = "pause.json"
CONTROL_GITHUB_PROCESSED = "github_processed.json"

CONTROL_FILENAMES: tuple[str, ...] = (
    CONTROL_AUTONOMY,
    CONTROL_INBOX,
    CONTROL_SPEND,
    CONTROL_PAUSE,
    CONTROL_GITHUB_PROCESSED,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def state_dir(root_dir: Path) -> Path:
    """Return the in-tree artifact/lock directory for ``root_dir``."""
    return root_dir / STATE_DIR_NAME


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_remote_url(url: str) -> str:
    """Canonicalize a git remote URL for stable hashing.

    Strips trailing ``.git``, lowercases, and maps ``git@host:path`` /
    ``ssh://git@host/path`` forms to ``host/path``.
    """
    raw = url.strip()
    if not raw:
        return ""
    value = raw
    if value.startswith("git@"):
        # git@github.com:org/repo.git -> github.com/org/repo
        rest = value[len("git@"):]
        if ":" in rest:
            host, path = rest.split(":", 1)
            value = f"{host}/{path}"
    elif "://" in value:
        # https://github.com/org/repo.git or ssh://git@github.com/org/repo
        _, _, remainder = value.partition("://")
        if remainder.startswith("git@"):
            remainder = remainder[len("git@"):]
        value = remainder
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[: -len(".git")]
    return value.lower()


def _origin_url(root_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root_dir), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _slug_from_identity(identity: str) -> str:
    base = identity.rsplit("/", 1)[-1] if identity else "repo"
    slug = _SLUG_RE.sub("-", base.lower()).strip("-")
    if not slug:
        slug = "repo"
    return slug[:32]


def repo_id(root_dir: Path) -> str:
    """Stable control-dir id for ``root_dir``.

    Prefer a hash of the normalized ``origin`` URL so every clone of the
    same remote shares control state. With no origin, hash the resolved
    absolute path (checkout-local).
    """
    origin = _origin_url(root_dir)
    if origin:
        identity = normalize_remote_url(origin)
        source = "origin"
    else:
        try:
            identity = str(root_dir.resolve())
        except OSError:
            identity = str(root_dir)
        source = "path"
    digest = hashlib.sha256(f"{source}:{identity}".encode()).hexdigest()[:16]
    return f"{_slug_from_identity(identity)}-{digest}"


def xdg_state_home() -> Path:
    """XDG state home, overridable via ``XDG_STATE_HOME``."""
    override = os.environ.get("XDG_STATE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state"


def control_dir(root_dir: Path) -> Path:
    """XDG control directory for ``root_dir`` (outside the agent tree)."""
    return xdg_state_home() / CONTROL_APP_NAME / repo_id(root_dir)


def control_file(root_dir: Path, name: str) -> Path:
    """Path to a named control file under the XDG control directory."""
    if name not in CONTROL_FILENAMES:
        raise ValueError(f"unknown control file {name!r}; expected one of {CONTROL_FILENAMES}")
    return control_dir(root_dir) / name


def legacy_control_paths(root_dir: Path) -> dict[str, Path]:
    """Former in-tree locations for each control file (migration + halt set)."""
    queue = state_dir(root_dir) / "queue"
    return {
        CONTROL_AUTONOMY: state_dir(root_dir) / CONTROL_AUTONOMY,
        CONTROL_INBOX: state_dir(root_dir) / CONTROL_INBOX,
        CONTROL_SPEND: queue / CONTROL_SPEND,
        CONTROL_PAUSE: queue / CONTROL_PAUSE,
        CONTROL_GITHUB_PROCESSED: queue / CONTROL_GITHUB_PROCESSED,
    }


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def control_dir_accessible(root_dir: Path) -> bool:
    """Whether the control directory can be created and listed.

    Used by fail-closed pause: an inaccessible control plane must not
    look like "no pause marker → running".
    """
    try:
        target = control_dir(root_dir)
        target.mkdir(parents=True, exist_ok=True)
        # Probe readability; ignore the listing itself.
        list(target.iterdir())
        return True
    except OSError:
        return False


def control_is_external(root_dir: Path) -> bool:
    """True when live control state is outside the agent-reachable tree.

    False when the control dir resolves under ``root_dir`` (mis-set
    ``XDG_STATE_HOME``), when the control dir is inaccessible, or when
    any legacy in-tree control file still exists (migration incomplete -
    an agent can still edit the leftover).
    """
    if not control_dir_accessible(root_dir):
        return False
    try:
        if _is_under(control_dir(root_dir), root_dir):
            return False
    except OSError:
        return False
    for legacy in legacy_control_paths(root_dir).values():
        try:
            if legacy.exists():
                return False
        except OSError:
            return False
    return True


def _write_relocated_marker(root_dir: Path, *, moved: list[str]) -> None:
    marker = state_dir(root_dir) / CONTROL_RELOCATED_MARKER
    try:
        state_dir(root_dir).mkdir(parents=True, exist_ok=True)
        payload = {
            "repo_id": repo_id(root_dir),
            "control_dir": str(control_dir(root_dir)),
            "migrated_at": _utc_now_iso(),
            "moved": moved,
        }
        marker.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Marker is operator convenience, not load-bearing.
        pass


def migrate_control_state(root_dir: Path) -> list[str]:
    """Move legacy in-tree control files into the XDG control dir once.

    Returns the list of filenames moved. Idempotent: a second call is a
    no-op when targets already exist or legacy files are gone.
    """
    if not control_dir_accessible(root_dir):
        return []
    target_root = control_dir(root_dir)
    moved: list[str] = []
    legacy = legacy_control_paths(root_dir)
    for name in CONTROL_FILENAMES:
        src = legacy[name]
        dst = target_root / name
        try:
            if dst.exists() or not src.exists():
                continue
        except OSError:
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
        except OSError as exc:
            warnings.warn(
                f"kstrl: failed to migrate control file {src} -> {dst}: {exc}",
                stacklevel=2,
            )
            continue
        moved.append(name)
        warnings.warn(
            f"kstrl: relocated control file {name} from {src} to {dst} "
            "(R8.9; legacy in-tree path is no longer written)",
            DeprecationWarning,
            stacklevel=2,
        )
    if moved:
        _write_relocated_marker(root_dir, moved=moved)
    return moved


def ensure_control_state(root_dir: Path) -> Path:
    """Ensure the XDG control dir exists and migrate any legacy files.

    Call at the start of every control read/write path so CLI and daemon
    both migrate before touching state.
    """
    migrate_control_state(root_dir)
    target = control_dir(root_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


@contextmanager
def control_lock(root_dir: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold the cross-checkout mutex on the XDG control directory.

    Serializes spend / pause / autonomy / inbox / GitHub-ledger writes so
    two checkouts sharing an origin cannot corrupt the shared ledger.
    POSIX ``fcntl`` only; without it we degrade to no exclusion (same
    pattern as ``queue_lock`` / factory lock).
    """
    ensure_control_state(root_dir)
    lock_path = control_dir(root_dir) / CONTROL_LOCK_FILENAME
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield
        return
    try:
        import fcntl
    except ImportError:
        yield
        return

    handle: IO[str] = open(lock_path, "a+")
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            raise ControlLockedError(
                f"control state is locked by another process ({lock_path})"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class ControlLockedError(RuntimeError):
    """Another process holds ``control.lock``."""
