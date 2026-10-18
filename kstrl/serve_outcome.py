"""What a finished factory run means: the runner's RunOutcome in, a Verdict out."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from kstrl.io import read_json, state_dir
from kstrl.plan import ADVERSARIAL_BUDGET_CHECK, Component, ComponentStatus, Manifest
from kstrl.serve_config import ServeObserver

#: Substrings the factory prints on two of its exit-2 refusals.
#: Matching output is EVIDENCE; a pre-launch lock probe is inference and
#: races (#186 F6). Both come from kstrl/factory/factory.py and kstrl/cli.py. A
#: third refusal, a red base (#654), is read from the record the run
#: wrote, never from output: see ``_red_base_outcome``.
_LOCK_REFUSAL_MARKER = "--force-lock"


_SPEC_BLOCKER_MARKER = "Spec issues written to:"


class Verdict(StrEnum):
    """What the evidence says about a finished run.

    ``UNCLASSIFIABLE`` is a first-class outcome rather than an error
    path: it is the common case for a crash, and treating it as "probably
    fine, retry" is the bug this whole module is arranged to avoid.
    """

    SUCCESS = "success"
    RETRY_INFRA = "retry_infra"
    SPEC_FAILURE = "spec_failure"
    UNCLASSIFIABLE = "unclassifiable"
    #: The run halted itself on a configured ceiling. Deliberate and
    #: DETERMINISTIC - the only verdict that would otherwise have been
    #: read as retryable infrastructure. See budget_halt_reason.
    BUDGET_HALT = "budget_halt"
    #: The merge gate parked work that passed every gate, and nothing
    #: failed (#465). Not a verdict on the spec and not retryable: the
    #: item waits for `ks inbox approve` or `ks inbox reject`.
    AWAITING_APPROVAL = "awaiting_approval"
    #: The architect escalated a question only the owner can answer (#644).
    #: Not a verdict on the spec and not retryable: the item waits for
    #: `ks queue answer`, and it is neither poisoned nor counted toward
    #: the consecutive-poison breaker.
    AWAITING_ANSWER = "awaiting_answer"
    #: The run refused because the base branch already fails its own gates
    #: (#654): a run this launch owns wrote ``base-gates.json`` with
    #: ``refused: true``. Repo-wide, so not a verdict on the spec: the item
    #: is requeued, claims pause until `ks queue resume`, and the
    #: consecutive-poison breaker does not count it.
    RED_BASE = "red_base"

    @property
    def may_retry(self) -> bool:
        """Only ONE verdict authorizes spending again."""
        return self is Verdict.RETRY_INFRA


@dataclass(frozen=True)
class Outcome:
    """A verdict plus the evidence that produced it.

    ``evidence`` is written to the inbox item, so a human deciding
    whether to requeue sees what the classifier actually read rather
    than having to trust its label.
    """

    verdict: Verdict
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunOutcome:
    """Raw result of one factory invocation, before interpretation."""

    returncode: int
    timed_out: bool = False
    #: Non-empty when the launch itself failed (binary missing, etc).
    launch_error: str = ""
    #: Tail of the child's combined output. The factory's two exit-2
    #: refusals are only distinguishable from what it printed (#186 F6),
    #: so this is classification evidence rather than a log nicety.
    output_tail: str = ""
    #: True when the timeout path confirmed the whole process GROUP was
    #: signalled and reaped. False means a descendant may still be alive,
    #: which must never be treated as "the run is over" (#186 F1).
    group_reaped: bool = False
    #: Why the reap check could not measure, when it could not. A
    #: `group_reaped=False` reached this way means "we could not see",
    #: not "a factory is still running", and the poison reason an
    #: operator reads says so. Empty when the check measured cleanly.
    #: Set on the reaped path too, where it means the "gone" itself was
    #: not measured, which is the unsafe direction and so is reported.
    group_reap_detail: str = ""
    #: Positive evidence the group IS occupied, as opposed to merely
    #: unconfirmed. Kept apart from `group_reap_detail` because reporting
    #: a refused signal as "unknown" states the opposite of the truth.
    group_occupied_detail: str = ""


class FactoryRunner(Protocol):
    """How the daemon executes one queue item.

    A Protocol so tests drive the entire loop without spawning a factory.
    That is not only a speed concern: a suite that ran the real thing
    would cost dollars per assertion.
    """

    def __call__(
        self,
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        design_acceptance: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome: ...


def budget_halt_reason(root_dir: Path, owned_run_ids: Sequence[str]) -> str:
    """Why an owned run halted on a RUN-LEVEL ceiling, or "" if none did.

    Covers the token and cost ceilings, which emit ``BudgetExceeded``.
    It does NOT cover the adversarial-call cap: R10.5 (#226) halts a
    hard-mode review or security phase on an exhausted
    ``max_adversarial_calls`` without emitting an event for it
    (doctrine 6), so this function is blind to that halt by
    construction. ``_classify_failed_components`` reads it from the
    manifest instead, and the two together are what make every
    deliberate ceiling terminal.

    Found by the first live `ks serve` run, and it is the most expensive
    thing this module could have got wrong. `pipeline.fail_for_budget`
    records a blown ceiling as ``Finding.infrastructure_error`` with no
    distinguishing category, so the classifier read a deliberate
    "stop spending" as transient infrastructure trouble and RETRIED it.

    A budget halt is deterministic, not transient. The retry re-runs the
    same work against the same ceiling, and retries cost MORE because
    they carry accumulated context ($3.99-7.42 versus $1.70-2.60
    measured). Three attempts of that is the crash loop R8.6 exists to
    prevent, arrived at through the one branch meant to be safe.

    Read from the typed ``BudgetExceeded`` event rather than by matching
    the finding's prose, so this cannot drift when the wording changes.
    """
    from kstrl.events import event_catalog, read_run_dir

    for run_id in owned_run_ids:
        if not run_id:
            continue
        try:
            events = read_run_dir(state_dir(root_dir) / "runs" / run_id)
        except OSError:
            continue
        for event in events:
            if isinstance(event, event_catalog.BudgetExceeded):
                named = ", ".join(event.ceilings)
                # BudgetExceeded also carries condition="unenforceable",
                # where NO threshold was crossed - the ceiling simply
                # cannot fire because nothing reports that axis. Telling
                # that operator to "raise the ceiling" points them at a
                # breach that never happened (#197 M2). Routed through the
                # shared classifier so this cannot drift from the reducer's
                # and the Linear sink's reading of the same payload.
                kind = event_catalog.budget_halt_kind(event.condition, event.ceilings)
                return _budget_halt_sentence(run_id, kind, named)
    return ""


def _budget_halt_sentence(run_id: str, kind: str, named: str) -> str:
    """The sentence for one budget halt, by the shared classifier's ``kind``."""
    if kind == "unreadable":
        return (
            f"the run halted in {run_id} on a budget record that names no "
            "ceilings (written before #180), so the cause cannot be read"
        )
    if kind == "unenforceable":
        return (
            f"the run halted in {run_id} because no configured "
            f"ceiling ({named}) can still fire: no metered call "
            "reported that axis, so the cap could never stop the "
            "spend. Fix the coverage or the configuration - "
            "raising a limit would change nothing"
        )
    return (
        f"the run halted on a configured ceiling ({named}) in "
        f"{run_id}: raising the ceiling or narrowing the spec is "
        "a human decision, and retrying would re-run the same "
        "work against the same limit at a higher cost"
    )


def _infra_casualty(component: Any) -> bool:
    """Whether a component's failure was infrastructural.

    Mirrors ``factory._infra_casualty`` exactly, and reuses the same
    ``Finding.is_infrastructure_error`` predicate rather than
    re-deriving it. Two copies of this rule that drift apart is how a
    spec failure becomes retryable, so the shared predicate is the whole
    point.
    """
    return any(f.is_infrastructure_error for f in component.findings)


def classify_run(
    root_dir: Path,
    *,
    run: RunOutcome,
    manifest_path: Path | None,
    owned_run_ids: Sequence[str] = (),
) -> Outcome:
    """Decide what a finished factory run means for its queue item.

    Positive evidence only. Every branch that cannot prove the failure
    was infrastructural returns a non-retrying verdict, and the reason
    string always says which branch fired so a human reading the inbox
    can audit the decision.
    """
    if run.launch_error:
        # The factory never started, so nothing was spent. Retrying is
        # free, which is what makes this safe to retry at all.
        return Outcome(
            Verdict.RETRY_INFRA,
            f"launch failed before any spend: {run.launch_error}",
            {"launch_error": run.launch_error},
        )

    # Checked BEFORE every retry-authorizing branch, and immediately after
    # the launch-error case (the one place where no child artifacts can
    # exist). Review #197 M1: placing it after the timeout / signal /
    # exit-2 branches meant a run that blew its ceiling and then hung long
    # enough for factory_timeout_seconds to kill it was requeued against
    # the very ceiling this verdict exists to make terminal.
    halted = budget_halt_reason(root_dir, owned_run_ids)
    if halted:
        return Outcome(
            Verdict.BUDGET_HALT,
            halted,
            {"returncode": run.returncode, "owned_run_ids": list(owned_run_ids)},
        )

    if run.timed_out:
        return _timeout_outcome(manifest_path, run.returncode)

    if run.returncode == 0:
        return Outcome(Verdict.SUCCESS, "factory exited 0", {"returncode": 0})

    if run.returncode < 0:
        # Killed by a signal: SIGKILL from an OOM, a suspend that took
        # the process with it, or an operator. That is affirmative
        # evidence of an EXTERNAL cause rather than a verdict on the
        # spec, so it is the one crash shape that legitimately retries.
        return Outcome(
            Verdict.RETRY_INFRA,
            f"killed by signal {-run.returncode} (external cause, not a verdict on the spec)",
            {"signal": -run.returncode},
        )

    if run.returncode == 2:
        return _refusal_outcome(root_dir, run.output_tail, owned_run_ids)

    # Everything else needs the manifest to say something specific - and
    # it must be a manifest THIS invocation wrote. ``None`` means the run
    # produced no owned artifacts, so there is nothing to read and the
    # honest verdict is unclassifiable (#186 F2: an empty run id used to
    # fall back to the newest run on disk).
    if manifest_path is None:
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"exit {run.returncode} and this invocation produced no run "
            "artifacts of its own; refusing to classify from another run\u2019s "
            "manifest",
            {"returncode": run.returncode},
        )
    try:
        manifest = Manifest.load(manifest_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"exit {run.returncode} and the manifest could not be read "
            f"({exc}); refusing to guess whether this was infrastructural",
            {"returncode": run.returncode, "manifest": str(manifest_path)},
        )

    failed = [comp for comp in manifest.components if str(comp.status) == "failed"]
    if not failed:
        return _unfailed_outcome(manifest, run.returncode)

    # R10.5 (#226). Checked FIRST among the manifest branches, because
    # every budget-halted component carries an infrastructure_error
    # finding and would otherwise reach the RETRY_INFRA return at the
    # bottom of _merits_outcome.
    budget = _budget_halt_outcome(failed, run.returncode)
    if budget is not None:
        return budget
    return _merits_outcome(failed, run.returncode)


