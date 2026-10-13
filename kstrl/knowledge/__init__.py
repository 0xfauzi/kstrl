"""The knowledge of each component.

The knowledge facts, their scope and the distiller.
"""

from __future__ import annotations

from kstrl.knowledge.fact_scope import authored_paths, paths_by_component
from kstrl.knowledge.knowledge import (
    current_run_id,
    measure_fact_utilization,
    retrieve_knowledge_context,
)
from kstrl.knowledge.knowledge_distill import distill_facts
from kstrl.knowledge.knowledge_facts import KNOWLEDGE_SECTION, KnowledgeConfig

__all__ = [
    "KNOWLEDGE_SECTION",
    "KnowledgeConfig",
    "authored_paths",
    "current_run_id",
    "distill_facts",
    "measure_fact_utilization",
    "paths_by_component",
    "retrieve_knowledge_context",
]
