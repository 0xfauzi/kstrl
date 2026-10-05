"""The acceptance runner over operator-written plans, record-only (#700 slice 4).

``ks factory --acceptance <dir>`` names a directory outside the repository
that holds ``plan.json`` and the files its checks use. The plan names, per
component, the checks that decide whether the component does what its
criteria say. kstrl never interprets a check: it runs its ``argv`` and
reads the exit status. Exit 0 passes and any other completed exit fails;
126, 127, a timeout or output that is not utf-8 did not run, and a check
that did not run is never read as a failure.

The plan is read whole, entry by entry against the vocabulary below, and
copied into the control directory under its digest (:func:`pin_plan`): the
checks then run from that copy, never from the operator's directory and
never from a tree an engineer writes. The digest goes into the plan an L1
person approves (``plan_gate.plan_digest``), and once the base replay has
accepted the plan the manifest pins it, so a later run of the same plan
with the directory edited refuses, naming both digests.

Every check runs inside the test zone of the proven rung, in a fresh copy
of the plan as its working directory, with :data:`TREE_ENV` naming the
checkout under test. The checkout is a throwaway worktree of one commit,
made by the slice 3 replay (:func:`kstrl.replay.replay_stack`): setup,
``up``, then the checks in place of the stack's own.

- On the base, after the plan gate and before the first engineer
  (:func:`replay_base`). A check the plan says fails on the base must fail
  there, and one it says passes must pass: either contradiction refuses
  the run. A check that did not run refuses, except in a component the
  plan marks ``createsApp``, which is recorded as
  :data:`BASE_NOT_RUNNABLE` (decision 11).
- On each component's head, after Phase 1 passed (:func:`judge_head`).
  Each check runs :data:`HEAD_RUNS` times and passes only when every run
  exited 0. Nothing is run again after a failure. The verdict is recorded
  and printed and gates nothing: slice 4 is record-only.

Held-out checks are recorded as such and are handed to no engineer: the
plan is never copied into a worktree, a PRD or a prompt.

The evidence of each head run is written under the run directory before
anything is printed: ``record.json``, ``logs/``, a byte copy of the plan
the checks ran from and ``index.json`` (each file's sha256).
:func:`render_lines` is the one renderer of a record: the terminal prints
its lines and the pull request's ``## Acceptance`` section is the same
lines (:func:`pr_section`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git
from kstrl.atomicio import atomic_write_json
from kstrl.events import RunPaths
from kstrl.jsonread import read_json
from kstrl.names import validate_component_id
from kstrl.replay import Replay, Stage, _ran, replay_stack
from kstrl.rung import HOST_LABEL, ProvenRung, zone_dir
from kstrl.stack import REPLAY_BOUNDARY_REFUSED
from kstrl.statedir import control_dir
from kstrl.timeout import limit_seconds
from kstrl.verify import SHELL_COULD_NOT_RUN

if TYPE_CHECKING:
    from kstrl.factory import FactoryConfig
    from kstrl.manifest import Manifest
    from kstrl.stack import Stack
    from kstrl.ui.base import UI

#: The one file a plan directory must hold.
PLAN_FILE = "plan.json"

#: Owner decision 4, the interim value for slice 4: one head run per
#: check. Every run must exit 0, and a failed run is never run again.
HEAD_RUNS = 1

#: What ``onBase`` may say. An absent value is refused, never defaulted.
ON_BASE = ("fails", "passes")

#: The keys of one check, and of one component's entry.
CHECK_KEYS = ("id", "criterion", "argv", "onBase", "heldOut")
COMPONENT_KEYS = ("createsApp", "checks")

#: The variable naming the checkout a check runs against. A check runs in
#: a copy of its plan, so it reaches the application through this path.
TREE_ENV = "KSTRL_TREE"

#: What a component's record says when the plan marks it as creating the
#: application and a check could not run on the base (decision 11).
BASE_NOT_RUNNABLE = "base not runnable"

#: A check's verdict, from its exit status alone.
PASS, FAIL, NOT_RUN = "pass", "fail", "not_run"

#: Where the plan copies live under the control directory, and where the
#: evidence lives under a run directory.
ACCEPTANCE_DIR = "acceptance"
BASE_FILE = "base.json"
RECORD_FILE = "record.json"
INDEX_FILE = "index.json"


@dataclass(frozen=True)
class Check:
    """One plan entry. ``sha256`` is the digest of the entry as written."""

    id: str
    criterion: str
    argv: tuple[str, ...]
    on_base: str
    held_out: bool
    sha256: str


@dataclass(frozen=True)
class ComponentPlan:
    creates_app: bool
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class PinnedPlan:
    """A plan copied into the control directory under its ``digest``."""

    digest: str
    directory: Path
    components: Mapping[str, ComponentPlan]


@dataclass(frozen=True)
class BaseReading:
    """What the base replay found: each check's exit by component and id
    (None when it gave none), and the components recorded as
    :data:`BASE_NOT_RUNNABLE`."""

    sha: str
    exits: Mapping[str, Mapping[str, int | None]]
    not_runnable: frozenset[str]


@dataclass(frozen=True)
class HeadOutcome:
    """What one head run of a component's checks printed and recorded."""

    passed: bool
    lines: list[str]
    checks: tuple[str, ...]
    failures: tuple[str, ...]
    isolation: str


