"""The acceptance checks of features that merged earlier (#466).

Phase 3 replays the checks of the components in the current run. This
module keeps the checks of earlier features and replays them too, so a
later feature cannot break an earlier one and still pass.

- **Kept.** When a run's Phase 3 passes on the base branch after its
  components merged (one PR per component), every check the base kept for
  those components is added to :func:`carried_path`, a file under the
  control directory, outside the repository, so an engineer does not find
  a held-out check there. An entry names the plan copy by its digest, the
  component and the check id: the copy under the control directory is
  content-addressed, so the three names are the check's identity. The
  entry also records the stack digest it passed under, the run and the
  commit. A run that merges nothing adds nothing: kstrl cannot know when
  or whether its branches reach the base.
- **Replayed on the base.** Every later run replays every carried check
  once on its base, before anything is spent (:func:`carried_on_base`).
  A check that does not pass there refuses the run and files one inbox
  item for it: the earlier feature is broken on the base, or a change
  made the check obsolete, and only a person can say which. Approving the
  item retires the check (:func:`retire_approved`), which is recorded
  with who approved it and when. A changed ``[stack]`` is no special
  case: the base replay measures whether the check still passes under it.
- **Replayed in Phase 3.** The checks that passed on the base run on the
  merged commit with the run's own (:func:`replay_merged`), each
  ``HEAD_RUNS`` times and judged as a head is. A failure fails Phase 3 as
  any acceptance check does (owner decision of 2026-10-07).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git
from kstrl.acceptance import (
    ACCEPTANCE_DIR,
    FAIL,
    HEAD_RUNS,
    PASS,
    PLAN_FILE,
    Check,
    ComponentPlan,
    PinnedPlan,
    _no_log,
    _parsed,
    _probe,
    _verified,
    evidence_dir,
    head_verdict,
    limits,
    verdict_of,
)
from kstrl.atomicio import atomic_write_json
from kstrl.jsonread import read_json
from kstrl.replay import Replay, Stage, replay_stack
from kstrl.rung import Rung
from kstrl.stack import REPLAY_BOUNDARY_REFUSED
from kstrl.statedir import control_dir
from kstrl.timeout import limit_seconds

if TYPE_CHECKING:
    from kstrl.contract import ContractConfig
    from kstrl.factory import FactoryConfig
    from kstrl.inbox import InboxItem
    from kstrl.manifest import Manifest
    from kstrl.stack import Stack
    from kstrl.ui.base import UI

#: The file of carried checks, under the control directory's acceptance
#: directory, and the evidence file of one run's base replay of them.
CARRIED_FILE = "carried.json"
CARRIED_BASE_FILE = "carried-base.json"
SCHEMA_VERSION = 1

#: The keys of one carried check, and of one retired check.
CARRIED_KEYS = ("planId", "component", "check", "stackDigest", "run", "passedAt")
RETIRED_KEYS = ("planId", "component", "check", "item", "by", "at", "run")

#: The prefix of a carried check's component label in Phase 3's evidence.
LABEL_PREFIX = "carried:"


@dataclass(frozen=True)
class Carried:
    """One carried check, resolved from its plan copy."""

    plan: PinnedPlan
    component: str
    check: Check
    entry: Mapping[str, str]

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}{self.plan.digest[:12]}/{self.component}"


def carried_path(root: Path) -> Path:
    return control_dir(root) / ACCEPTANCE_DIR / CARRIED_FILE


def identity(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(entry["planId"]), str(entry["component"]), str(entry["check"]))


def _named(entry: Mapping[str, Any]) -> str:
    plan, comp, check = identity(entry)
    return f"{check} ({LABEL_PREFIX}{plan[:12]}/{comp})"


def read_carried(root: Path) -> tuple[dict[str, Any], list[str]]:
    """The carried checks, or why they cannot be read. No file is the state
    before any feature merged; a file that cannot be read is a refusal."""
    path = carried_path(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"schemaVersion": SCHEMA_VERSION, "checks": [], "retired": []}, []
    except OSError as exc:
        return {}, [f"the carried acceptance checks at {path} cannot be read: {exc}"]
    try:
        document: Any = read_json(raw)
    except ValueError as exc:
        return {}, [f"the carried acceptance checks at {path} are not JSON: {exc}"]
    errors = carried_errors(document)
    return (document, []) if not errors else ({}, [f"{path}: {line}" for line in errors])


def carried_errors(document: Any) -> list[str]:
    """Everything wrong with a parsed carried file, one indexed line each."""
    if not isinstance(document, dict) or set(document) != {"schemaVersion", "checks", "retired"}:
        return ['must be an object with the keys "schemaVersion", "checks" and "retired"']
    if document["schemaVersion"] != SCHEMA_VERSION:
        return [f"schemaVersion must be {SCHEMA_VERSION}"]
    errors: list[str] = []
    for key, keys in (("checks", CARRIED_KEYS), ("retired", RETIRED_KEYS)):
        rows = document[key]
        if not isinstance(rows, list):
            errors.append(f"{key} must be a list")
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != set(keys):
                errors.append(f"{key}[{index}] must be an object with the keys {', '.join(keys)}")
            elif not all(isinstance(row[k], str) and row[k] for k in keys):
                errors.append(f"{key}[{index}]: every value must be a non-empty string")
    return errors


def _write(root: Path, document: Mapping[str, Any]) -> str:
    path = carried_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, document)
    except OSError as exc:
        return f"the carried acceptance checks cannot be written at {path}: {exc}"
    return ""


def resolve(root: Path, entry: Mapping[str, str]) -> Carried | str:
    """The check ``entry`` names, read from its plan copy; or why it cannot be."""
    plan_id, comp, check_id = identity(entry)
    directory = control_dir(root) / ACCEPTANCE_DIR / plan_id
    try:
        files = _verified(directory, plan_id)
        raw: Any = read_json(files[PLAN_FILE][0])
        part = _parsed(raw)[comp]
        check = next(c for c in part.checks if c.id == check_id)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        return f"its plan copy at {directory} cannot be read: {exc!r}"
    plan = PinnedPlan(plan_id, directory, {comp: ComponentPlan(False, (check,))}, directory, False)
    return Carried(plan, comp, check, entry)


def retire_approved(
    root: Path, document: dict[str, Any], approved: Sequence[InboxItem], run_id: str, ui: UI
) -> str:
    """Move every carried check an approved item names to ``retired``, with
    who approved it and when; return why the file was not written, or ""."""
    named = {_item_identity(item): item for item in approved}
    kept, retired = [], list(document["retired"])
    for entry in document["checks"]:
        item = named.get(identity(entry))
        if item is None:
            kept.append(entry)
            continue
        plan, comp, check = identity(entry)
        retired.append(
            {
                "planId": plan,
                "component": comp,
                "check": check,
                "item": item.id,
                "by": item.decided_by or "unknown",
                "at": item.decided_at or "unknown",
                "run": run_id,
            }
        )
        ui.warn(f"  The carried check {_named(entry)} is retired: approval {item.id[:8]}")
    if len(kept) == len(document["checks"]):
        return ""
    document["checks"], document["retired"] = kept, retired
    return _write(root, document)


def _item_identity(item: InboxItem) -> tuple[str, str, str]:
    carried = item.evidence.get("carried")
    assert isinstance(carried, dict)
    return identity(carried)


def _all(probes: Sequence[Callable[[Path, Rung], None]]) -> Callable[[Path, Rung], None]:
    def probe(tree: Path, rung: Rung) -> None:
        for one in probes:
            one(tree, rung)

    return probe


def carried_on_base(
    root: Path,
    config: FactoryConfig,
    manifest: Manifest,
    run_id: str,
    ui: UI,
    halt: Callable[[Mapping[str, str], str, str], None],
) -> tuple[tuple[Carried, ...], list[str]]:
    """Replay every carried check once on the base: the ones that passed,
    and why the run must not start. ``halt(entry, where, detail)``
    files the inbox item of a check that did not pass; ``where`` names
    the commit."""
    from kstrl.waivers import approvals_at

    stack = config.project_stack
    document, errors = read_carried(root)
    if errors or stack is None:
        return (), errors
    snapshot = approvals_at(root)
    error = retire_approved(root, document, snapshot.retirements, run_id, ui)
    if error:
        return (), [error]
    if not document["checks"]:
        return (), []
    try:
        sha = git.resolve_base_sha(manifest.base_branch, root)
    except git.GitDiffError as exc:
        return (), [f"the base branch {manifest.base_branch} did not resolve: {exc}"]
    resolved = [(entry, resolve(root, entry)) for entry in document["checks"]]
    runnable = [c for _, c in resolved if isinstance(c, Carried)]
    ui.info(f"  Replaying {len(runnable)} carried acceptance checks on the base {sha[:12]}...")
    runs: dict[tuple[str, str], list[Stage]] = {}
    setup_limit, check_limit = limits(config)
    record = replay_stack(
        root,
        stack,
        setup_limit=setup_limit,
        check_limit=check_limit,
        ui=ui,
        at=sha,
        probe=_all(
            [
                _probe(c.plan, [(c.label, c.check)], stack, check_limit, runs, 1, _no_log)
                for c in runnable
            ]
        ),
    )
    passed, refused = _base_verdicts(resolved, runs, record, stack, sha, halt)
    if refused and snapshot.unconsulted_reason:
        refused.append(f"No approval was read: {snapshot.unconsulted_reason}")
    write_error = _write_base(root, run_id, sha, record, resolved, runs, refused)
    return passed, refused + ([write_error] if write_error else [])


def _base_verdicts(
    resolved: Sequence[tuple[Mapping[str, str], Carried | str]],
    runs: Mapping[tuple[str, str], Sequence[Stage]],
    record: Replay,
    stack: Stack,
    sha: str,
    halt: Callable[[Mapping[str, str], str, str], None],
) -> tuple[tuple[Carried, ...], list[str]]:
    if record.error or record.failed == REPLAY_BOUNDARY_REFUSED:
        said = record.error or record.detail
        return (), [f"the carried acceptance checks could not be replayed on the base: {said}"]
    passed: list[Carried] = []
    refused: list[str] = []
    for entry, carried in resolved:
        detail = _base_detail(carried, runs)
        if not detail:
            assert isinstance(carried, Carried)
            passed.append(carried)
            continue
        if entry["stackDigest"] != stack.digest:
            detail += f"; it passed under the stack {entry['stackDigest'][:12]}, not this one"
        refused.append(
            f"the carried check {_named(entry)} does not pass on the base {sha[:12]} ({detail}). "
            "Repair the base, or approve its inbox item to retire the check."
        )
        halt(entry, f"the base {sha[:12]}", detail)
    return tuple(passed), refused


def _stages(
    carried: Carried | str, runs: Mapping[tuple[str, str], Sequence[Stage]]
) -> Sequence[Stage]:
    return [] if isinstance(carried, str) else runs.get((carried.label, carried.check.id), [])


def _base_detail(carried: Carried | str, runs: Mapping[tuple[str, str], Sequence[Stage]]) -> str:
    """Why ``carried`` did not pass its base run, or "" when it passed."""
    if isinstance(carried, str):
        return carried
    stages = _stages(carried, runs)
    code = stages[0].exit if stages else None
    if verdict_of(code) == PASS:
        return ""
    return f"exit {code}" if code is not None else "no exit"


def _base_row(
    entry: Mapping[str, str],
    carried: Carried | str,
    runs: Mapping[tuple[str, str], Sequence[Stage]],
) -> dict[str, Any]:
    """One carried check's base run, as the evidence keeps it. A held-out
    check's output is not kept: it can say what the check expects."""
    stages = _stages(carried, runs)
    shown = not isinstance(carried, str) and not carried.check.held_out
    return {
        **dict(entry),
        "exit": stages[0].exit if stages else None,
        "seconds": stages[0].seconds if stages else None,
        "tail": [line for stage in stages for line in stage.tail] if shown else [],
    }