def _refusal_outcome(root_dir: Path, tail: str, owned_run_ids: Sequence[str]) -> Outcome:
    """What the factory's exit 2, its own "refused to proceed" code, means.

    It covers an architect halt on a blocker-severity spec issue, refusal
    because another run holds .kstrl/factory.lock, and a base branch that
    already fails its gates (#654). The first two need opposite treatment,
    and a pre-launch lock probe cannot tell them apart: the probe releases
    the lock, so a manual factory can take it in the gap before our child
    does (#186 F6, a real TOCTOU on exactly the distinction this relies on).

    So decide from evidence the child left, not inference: the red base
    from the record it wrote, the other two from its own output, and stay
    UNCLASSIFIABLE when none is present rather than guessing. The red base
    is read first and never from output: its refusal prints the base's
    failing test names, which are the repository's own text. Split out of
    ``classify_run``, which is past the cyclomatic ratchet.
    """
    red_base = _red_base_outcome(root_dir, owned_run_ids)
    if red_base is not None:
        return red_base
    if _LOCK_REFUSAL_MARKER in tail:
        return Outcome(
            Verdict.RETRY_INFRA,
            "factory exited 2 because another run held the run lock; contention is infrastructural",
            {"returncode": 2, "cause": "lock_contention"},
        )
    if _SPEC_BLOCKER_MARKER in tail:
        return Outcome(
            Verdict.AWAITING_ANSWER,
            "factory exited 2: the architect escalated a question only the owner can answer",
            {"returncode": 2, "cause": "spec_blocker"},
        )
    return Outcome(
        Verdict.UNCLASSIFIABLE,
        "factory exited 2 but its output named neither a spec blocker "
        "nor lock contention; refusing to guess which refusal it was",
        {"returncode": 2, "output_tail": tail[-500:]},
    )