Files = dict[str, tuple[bytes, bool]]


def verdict_of(code: int | None) -> str:
    """One run's verdict: 0 passes, a completed exit fails, and no exit,
    126 or 127 did not run."""
    if code == 0:
        return PASS
    if code is None or code in SHELL_COULD_NOT_RUN:
        return NOT_RUN
    return FAIL


def head_verdict(exits: Sequence[int | None]) -> str:
    """Pass only when there were runs and every one exited 0; fail when any
    run failed; otherwise the check did not run."""
    verdicts = [verdict_of(code) for code in exits]
    if verdicts and all(verdict == PASS for verdict in verdicts):
        return PASS
    return FAIL if FAIL in verdicts else NOT_RUN


def _refuse(error: OSError) -> None:
    raise error


def _plan_files(source: Path) -> Files:
    """Every file under ``source``: its relative path, bytes and whether it
    is executable. A directory or file that cannot be read raises, so an
    unreadable plan is a refusal, never a plan with fewer files."""
    files: Files = {}
    for here, dirs, names in os.walk(source, onerror=_refuse):
        for name in sorted([*dirs, *names]):
            path = Path(here) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise OSError(f"{path} is not a regular file or directory")
        for name in sorted(names):
            path = Path(here) / name
            files[path.relative_to(source).as_posix()] = (
                path.read_bytes(),
                bool(path.stat().st_mode & 0o111),
            )
    return files


def _digest(files: Files) -> str:
    rows = [
        [name, hashlib.sha256(data).hexdigest(), executable]
        for name, (data, executable) in sorted(files.items())
    ]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


def _entry_sha(entry: Mapping[str, Any]) -> str:
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_errors(raw: Any, component_ids: Sequence[str]) -> list[str]:
    """Everything wrong with a parsed ``plan.json``, one indexed line each,
    against the same constants the runner reads."""
    if not isinstance(raw, dict) or set(raw) != {"components"}:
        return [f'{PLAN_FILE} must be an object with one key, "components"']
    components = raw["components"]
    if not isinstance(components, dict) or not components:
        return [f"{PLAN_FILE}: components must be a non-empty object of component ids"]
    errors: list[str] = []
    for name, entry in components.items():
        errors += _component_errors(f"components.{name}", name, entry, component_ids)
    return errors