def _write_base(
    root: Path,
    run_id: str,
    sha: str,
    record: Replay,
    resolved: Sequence[tuple[Mapping[str, str], Carried | str]],
    runs: Mapping[tuple[str, str], Sequence[Stage]],
    refused: list[str],
) -> str:
    path = evidence_dir(root, run_id) / CARRIED_BASE_FILE
    document = {
        "run": run_id,
        "baseSha": sha,
        "stackDigest": record.stack_digest,
        "isolation": record.isolation,
        "seconds": record.seconds,
        "replay": {"failed": record.failed, "detail": record.detail, "error": record.error},
        "checks": [_base_row(entry, carried, runs) for entry, carried in resolved],
        "refused": refused,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, document)
    except OSError as exc:
        return f"the carried base reading cannot be written at {path}: {exc}"
    return ""


def carry_passed(
    root: Path,
    config: FactoryConfig,
    manifest: Manifest,
    merged: Sequence[str],
    sha: str,
    run_id: str,
) -> str:
    """Add the kept checks of the ``merged`` components to the carried
    file, after Phase 3 passed on ``sha``; return why it was not written."""
    from kstrl.factory import _has_merged

    plan, base, stack = config.acceptance_plan, config.acceptance_base, config.project_stack
    if plan is None or base is None or stack is None or not sha:
        return ""
    document, errors = read_carried(root)
    if errors:
        return errors[0]
    seen = {identity(entry) for entry in document["checks"]}
    added = 0
    for comp_id in merged:
        comp = manifest.get_component(comp_id)
        if comp is None or not _has_merged(comp) or comp_id not in base.kept:
            continue
        for check in base.kept[comp_id].checks:
            if (plan.digest, comp_id, check.id) in seen:
                continue
            document["checks"].append(
                {
                    "planId": plan.digest,
                    "component": comp_id,
                    "check": check.id,
                    "stackDigest": stack.digest,
                    "run": run_id,
                    "passedAt": sha,
                }
            )
            added += 1
    return _write(root, document) if added else ""


