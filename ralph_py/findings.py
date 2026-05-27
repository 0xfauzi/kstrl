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