def _component_errors(where: str, name: str, entry: Any, ids: Sequence[str]) -> list[str]:
    if name not in ids:
        return [f"{where}: this plan has no component {name!r}"]
    if not isinstance(entry, dict) or not set(entry) <= set(COMPONENT_KEYS):
        return [f"{where} must be an object whose keys are among {', '.join(COMPONENT_KEYS)}"]
    errors = []
    if not isinstance(entry.get("createsApp", False), bool):
        errors.append(f"{where}.createsApp must be true or false")
    checks = entry.get("checks")
    if not isinstance(checks, list) or not checks:
        return [*errors, f"{where}.checks must be a non-empty list"]
    for index, check in enumerate(checks):
        errors += _check_errors(f"{where}.checks[{index}]", check)
    seen = [check.get("id") for check in checks if isinstance(check, dict)]
    repeated = sorted({i for i in seen if isinstance(i, str) and seen.count(i) > 1})
    return errors + [f"{where}.checks: the id {i!r} is used more than once" for i in repeated]


def _check_errors(where: str, check: Any) -> list[str]:
    if not isinstance(check, dict):
        return [f"{where} must be an object"]
    errors = [f"{where}.{key} is not a check key" for key in sorted(set(check) - set(CHECK_KEYS))]
    errors += [f"{where}.{key} is required" for key in CHECK_KEYS if key not in check]
    if errors:
        return errors
    if not isinstance(check["id"], str) or validate_component_id(check["id"]) is not None:
        errors.append(f"{where}.id must be a lowercase name usable as a file name")
    if not isinstance(check["criterion"], str) or not check["criterion"].strip():
        errors.append(f"{where}.criterion must be a non-empty string")
    argv = check["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        errors.append(f"{where}.argv must be a non-empty list of non-empty strings")
    if check["onBase"] not in ON_BASE:
        errors.append(
            f"{where}.onBase must be one of {', '.join(ON_BASE)}, got {check['onBase']!r}"
        )
    if not isinstance(check["heldOut"], bool):
        errors.append(f"{where}.heldOut must be true or false")
    return errors


def _parsed(raw: Mapping[str, Any]) -> dict[str, ComponentPlan]:
    return {
        name: ComponentPlan(
            entry.get("createsApp", False),
            tuple(
                Check(
                    check["id"],
                    check["criterion"],
                    tuple(check["argv"]),
                    check["onBase"],
                    check["heldOut"],
                    _entry_sha(check),
                )
                for check in entry["checks"]
            ),
        )
        for name, entry in raw["components"].items()
    }


def pinned_digest(plan: PinnedPlan | None) -> str:
    """The digest the plan gate folds into the plan a person approves."""
    return plan.digest if plan is not None else ""


def pin_plan(
    root: Path, manifest: Manifest, source: str, stack: Stack | None
) -> tuple[PinnedPlan | None, list[str]]:
    """Read, check and copy the plan at ``source``; or why the run must not
    start. Without ``source`` there is no plan, unless the manifest pins
    one, which refuses: a run must not drop the checks its plan was made
    to pass."""
    if not source:
        return None, _unpinned_errors(manifest)
    if stack is None:
        return None, [
            "--acceptance runs its checks inside the isolation rung, which only a "
            "[stack] in kstrl.toml proves. Nothing was run."
        ]
    where = Path(source).expanduser().resolve()
    files, errors = _read_source(root, where)
    if errors:
        return None, errors
    digest = _digest(files)
    if manifest.acceptance_digest and manifest.acceptance_digest != digest:
        return None, [
            f"this plan pinned the acceptance plan {manifest.acceptance_digest[:12]}, and "
            f"{where} now reads {digest[:12]}. Nothing was run.",
            f"Put {where} back as it was, or plan again to run under the edited checks.",
        ]
    return _checked_copy(root, where, files, digest, manifest)


def _unpinned_errors(manifest: Manifest) -> list[str]:
    if not manifest.acceptance_digest:
        return []
    return [
        f"this plan pinned the acceptance plan {manifest.acceptance_digest[:12]}, and this run "
        "names none. Nothing was run. Pass --acceptance with the directory of that plan."
    ]


def _read_source(root: Path, where: Path) -> tuple[Files, list[str]]:
    if where.is_relative_to(root.resolve()):
        return {}, [
            f"the acceptance plan {where} is inside the repository, where an engineer can "
            "read it. Keep it outside the repository. Nothing was run."
        ]
    try:
        return _plan_files(where), []
    except OSError as exc:
        return {}, [f"the acceptance plan {where} cannot be read: {exc}. Nothing was run."]


def _checked_copy(
    root: Path, where: Path, files: Files, digest: str, manifest: Manifest
) -> tuple[PinnedPlan | None, list[str]]:
    """Parse and check ``plan.json``, then copy the plan under its digest."""
    if PLAN_FILE not in files:
        return None, [f"the acceptance plan {where} has no {PLAN_FILE}. Nothing was run."]
    try:
        raw: Any = read_json(files[PLAN_FILE][0])
    except ValueError as exc:
        return None, [f"{where / PLAN_FILE} is not JSON: {exc}"]
    errors = plan_errors(raw, [comp.id for comp in manifest.components])
    if errors:
        return None, [f"{where / PLAN_FILE}: {line}" for line in errors]
    try:
        directory = _copy(root, digest, files)
    except OSError as exc:
        return None, [f"the acceptance plan cannot be copied under {control_dir(root)}: {exc}"]
    return PinnedPlan(digest, directory, _parsed(raw)), []


def _copy(root: Path, digest: str, files: Files) -> Path:
    """The plan's copy under the control directory, made once per digest."""
    final = control_dir(root) / ACCEPTANCE_DIR / digest
    if final.is_dir():
        _verified(final, digest)
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".plan-", dir=final.parent))
    try:
        _write_files(staging, files)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