def _red_base_outcome(root_dir: Path, owned_run_ids: Sequence[str]) -> Outcome | None:
    """RED_BASE when a run this launch owns recorded a refused base, else None (#654).

    ``factory._preflight_base_gates`` writes ``base-gates.json`` before it
    acts on the reading, and a reading it could not write refuses with no
    record. So a missing, unreadable or malformed record is no evidence, and
    ``refused`` must be the JSON ``true``, not merely truthy.
    """
    from kstrl.verify import base_gates_path

    for run_id in owned_run_ids:
        if not run_id:
            continue
        path = base_gates_path(root_dir, run_id)
        record = _read_manifest_json(path)
        if record is None or record.get("refused") is not True:
            continue
        raw = record.get("reasons")
        reasons = [line for line in raw if isinstance(line, str)] if isinstance(raw, list) else []
        branch = str(record.get("baseBranch", ""))
        sha = str(record.get("baseSha", ""))
        return Outcome(
            Verdict.RED_BASE,
            f"factory exited 2: the base branch {branch} at {sha[:12]} fails its own "
            f"gates ({'; '.join(reasons) or 'no reason recorded'}), recorded in {path}",
            {
                "returncode": 2,
                "cause": "red_base",
                "run_id": run_id,
                "record": str(path),
                "base_branch": branch,
                "base_sha": sha,
                "reasons": reasons,
            },
        )
    return None


