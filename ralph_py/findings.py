"""E3: structured Finding type for adversarial role outputs.

Before this module, review/security findings funneled through
``Component.review_findings`` as a single rendered string. Downstream
consumers (dashboards, evolution journal, retry-context builder) had to
re-parse that string to recover structure. Strings drift, parsers break,
the typed information is gone.

``Finding`` is the source-of-truth representation. The rendered string at
``Component.review_findings`` is now a **derived view** kept for backward
compatibility with manifest.json readers and PR body rendering. New code
should consume ``Component.findings`` directly.

Phase tag examples: ``"review"`` (Phase 2 reviewer), ``"security"`` (Phase
2.5 security reviewer). Severity uses each role's native taxonomy
(``critical/high/medium/low`` for security, ``fail/advisory`` for review).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_INFRASTRUCTURE_CATEGORY = "infrastructure_error"


@dataclass(frozen=True)
class Finding:
    """One adversarial finding produced by a Phase 2 or 2.5 role.

    Frozen so a list[Finding] is safe to share across threads / pickling
    boundaries; the factory's ProcessPoolExecutor path (Phase C1) needs
    structurally-comparable items.
    """

    phase: str
    category: str
    severity: str
    location: str
    explanation: str
    suggestion: str = ""
    owasp: str = ""
    cwe: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_infrastructure_error(self) -> bool:
        """True when this finding represents a failed role run, not a
        real finding. ``len(findings) == 0`` then means "the role ran
        cleanly"; ``[f for f in findings if not f.is_infrastructure_error]``
        gives the verified-clean subset (E3-infra)."""
        return self.category == _INFRASTRUCTURE_CATEGORY

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "category": self.category,
            "severity": self.severity,
            "location": self.location,
            "explanation": self.explanation,
            "suggestion": self.suggestion,
            "owasp": self.owasp,
            "cwe": self.cwe,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        tags_raw = data.get("tags", []) or []
        if not isinstance(tags_raw, (list, tuple)):
            tags_raw = []
        return cls(
            phase=str(data.get("phase", "")),
            category=str(data.get("category", "")),
            severity=str(data.get("severity", "")),
            location=str(data.get("location", "")),
            explanation=str(data.get("explanation", "")),
            suggestion=str(data.get("suggestion", "")),
            owasp=str(data.get("owasp", "")),
            cwe=str(data.get("cwe", "")),
            tags=tuple(str(t) for t in tags_raw),
        )

    @classmethod
    def infrastructure_error(cls, phase: str, explanation: str) -> Finding:
        """Build a synthetic Finding marking that the given role failed
        to execute (timeout, parse failure, agent crash). Treated by the
        factory as a critical-severity blocker in hard mode (E9 already
        produces this signal via the result-level flag; this lifts it
        into the typed findings stream so `len(findings)==0` is a safe
        proxy for "the role ran cleanly")."""
        return cls(
            phase=phase,
            category=_INFRASTRUCTURE_CATEGORY,
            severity="critical",
            location="",
            explanation=explanation,
            tags=("infrastructure",),
        )

    @classmethod
    def from_review_concern(
        cls,
        category: str,
        severity: str,
        location: str,
        explanation: str,
        suggestion: str = "",
    ) -> Finding:
        """Construct a review-phase Finding with consistent tagging
        (`phase:review`, `category:<X>`). Centralizes the tag format so
        all consumers can rely on it."""
        return cls(
            phase="review",
            category=category,
            severity=severity,
            location=location,
            explanation=explanation,
            suggestion=suggestion,
            tags=("phase:review", f"category:{category}"),
        )

    @classmethod
    def from_security_finding(
        cls,
        category: str,
        severity: str,
        location: str,
        explanation: str,
        suggestion: str,
        owasp: str,
        cwe: str,
    ) -> Finding:
        """Construct a security-phase Finding with OWASP/CWE tags
        populated. Tag set: ``phase:security``, ``category:<X>``, and
        when present ``owasp:<bucket>``, ``cwe:<id>``. Lets downstream
        consumers filter by taxonomy without re-parsing the owasp/cwe
        string fields."""
        tags: list[str] = ["phase:security", f"category:{category}"]
        if owasp:
            tags.append(f"owasp:{owasp}")
        if cwe:
            tags.append(f"cwe:{cwe}")
        return cls(
            phase="security",
            category=category,
            severity=severity,
            location=location,
            explanation=explanation,
            suggestion=suggestion,
            owasp=owasp,
            cwe=cwe,
            tags=tuple(tags),
        )


def render_findings_markdown(findings: list[Finding]) -> str:
    """Render a list[Finding] as a markdown section suitable for a PR
    body or evolution journal. Returns empty string for empty input.

    The output is grouped by phase and includes infrastructure-error
    callouts so the reader can distinguish "no findings" (good) from
    "review never ran" (must investigate)."""
    if not findings:
        return ""
    by_phase: dict[str, list[Finding]] = {}
    for f in findings:
        by_phase.setdefault(f.phase, []).append(f)

    lines: list[str] = ["## Adversarial Findings", ""]
    for phase in sorted(by_phase):
        items = by_phase[phase]
        infra = [f for f in items if f.is_infrastructure_error]
        real = [f for f in items if not f.is_infrastructure_error]

        lines.append(f"### {phase.capitalize()} ({len(real)} findings)")
        lines.append("")
        if infra:
            for f in infra:
                lines.append(
                    f"- **INFRASTRUCTURE ERROR**: {f.explanation} "
                    "(this role did not actually run -- the PR is unverified for this phase)"
                )
            lines.append("")
        for f in real:
            loc = f" at `{f.location}`" if f.location else ""
            tax = ""
            if f.owasp or f.cwe:
                pieces = [p for p in (f.owasp, f.cwe) if p]
                tax = f" [{', '.join(pieces)}]"
            lines.append(
                f"- [{f.severity}] **{f.category}**{loc}{tax}"
            )
            lines.append(f"  - {f.explanation}")
            if f.suggestion:
                lines.append(f"  - Suggestion: {f.suggestion}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