def _verified(directory: Path, digest: str) -> Files:
    files = _plan_files(directory)
    found = _digest(files)
    if found != digest:
        raise OSError(f"the plan copy {directory} reads {found[:12]}, not {digest[:12]}")
    return files


def _write_files(into: Path, files: Files) -> None:
    for name, (data, executable) in files.items():
        target = into / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if executable else 0o644)


def limits(config: FactoryConfig) -> tuple[float | None, float | None]:
    """The setup limit and the per-command limit a replay runs under: the
    ones the worktree setup and Phase 1 run under."""
    return (
        limit_seconds(config.worktree_setup_timeout),
        limit_seconds(config.resolved_verify_config().subprocess_timeout),
    )


def _run_check(
    plan: PinnedPlan,
    check: Check,
    tree: Path,
    rung: ProvenRung,
    limit: float | None,
    stack: Stack,
    log: Path | None,
) -> Stage:
    """One run of ``check`` against ``tree``, in a fresh copy of the plan."""
    name = f"acceptance:{check.id}"
    try:
        cwd = Path(tempfile.mkdtemp(prefix=f"{check.id}-", dir=zone_dir(Path(rung.scratch))))
        _write_files(cwd, _verified(plan.directory, plan.digest))
        return _ran(
            name,
            list(check.argv),
            cwd,
            rung,
            limit,
            stack,
            extra_env={TREE_ENV: str(tree)},
            log=log,
        )
    except OSError as exc:
        return Stage(name, " ".join(check.argv), None, f"did not run: {exc}", 0.0, ())


def _probe(
    plan: PinnedPlan,
    wanted: Sequence[tuple[str, Check]],
    stack: Stack,
    limit: float | None,
    runs: dict[tuple[str, str], list[Stage]],
    times: int,
    logs: Callable[[str, str, int], Path | None],
) -> Callable[[Path, ProvenRung], None]:
    """Run each wanted check ``times`` times against the tree, into ``runs``.
    Every run is kept: a failed run is never run again."""

    def probe(tree: Path, rung: ProvenRung) -> None:
        for comp, check in wanted:
            runs[(comp, check.id)] = [
                _run_check(plan, check, tree, rung, limit, stack, logs(comp, check.id, run))
                for run in range(1, times + 1)
            ]

    return probe


def _no_log(_comp: str, _check: str, _run: int) -> Path | None:
    return None