def _unfailed_outcome(manifest: Manifest, returncode: int) -> Outcome:
    """A nonzero exit whose manifest blames no component.

    #465: a component the merge gate parked is positive evidence of WHY:
    it passed every gate and waits for a human, so the item awaits
    approval. Anything else here (unconfirmed merges, contract failures,
    a stop mid-run) may well be resumable, but none is an
    infrastructure_error, and inventing that label here is exactly the
    fail-open shape this module refuses. Split out of ``classify_run``,
    which is past the cyclomatic ratchet.
    """
    if manifest.plan_awaiting_approval:
        # #602: the L1 plan gate parked the plan with nothing run.
        return Outcome(
            Verdict.AWAITING_APPROVAL,
            "awaiting plan approval: nothing was run; `ks inbox approve <id>` runs "
            "the plan, `ks inbox reject <id> --comment ...` refuses it",
            {"returncode": returncode, "plan_awaiting_approval": manifest.plan_awaiting_approval},
        )
    parked = [
        comp.id
        for comp in manifest.components
        if comp.status == ComponentStatus.AWAITING_APPROVAL.value
    ]
    if parked:
        return Outcome(
            Verdict.AWAITING_APPROVAL,
            "awaiting merge approval: "
            + ", ".join(parked)
            + " passed every gate and the merge gate parked it with nothing "
            "pushed; `ks inbox approve <id>` merges it and continues the run",
            {"returncode": returncode, "awaiting_approval": parked},
        )
    return Outcome(
        Verdict.UNCLASSIFIABLE,
        f"exit {returncode} with no failed component to attribute it "
        "to (unconfirmed merge, contract failure, or an interrupted "
        "run); a human decides whether to resume",
        {
            "returncode": returncode,
            "statuses": sorted({str(c.status) for c in manifest.components}),
        },
    )


def _timeout_outcome(manifest_path: Path | None, returncode: int) -> Outcome:
    """What a run WE killed on ``factory_timeout_seconds`` classifies as.

    Same rule as the ``budget_halt_reason`` check above the timeout
    branch in ``classify_run``, one branch over. A run that halted a component on
    ``max_adversarial_calls`` and THEN hung long enough for the timeout
    to kill it is still a run that reached the cap, and requeuing it pays
    a whole run to re-reach a counter that starts again at zero. That is
    the shape #197 M1 already fixed once for the token and cost ceilings.

    The manifest is written by the pipeline from its own counter before
    the kill, so it is evidence about THIS run rather than inference. It
    is read for positive evidence only: no path, no file, or a file that
    will not parse all answer "no evidence" and leave the timeout verdict
    standing, which is what this branch returned for every run before
    #226. A separate function rather than two more branches inside
    ``classify_run``, which is already at the cyclomatic ratchet.
    """
    if manifest_path is not None:
        try:
            manifest = Manifest.load(manifest_path)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            manifest = None
        if manifest is not None:
            failed = [comp for comp in manifest.components if str(comp.status) == "failed"]
            halted = _budget_halt_outcome(failed, returncode)
            if halted is not None:
                # The halt does not erase the hang. Same rule as the
                # sibling note below: the evidence is what a human reads
                # before deciding to requeue, and "terminal on the cap"
                # and "we also had to kill it" are two facts, one of
                # which is worth investigating on its own.
                return replace(halted, evidence={**halted.evidence, "timed_out": True})
    # WE killed it. A hang is an infrastructure symptom, and max_attempts
    # plus the daily budget bound the exposure.
    return Outcome(
        Verdict.RETRY_INFRA,
        "run exceeded serve.factory_timeout_seconds and was killed",
        {"timed_out": True},
    )


