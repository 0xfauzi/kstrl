"""Detached throwaway worktrees for the contract, base and replay checks.

``create_temp_worktree`` cuts a worktree under ``.kstrl/contract/`` with
``git worktree add --detach`` and ``remove_temp_worktree`` removes it. If a
worktree survives removal, :class:`ContractCleanupError` is raised so the
run fails loudly instead of leaving stale state behind (R0.3 / CRIT-6).
"""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.git import git_base
from kstrl.scrubbed_run import ChildOutputDecodeError, run_scrubbed
from kstrl.worktree_sweep import sweep_worktree, warn_sweep

if TYPE_CHECKING:
    from kstrl.ui import UI


class ContractCleanupError(RuntimeError):
    """A contract temp worktree could not be removed.

    Raised so the factory fails loudly: a surviving temp worktree means
    ``.kstrl/contract/`` holds stale state and git's worktree metadata
    still references it, which would make every later contract pass
    (and possibly component worktree setup) fail in confusing ways.
    """


def create_temp_worktree(
    base: str,
    root_dir: Path,
    label: str,
    timeout: float = 60.0,
) -> tuple[Path | None, str]:
    """Create a detached throwaway worktree at ``base``.

    Returns ``(path, "")`` on success or ``(None, error)`` on failure.
    Detached HEAD means merges move only the temp worktree's HEAD; no
    branch is created and the user's checkout is never touched.

    The base is resolved through :func:`git.resolve_base_ref`, here
    rather than at the three call sites, so every contract checkout
    (bisection, the tier check, the integrated check) asks one question.
    A bare base name cuts from the operator's local branch, which a
    squash merge on GitHub leaves behind; #435 recorded a tier-1 contract
    check failing in 0.75s with ``Installed 28 packages in 23ms`` as the
    whole of its evidence, because the tree it checked out had no tests
    in it at all.
    """
    contract_base = root_dir / ".kstrl" / "contract"
    contract_base.mkdir(parents=True, exist_ok=True)
    worktree_path = contract_base / f"{label}-{secrets.token_hex(4)}"
    base_ref = git_base.resolve_base_ref(base, root_dir, timeout)
    try:
        result = run_scrubbed(
            ["git", "worktree", "add", "--detach", str(worktree_path), base_ref],
            cwd=root_dir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"git worktree add timed out after {timeout}s"
    except ChildOutputDecodeError as exc:
        return None, f"git worktree add output could not be decoded: {exc}"
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip()
    return worktree_path, ""


def remove_temp_worktree(
    worktree_path: Path,
    root_dir: Path,
    ui: UI,
    phase: str,
    timeout: float = 60.0,
) -> None:
    """Remove a contract temp worktree, asserting the removal succeeded.

    First kills every process still running in it and warns on ``ui``
    naming each one under ``phase`` (#528): the project's test command and
    the integration reviewer's shell both run here, and a process either of
    them starts in a session of its own outlives the call that started it.

    Raises :class:`ContractCleanupError` when the worktree directory
    survives the forced removal - the one case where silent continuation
    would leave the repo's worktree metadata pointing at stale state.
    """
    warn_sweep(sweep_worktree(worktree_path), ui, phase)
    try:
        result = run_scrubbed(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=root_dir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContractCleanupError(
            f"git worktree remove timed out after {timeout}s for "
            f"{worktree_path}; remove it manually with "
            f"'git worktree remove --force {worktree_path}'"
        ) from exc
    except ChildOutputDecodeError as exc:
        raise ContractCleanupError(
            f"git worktree remove output could not be decoded for "
            f"{worktree_path} ({exc}); check it and remove it manually with "
            f"'git worktree remove --force {worktree_path}'"
        ) from exc
    if worktree_path.exists():
        raise ContractCleanupError(
            f"Contract temp worktree {worktree_path} survived removal "
            f"(git: {result.stderr.strip() or result.stdout.strip()}); "
            f"remove it manually with "
            f"'git worktree remove --force {worktree_path}'"
        )
    if result.returncode != 0:
        # Directory is gone but git may still track it; prune metadata.
        try:
            run_scrubbed(
                ["git", "worktree", "prune"],
                cwd=root_dir,
                timeout=timeout,
            )
        except ChildOutputDecodeError:
            # This spawn's result was never read: the directory is already
            # gone and the prune is best-effort metadata tidying. A decode
            # failure carries nothing the caller acts on, for the same reason
            # _abort_merge's timeout does not block removal.
            pass