def _first_exit(runs: Mapping[tuple[str, str], list[Stage]], comp: str, check: str) -> int | None:
    stages = runs.get((comp, check), [])
    return stages[0].exit if stages else None


def replay_base(
    root: Path,
    stack: Stack,
    plan: PinnedPlan,
    base_branch: str,
    run_id: str,
    config: FactoryConfig,
    ui: UI,
) -> tuple[BaseReading | None, list[str]]:
    """Replay every check of the plan once on the base; or why the run must
    not start. The reading is written to the run directory either way."""
    try:
        sha = git.resolve_base_sha(base_branch, root)
    except git.GitDiffError as exc:
        return None, [f"the base branch {base_branch} did not resolve: {exc}"]
    ui.info(f"  Replaying the acceptance checks on the base {sha[:12]}...")
    wanted = [(comp, check) for comp, part in plan.components.items() for check in part.checks]
    runs: dict[tuple[str, str], list[Stage]] = {}
    setup_limit, check_limit = limits(config)
    record = replay_stack(
        root,
        stack,
        setup_limit=setup_limit,
        check_limit=check_limit,
        ui=ui,
        at=sha,
        probe=_probe(plan, wanted, stack, check_limit, runs, 1, _no_log),
    )
    exits = {
        comp: {check.id: _first_exit(runs, comp, check.id) for check in part.checks}
        for comp, part in plan.components.items()
    }
    not_runnable, errors = _base_errors(plan, record, exits)
    reading = BaseReading(sha, exits, frozenset(not_runnable))
    for comp in sorted(not_runnable):
        ui.warn(f"  {comp}: {BASE_NOT_RUNNABLE} (the plan marks it as creating the app)")
    return reading, errors + _write_base(root, run_id, plan, record, reading, runs, errors)


def _base_errors(
    plan: PinnedPlan, record: Replay, exits: Mapping[str, Mapping[str, int | None]]
) -> tuple[set[str], list[str]]:
    """The components recorded as not runnable on the base, and why the base
    refuses the plan."""
    if record.error or record.failed == REPLAY_BOUNDARY_REFUSED:
        said = record.error or record.detail
        return set(), [f"the acceptance checks could not be replayed on the base: {said}"]
    stopped = f"; the replay stopped at {record.failed}: {record.detail}" if record.failed else ""
    not_runnable: set[str] = set()
    errors: list[str] = []
    for comp, part in plan.components.items():
        for check in part.checks:
            verdict = verdict_of(exits[comp][check.id])
            if verdict == NOT_RUN and part.creates_app:
                not_runnable.add(comp)
            else:
                errors += _contradiction(comp, check, verdict, exits[comp][check.id], stopped)
    return not_runnable, errors


def _contradiction(
    comp: str, check: Check, verdict: str, code: int | None, stopped: str
) -> list[str]:
    """Why ``check``'s base verdict refuses the plan, or []."""
    if verdict == NOT_RUN:
        said = "no exit" if code is None else f"exit {code}"
        return [f"{comp}: the check {check.id} could not run on the base ({said}){stopped}"]
    if verdict == PASS and check.on_base == "fails":
        return [
            f"{comp}: the check {check.id} passes on the base, so it cannot tell the change "
            "from no change (onBase: fails)"
        ]
    if verdict == FAIL and check.on_base == "passes":
        return [f"{comp}: the check {check.id} fails on the base (exit {code}); onBase: passes"]
    return []