def _budget_halt_outcome(failed: Sequence[Component], returncode: int) -> Outcome | None:
    """BUDGET_HALT when the adversarial call cap ended the run, else None.

    The cap halting a hard-mode review or security phase is a deliberate
    "stop spending", not transient trouble. Reading it as the latter is
    the mistake ``budget_halt_reason`` exists to prevent, and that
    function cannot see this halt: it emits no ``BudgetExceeded`` event
    (doctrine 6), so the record is ``Component.failed_check`` in the
    manifest. Positive evidence from a field the pipeline sets from its
    own counter, never from anything a model reported.

    ANY halted component, not every: the counter is per pipeline
    instance and starts again at zero on the retry, so the retry gets
    exactly as far and stops at the same component. A manifest mixing a
    budget halt with a genuine infrastructure casualty would pay a whole
    run to re-reach the same cap, and retries cost more than the first
    attempt because they carry accumulated context.
    """
    halted = [comp.id for comp in failed if comp.failed_check == ADVERSARIAL_BUDGET_CHECK]
    if not halted:
        return None
    others = [comp for comp in failed if comp.failed_check != ADVERSARIAL_BUDGET_CHECK]
    # Written through the same two helpers _merits_outcome uses, and for
    # the same reason (#197 M3): a component that failed for another
    # cause must not lose its CAUSE just because this branch fired
    # first. An id alone sent a real operator looking in the wrong
    # place, and a sibling that produced no finding has nothing but
    # ``Component.error`` to be read from.
    other_evidence = _unevidenced_evidence(others, returncode)
    detail = _unevidenced_detail(others)
    also = f"; also failed, for other reasons - {detail}" if detail else ""
    return Outcome(
        Verdict.BUDGET_HALT,
        "the run halted on max_adversarial_calls: "
        + ", ".join(halted)
        + " reached a hard-mode review or security phase with the adversarial "
        "call budget already spent. Raise the cap or set the mode to advisory; "
        "retrying would re-run the same work against the same cap" + also,
        {
            "returncode": returncode,
            "budget_halted": halted,
            # Named ``other_failures`` rather than the helper's own key:
            # a sibling here may well carry findings, so calling it
            # unevidenced would be a claim this branch has not checked.
            "other_failures": other_evidence["unevidenced_failures"],
            "component_errors": other_evidence["component_errors"],
        },
    )


def _merits_outcome(failed: Sequence[Component], returncode: int) -> Outcome:
    """Whether named failures were the spec's fault, unproven, or infra.

    Split out of ``classify_run`` by #226 round 2, which added the branch
    above it and would otherwise have pushed that function past the
    cyclomatic ratchet, with the two duplicated joins written once. The
    verdicts, their order and their text are the ones that were here
    before the split; ``run.returncode`` became a parameter, and the
    ``sibling_note`` guard reads ``detail`` rather than ``unevidenced``,
    which is the same condition because every element of ``detail``
    formats to at least ``": no error recorded"``.
    """
    # A component that produced FINDINGS, none of them infrastructural, is
    # positive evidence of a merits-based failure. A component that
    # produced NO findings at all is not evidence of anything - and
    # claiming otherwise sent a real operator looking in the wrong place.
    #
    # Found by the first live `ks serve` run: a git worktree creation
    # failure ("fatal: invalid reference") set Component.error and emitted
    # no Finding, and this branch reported it as "failed on their own
    # merits, not on infrastructure". Both verdicts poison, so the money
    # behaviour was already right; the STATEMENT was false. Every unit
    # test had constructed manifests WITH findings, so nothing caught it.
    # #465: a component stopped at the merge gate passed every gate, so
    # its findings are not why it stopped; its error says why.
    judged = [
        comp.id
        for comp in failed
        if comp.findings and not _infra_casualty(comp) and comp.failed_check != "merge_gate"
    ]
    unevidenced = [
        comp for comp in failed if not comp.findings or comp.failed_check == "merge_gate"
    ]
    # Collected BEFORE the SPEC_FAILURE return: a mixed manifest used to
    # report only the component with a finding, silently dropping a
    # sibling's "fatal: invalid reference" from both the reason and the
    # evidence - recreating the exact operator misdirection this change
    # exists to remove (#197 M3).
    detail = _unevidenced_detail(unevidenced)
    sibling_note = f"; also failed with no finding to explain it - {detail}" if detail else ""
    evidence = _unevidenced_evidence(unevidenced, returncode)
    if judged:
        return Outcome(
            Verdict.SPEC_FAILURE,
            "spec-level failure: "
            + ", ".join(judged)
            + " failed on their own merits, not on infrastructure"
            + sibling_note,
            {**evidence, "judged_failures": judged},
        )

    if unevidenced:
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"failed with no finding to attribute it to, so the cause is unproven - {detail}",
            evidence,
        )

    return Outcome(
        Verdict.RETRY_INFRA,
        "every failed component carried an infrastructure_error finding: "
        + ", ".join(comp.id for comp in failed),
        {
            "returncode": returncode,
            "infra_failures": [comp.id for comp in failed],
        },
    )


