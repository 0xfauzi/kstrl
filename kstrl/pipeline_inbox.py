"""The inbox items that a run adds and resolves.

``InboxDesk`` holds the inbox items that a run adds and resolves, and the
approvals (waivers) that the run reads from the inbox.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from kstrl.inbox import UNDECIDED, Inbox, InboxError, InboxItem, ItemKind
from kstrl.manifest import Component, park_dedupe_key
from kstrl.pipeline_state import PipelineState
from kstrl.statedir import ControlStateError
from kstrl.waivers import ApprovalSnapshot, Waivers, WaiverScope, load_approvals


class InboxDesk(PipelineState):
    """The inbox items and the approvals of one run."""

    def record_integration_halt(
        self, reason: str, open_findings: Sequence[str], evidence: str
    ) -> None:
        """The one inbox item a blocking integration loop leaves when it stops
        without a clean verdict (#483). Run-level, so it names no component.
        The durable record is .kstrl/integration/state.json, written first."""
        self._inbox_add(
            ItemKind.HALTED_RUN,
            "integration loop stopped without a clean verdict",
            detail=reason,
            dedupe_key=f"halted:integration:{self.run_id}",
            evidence={"open_findings": list(open_findings), "evidence": evidence},
        )

    def record_carried_halt(self, entry: Mapping[str, str], where: str, detail: str) -> None:
        """The inbox item of an earlier feature's check that does not pass on
        the base or in a Phase 3 that failed (#466). Approving it retires the
        check. One open item per check: a later failure bumps it."""
        plan, comp, check = entry["planId"], entry["component"], entry["check"]
        self._inbox_add(
            ItemKind.HALTED_RUN,
            f"An earlier feature's acceptance check {check} fails",
            detail=(
                f"The check {check} of {comp} in the acceptance plan {plan[:12]} passed on "
                f"{entry['passedAt'][:12]} and does not pass on {where} ({detail}). Approve "
                "this item to retire the check if a change made it obsolete: no later run "
                "replays it. Otherwise repair the change."
            ),
            dedupe_key=f"carried:{plan}:{comp}:{check}",
            evidence={
                "carried": {"planId": plan, "component": comp, "check": check},
                "where": where,
            },
        )

    def _open_inbox(self) -> Inbox:
        """The one ``Inbox`` this pipeline lazily builds and reuses.

        Every site below constructs on first use and none reconstructs:
        each still gates construction on ``self._inbox is None`` itself
        (some also branch on ``inbox_config.enabled`` before ever
        reaching here), so this is only the shared "build it once" step,
        not the disabled check - a caller that must not construct one at
        all when the inbox is disabled keeps that check ahead of the call.
        """
        if self._inbox is None:
            self._inbox = Inbox(self.root_dir, self.inbox_config)
        return self._inbox

    def _inbox_resolve(self, dedupe_key: str, reason: str) -> None:
        """Close an open item whose question the world has answered."""
        try:
            if self._inbox is None:
                if not self.inbox_config.enabled:
                    return
                self._inbox = self._open_inbox()
            existing = self._inbox.find_by_dedupe_key(dedupe_key)
            if existing is not None:
                # #648: the precondition is the fresh row inside resolve's
                # lock, so an operator's decision landing after this read wins.
                self._inbox.resolve(existing.id, comment=reason, only_from=UNDECIDED)
        except (OSError, TypeError, ValueError, InboxError, ControlStateError) as exc:
            # Same tuple as _inbox_add below, and for the same two
            # reasons: Inbox.resolve takes the control lock in _decide, so
            # it can raise ControlStateError,
            # and InboxConfig.load casts per key, so a TOML date raises
            # TypeError. #192 moved that cast to ``__init__``, behind
            # the entry preflight; the tuple keeps TypeError anyway,
            # because this function's contract is that closing a stale
            # item cannot fail the run that answered it.
            self.ui.warn(f"  Inbox resolve failed (non-fatal): {exc}")

    def _inbox_resolve_component(self, comp_id: str, detail: str = "") -> None:
        """Resolve every undecided item naming a component that COMPLETED (#438).

        Resolved on the fact, not on the command. ``ks retry`` never
        touched the inbox and ``ks inbox retry`` resolves only the item
        it was given, so a component completed either way could leave an
        item open, and ``ks inbox ls`` then disagreed with ``ks status``.
        Every place a component becomes COMPLETED calls this;
        ``tests/test_inbox_resolves_on_completion.py`` counts those places
        and fails a new one that does not.

        Undecided means OPEN or SNOOZED (``inbox.UNDECIDED``). A snoozed
        item comes back when its TTL lapses, and it would come back
        asking about a component that has already completed. APPROVED,
        REJECTED and RESOLVED are decisions already made and are left as
        they are - guaranteed at the WRITE, not by this list. The
        ``undecided`` list below is a pre-filter built from a snapshot,
        so an operator's ``approve``/``reject``/``snooze`` can land after
        it and before ``resolve`` appends; ``only_from=UNDECIDED`` makes
        ``resolve`` re-check the fresh status inside the same lock as its
        write and, when the decision won, skip the append and hand back
        ``None`` instead of overwriting it.

        Never fatal and never silent. The component's work is done and
        saved, so a broken inbox must not fail the run. The items stay
        open, which is the state the operator already saw, and the
        warning names the component and the error in the run's output.
        """
        comment = f"{comp_id} completed in run {self.run_id}"
        if detail:
            comment = f"{comment}: {detail}"
        try:
            if self._inbox is None:
                if not self.inbox_config.enabled:
                    return
                self._inbox = self._open_inbox()
            undecided = [
                item
                for item in self._inbox.items()
                if item.component == comp_id and item.status in UNDECIDED
            ]
            for item in undecided:
                resolved = self._inbox.resolve(item.id, comment=comment, only_from=UNDECIDED)
                if resolved is not None:
                    self.ui.info(f"  Inbox: resolved {item.id[:8]} ({item.kind}): {comment}")
        except (OSError, TypeError, ValueError, InboxError, ControlStateError) as exc:
            # The tuple _inbox_resolve catches, for the reasons it gives.
            self.ui.warn(
                f"  Inbox resolve for {comp_id} failed (non-fatal; its items stay open): {exc}"
            )

    def _inbox_suppress_generic(self, comp_id: str) -> None:
        """Mark that a typed item already covers this component's halt.

        fail() emits a generic halted_run for every terminal failure; a
        budget halt (or a parked merge gate) has already raised a more
        specific item, and two items for one event burn two cap slots and
        bury the reason.
        """
        self._inbox_typed.add(comp_id)

    def _inbox_add(
        self,
        kind: ItemKind,
        title: str,
        *,
        detail: str = "",
        component: str = "",
        dedupe_key: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Record an exception for a human (R8.3). Never fatal.

        Every terminal halt routes through here, so the inbox reflects
        what actually happened rather than what someone remembered to
        report. Bookkeeping must not be able to fail a run, so a broken
        inbox degrades to a warning - but it warns, because a silently
        empty inbox reads exactly like a clean run.
        """
        try:
            if self._inbox is None:
                if not self.inbox_config.enabled:
                    self._inbox_disabled = True
                    return
                self._inbox = self._open_inbox()
            if self._inbox_disabled:
                return
            self._inbox.add(
                kind,
                title,
                detail=detail,
                component=component,
                run_id=self.run_id,
                dedupe_key=dedupe_key,
                evidence=evidence or {},
                notify=self.notify,
            )
        except (OSError, TypeError, ValueError, ControlStateError) as exc:
            # ControlStateError is a RuntimeError: every Inbox write takes
            # the control lock, and the (OSError,
            # ValueError) pair all seven inbox sites were written with
            # does not catch what that lock raises. TypeError was
            # InboxConfig.load's per-key cast, which #192 moved to
            # ``__init__``; it stays for the reason _inbox_resolve gives.
            self.ui.warn(f"  Inbox write failed (non-fatal): {exc}")

    def _park_decision(self, comp_id: str) -> InboxItem | None:
        """The merge_gate item that parked ``comp_id``, or None when unreadable."""
        try:
            if self._inbox is None:
                if not self.inbox_config.enabled:
                    return None
                self._inbox = self._open_inbox()
            return self._inbox.find_by_dedupe_key(park_dedupe_key(comp_id))
        except (OSError, TypeError, ValueError, InboxError, ControlStateError) as exc:
            # The tuple _inbox_resolve catches. Unreadable is not a
            # decision: the component stays parked, and says why.
            self.ui.warn(f"  Inbox read failed; '{comp_id}' stays parked: {exc}")
            return None

    def snapshot_waivers(self) -> None:
        """#595: read the approved policy_exception items once.

        Called by the factory right after ``apply_merge_decisions``: after
        every pre-spend refusal and before any engineer is scheduled. The
        #192 rule quoted in ``_phase_verify`` applies: an approval made
        mid-run, by an operator or by an engineer running ``ks inbox
        approve`` in its own worktree, does not change what a later
        attempt in this run is held to. It takes effect from the next run.

        Every failure here waives nothing: the checks block exactly as
        they did before approvals were read, and say that the approvals
        were not consulted.
        """
        try:
            if self._inbox is None:
                if not self.inbox_config.enabled:
                    self._approvals = ApprovalSnapshot(unconsulted_reason="the inbox is disabled")
                    return
                self._inbox = self._open_inbox()
            self._approvals = load_approvals(self._inbox)
        except (OSError, TypeError, ValueError, InboxError, ControlStateError) as exc:
            # The tuple _park_decision catches, for the reasons it gives.
            self.ui.warn(f"  Inbox read failed; no approval waives a finding in this run: {exc}")
            self._approvals = ApprovalSnapshot(unconsulted_reason=f"the inbox read failed: {exc}")

    def _waiver_scope(self, comp: Component) -> WaiverScope:
        """What a waiver key binds a finding to: the run's plan and the component."""
        return WaiverScope(
            project=self.manifest.project_name,
            spec_file=self.manifest.spec_file,
            plan_id=comp.plan_id,
            component=comp.id,
        )

    def _waivers_for(self, comp: Component, diff_sha: str) -> Waivers:
        """This run's approvals for ``comp`` that cover ``diff_sha``, or an unconsulted snapshot.

        ``diff_sha`` is the change this attempt is judged on, from
        :meth:`_judged_change`: an approval covers only the change it was
        taken on (#646), so every other diff is asked again.

        ``self._approvals`` is None only when ``snapshot_waivers`` was
        never called for this pipeline - a bug here, not ``ks check``'s
        legitimate ``waivers=None`` (it calls the checks directly and
        never reaches this method). Defaulting to unconsulted keeps that
        bug from reading as "nothing waived silently": the check still
        says why.
        """
        approvals = self._approvals or ApprovalSnapshot(
            unconsulted_reason="snapshot_waivers was not called before this check"
        )
        return approvals.for_scope(self._waiver_scope(comp), diff_sha)