# --- Phase 3 ---------------------------------------------------------------


def replay_merged(
    cwd: Path,
    config: ContractConfig,
    root_dir: Path,
    ui: UI,
    plan: PinnedPlan | None,
    merged: Sequence[str],
    carried: Sequence[Carried],
) -> tuple[bool, list[str], tuple[str, ...], tuple[Mapping[str, str], ...]]:
    """Replay the ``merged`` components' checks and every ``carried`` check on
    the commit ``cwd`` holds, each ``HEAD_RUNS`` times and judged as a
    head is, whoever wrote the plan (owner decision of 2026-10-07). It
    replays only the checks the base kept: a designed check the base
    removed runs on no head and does not run here. The last value is the
    entry of each carried check that did not pass."""
    stack = config.project_stack
    groups = [(plan, _wanted(plan, merged))] if plan is not None else []
    groups += [(c.plan, [(c.label, c.check)]) for c in carried]
    groups = [(p, w) for p, w in groups if w]
    if stack is None or not groups:
        return True, [], (), ()
    sha = git.get_head_sha(cwd) or ""
    if not sha:
        return False, [f"acceptance: the commit of {cwd} cannot be read, so no check ran"], (), ()
    limit = limit_seconds(config.timeout)
    runs: dict[tuple[str, str], list[Stage]] = {}
    record = replay_stack(
        root_dir,
        stack,
        setup_limit=limit,
        check_limit=limit,
        ui=ui,
        at=sha,
        probe=_all([_probe(p, w, stack, limit, runs, HEAD_RUNS, _no_log) for p, w in groups]),
    )
    failing, held_out = _failing_lines([item for _, w in groups for item in w], runs)
    if not failing:
        return True, [], (), ()
    stopped = record.error or (f"{record.failed}: {record.detail}" if record.failed else "")
    lines = [f"acceptance replay at {sha[:12]} stopped: {stopped}"] if stopped else []
    lines += failing
    broken = tuple(c.entry for c in carried if _verdict(c, runs) != PASS)
    return False, lines, held_out, broken