def _write_base(
    root: Path,
    run_id: str,
    plan: PinnedPlan,
    record: Replay,
    reading: BaseReading,
    runs: Mapping[tuple[str, str], list[Stage]],
    errors: list[str],
) -> list[str]:
    """Write the base reading; return why it could not be written, or []."""
    path = RunPaths.for_run(root, run_id).root / ACCEPTANCE_DIR / BASE_FILE
    document = {
        "run": run_id,
        "planId": plan.digest,
        "stackDigest": record.stack_digest,
        "baseSha": reading.sha,
        "isolation": record.isolation,
        "replay": {"failed": record.failed, "detail": record.detail, "error": record.error},
        "components": {
            comp: {
                "base": BASE_NOT_RUNNABLE if comp in reading.not_runnable else "",
                "checks": [
                    {
                        "id": check.id,
                        "onBase": check.on_base,
                        "exit": reading.exits[comp][check.id],
                        # A held-out check's output can say what it expected,
                        # and this file is written before the first engineer.
                        "tail": [
                            line
                            for stage in runs.get((comp, check.id), [])
                            for line in stage.tail
                            if not check.held_out
                        ],
                    }
                    for check in part.checks
                ],
            }
            for comp, part in plan.components.items()
        },
        "refused": errors,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, document)
    except OSError as exc:
        return [f"the acceptance base reading cannot be written at {path}: {exc}"]
    return []


def judge_head(
    root: Path,
    config: FactoryConfig,
    comp_id: str,
    head_sha: str,
    *,
    run_id: str,
    attempt: int,
    ui: UI,
) -> HeadOutcome | None:
    """Run ``comp_id``'s checks on ``head_sha``, write the evidence, and
    return what to print; None when the run has no plan for it. Record-only:
    nothing here decides what happens to the component."""
    plan, base, stack = config.acceptance_plan, config.acceptance_base, config.project_stack
    if plan is None or base is None or stack is None or comp_id not in plan.components:
        return None
    if not head_sha:
        # An empty ``at`` replays the base: never judge the base as the head.
        line = f"Acceptance for {comp_id}: the head commit cannot be read, so no check ran"
        return HeadOutcome(False, [line], (), (line,), HOST_LABEL)
    part = plan.components[comp_id]
    evidence = RunPaths.for_run(root, run_id).root / ACCEPTANCE_DIR / comp_id / f"attempt-{attempt}"
    try:
        (evidence / "logs").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _unwritten(comp_id, evidence, exc)
    runs: dict[tuple[str, str], list[Stage]] = {}
    setup_limit, check_limit = limits(config)
    record = replay_stack(
        root,
        stack,
        setup_limit=setup_limit,
        check_limit=check_limit,
        ui=ui,
        at=head_sha,
        probe=_probe(
            plan,
            [(comp_id, check) for check in part.checks],
            stack,
            check_limit,
            runs,
            HEAD_RUNS,
            lambda _comp, check, run: evidence / "logs" / f"{check}-{run}.log",
        ),
    )
    document = _record(plan, base, record, comp_id, (head_sha, attempt, run_id), runs, evidence)
    lines = render_lines(document)
    failures = [_row_line(row, HEAD_RUNS) for row in document["checks"] if row["verdict"] != PASS]
    try:
        _write_evidence(evidence, plan, document)
    except OSError as exc:
        return _unwritten(comp_id, evidence, exc)
    return HeadOutcome(
        passed=not failures,
        lines=lines,
        checks=tuple(row["id"] for row in document["checks"]),
        failures=tuple(failures),
        isolation=record.isolation.get("test", HOST_LABEL),
    )


def _unwritten(comp_id: str, evidence: Path, exc: OSError) -> HeadOutcome:
    """A head run whose evidence could not be written: nothing else is shown."""
    line = f"Acceptance for {comp_id}: the evidence cannot be written at {evidence}: {exc}"
    return HeadOutcome(False, [line], (), (line,), HOST_LABEL)