def _unevidenced_detail(unevidenced: Sequence[Component]) -> str:
    """``id: error`` for each component the caller can only name.

    One writer for what was the same join written twice, so the reason a
    human reads in the inbox cannot say two different things about the
    same components. ``_budget_halt_outcome`` is the third caller and
    passes siblings that may carry findings: ``Component.error`` is set
    for those too, and it is the whole record for a component that
    produced no finding at all.
    """
    return "; ".join(f"{comp.id}: {comp.error or 'no error recorded'}" for comp in unevidenced)


def _unevidenced_evidence(unevidenced: Sequence[Component], returncode: int) -> dict[str, Any]:
    """The evidence a verdict records about failures it can only name.

    Same reason as ``_unevidenced_detail``: it was written out twice,
    and the inbox item is what a human reads before deciding to requeue.
    ``_budget_halt_outcome`` reads the two lists out under its own key
    names rather than merging the dict, because ``unevidenced_failures``
    would be a claim about its siblings that it has not checked.
    """
    return {
        "returncode": returncode,
        "unevidenced_failures": [comp.id for comp in unevidenced],
        "component_errors": {comp.id: comp.error for comp in unevidenced},
    }


def _read_manifest_json(path: Path) -> dict[str, Any] | None:
    """A run artifact's raw JSON object, or ``None`` if it cannot be read.

    The parser's error taxonomy belongs to the parser: this does the
    file read and the ``json.loads`` for every post-run manifest reader
    in this module, and for the base-gates record (#654), and catches
    ``Exception`` exactly because every caller reads after the spend is
    charged, so nothing one of them hits may end the cycle.
    Measured: a 20000-deep nested JSON array makes ``json.loads`` raise
    ``RecursionError``, which is a ``RuntimeError`` and escapes the
    narrower tuple ``classify_run`` uses on the pre-verdict path.
    """
    try:
        data = read_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _run_id_from_manifest(manifest_path: Path) -> str:
    """The run id the factory recorded, or "" if it never got that far.

    Routed through ``_read_manifest_json``: every cause of an unreadable
    manifest returns "" here, the caller then falls back to selecting by
    mtime, and no message is emitted about which happened.
    """
    data = _read_manifest_json(manifest_path)
    if data is None:
        return ""
    # "runId", not "run_id": Manifest.to_dict is camelCase on disk.
    # Review #186 F2 - reading the snake_case key returned "" for every
    # real manifest, which then selected the NEWEST run instead.
    run_id = data.get("runId")
    return run_id if isinstance(run_id, str) else ""


def _pr_urls_from_manifest(
    manifest_path: Path,
    owned: bool,
    observer: ServeObserver,
) -> tuple[str, ...]:
    """Every distinct PR URL an owned manifest records, in file order.

    ``owned`` false means this invocation produced no manifest of its
    own, so there is nothing to attribute and nothing to say - the same
    refusal ``classify_run`` makes, for the same reason (#186 F2).
    """
    if not owned:
        return ()
    data = _read_manifest_json(manifest_path)
    if data is None:
        observer.warn(f"  could not read PR URLs from {manifest_path}; recording none")
        return ()
    components = data.get("components")
    if not isinstance(components, list):
        return ()
    return tuple(
        dict.fromkeys(
            url
            for c in components
            if isinstance(c, dict)
            and isinstance(c.get("prUrl"), str)
            and (url := c["prUrl"].strip())
        )
    )