def _verdict(carried: Carried, runs: Mapping[tuple[str, str], Sequence[Stage]]) -> str:
    return head_verdict([stage.exit for stage in _stages(carried, runs)])


def _wanted(plan: PinnedPlan | None, merged: Sequence[str]) -> list[tuple[str, Check]]:
    """The checks of the components in ``merged``; no other component's."""
    if plan is None:
        return []
    return [
        (comp, check)
        for comp in merged
        if comp in plan.components
        for check in plan.components[comp].checks
    ]


def _failing_lines(
    wanted: Sequence[tuple[str, Check]], runs: Mapping[tuple[str, str], Sequence[Stage]]
) -> tuple[list[str], tuple[str, ...]]:
    """One line per check that did not pass all its runs, followed by what
    its last run printed, and the ids of the held-out checks that failed. A
    held-out check is named by its id alone: this evidence is printed."""
    lines: list[str] = []
    held_out: list[str] = []
    for comp, check in wanted:
        stages = runs.get((comp, check.id), [])
        exits = [stage.exit for stage in stages]
        verdict = head_verdict(exits)
        if verdict == PASS:
            continue
        lines.append(f"acceptance:{check.id} ({comp}): {verdict}, exits {exits}")
        if check.held_out and verdict == FAIL:
            held_out.append(check.id)
        if not check.held_out and stages:
            lines.extend(stages[-1].tail)
    return lines, tuple(held_out)