def _record(
    plan: PinnedPlan,
    base: BaseReading,
    record: Replay,
    comp_id: str,
    head: tuple[str, int, str],
    runs: Mapping[tuple[str, str], list[Stage]],
    evidence: Path,
) -> dict[str, Any]:
    head_sha, attempt, run_id = head
    part = plan.components[comp_id]
    rows = []
    for check in part.checks:
        stages = runs.get((comp_id, check.id), [])
        exits = [stage.exit for stage in stages]
        logs = [evidence / "logs" / f"{check.id}-{run}.log" for run in range(1, len(stages) + 1)]
        rows.append(
            {
                "id": check.id,
                "sha256": check.sha256,
                "kind": "held out" if check.held_out else "visible",
                "heldOut": check.held_out,
                "baseExit": base.exits[comp_id][check.id],
                "headExits": exits,
                "seconds": [stage.seconds for stage in stages],
                "logSha256": [_file_sha(log) for log in logs],
                "verdict": head_verdict(exits),
            }
        )
    return {
        "run": run_id,
        "component": comp_id,
        "attempt": attempt,
        "planId": plan.digest,
        "stackDigest": record.stack_digest,
        "checksDigest": _entry_sha({"checks": [check.sha256 for check in part.checks]}),
        "baseSha": base.sha,
        "headSha": head_sha,
        "isolation": record.isolation,
        "base": BASE_NOT_RUNNABLE if comp_id in base.not_runnable else "",
        "headRuns": HEAD_RUNS,
        "replay": {"failed": record.failed, "detail": record.detail, "error": record.error},
        "checks": rows,
    }


def _file_sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _write_evidence(evidence: Path, plan: PinnedPlan, document: Mapping[str, Any]) -> None:
    """The plan copy the checks ran from, the record, then the index of
    every file's sha256, written last."""
    _write_files(evidence / "checks", _verified(plan.directory, plan.digest))
    atomic_write_json(evidence / RECORD_FILE, document)
    index = {
        path.relative_to(evidence).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(evidence.rglob("*"))
        if path.is_file() and path.name != INDEX_FILE
    }
    atomic_write_json(evidence / INDEX_FILE, index)


def render_lines(record: Mapping[str, Any]) -> list[str]:
    """The lines a head record is shown as, on the terminal and in the PR
    body alike."""
    isolation = record["isolation"].get("test", HOST_LABEL)
    base = f", {record['base']}" if record["base"] else ""
    lines = [
        f"Acceptance for {record['component']}, attempt {record['attempt']} (record-only): "
        f"plan {record['planId'][:12]}, head {record['headSha'][:12]}{base}; {isolation}"
    ]
    replay = record["replay"]
    if replay["error"] or replay["failed"]:
        lines.append(f"- the head replay stopped: {replay['error'] or replay['detail']}")
    return lines + [_row_line(row, record["headRuns"]) for row in record["checks"]]


def _row_line(row: Mapping[str, Any], runs: int) -> str:
    passed = sum(1 for code in row["headExits"] if code == 0)
    if row["verdict"] == PASS:
        said = f"passed {passed} of {runs} runs"
    elif row["verdict"] == FAIL:
        said = f"passed {passed} of {runs} runs -> fail"
    else:
        said = "did not run (" + ", ".join(str(code) for code in row["headExits"]) + ")"
    base = "no base exit" if row["baseExit"] is None else f"base exit {row['baseExit']}"
    return f"- {row['id']} ({row['kind']}): {said}; {base}"


def pr_section(root: Path | None, run_id: str, comp_id: str) -> list[str]:
    """The ``## Acceptance`` section of a component's PR body: the lines of
    its latest head record, or [] when this run judged none (or the caller
    named no root)."""
    if root is None or not run_id:
        return []
    attempts = RunPaths.for_run(root, run_id).root / ACCEPTANCE_DIR / comp_id
    if not attempts.is_dir():
        return []
    numbered = [
        (int(path.name.removeprefix("attempt-")), path)
        for path in attempts.iterdir()
        if path.name.removeprefix("attempt-").isdigit()
    ]
    if not numbered:
        return ["## Acceptance", "", f"No acceptance record was written under {attempts}.", ""]
    path = max(numbered)[1] / RECORD_FILE
    try:
        record = read_json(path.read_text(encoding="utf-8"))
        lines = render_lines(record)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        lines = [f"The acceptance record {path} cannot be read: {exc}"]
    return ["## Acceptance", "", *lines, ""]
