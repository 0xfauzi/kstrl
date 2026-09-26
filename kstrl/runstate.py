"""The mutable structures one factory run shares with its pipeline.

Not frozen and never copied. Every field is an ALIAS: the factory's
scheduler and :class:`kstrl.pipeline.ComponentPipeline` hold the same
objects, and the writes cross between them in both directions. Freezing
this dataclass, or rebuilding it at a phase boundary, or handing a
``dict(...)`` / ``set(...)`` / ``copy.copy(...)`` of a field to the
constructor, disconnects the two halves. The five plants below at the
construction site were not all measured in one run: see the
provenance column, and the paragraph after the table for the exact
attribution.

==================================  ======================  =======  ========================
plant at the construction site      result                  verdict  provenance
==================================  ======================  =======  ========================
``worktree_paths=dict(...)``        4 failed / 6977 passed  caught   design panel, not re-run
``component_failure_signatures=``   1 failed / 256 passed   caught   design panel, not re-run
``component_contexts=dict(...)``    6 failed / 6975 passed  caught   design panel, not re-run
``factory_result=FactoryResult()``  11 failed / 176 passed  caught   this lane
``fresh_base_retry_ids=set(...)``   187 passed              SILENT   this lane
==================================  ======================  =======  ========================

The two rows marked "this lane" (``factory_result`` and
``fresh_base_retry_ids``) were measured in this lane against the
187-passed focused-set control of ``tests/test_factory.py``,
``tests/test_pipeline.py`` and ``tests/test_timeout_enforcement.py``.
The three rows marked "design panel, not re-run" were not re-run
here: ``worktree_paths`` (4 failed / 6977 passed) and
``component_contexts`` (6 failed / 6975 passed) are FULL-SUITE
numbers from the design panel's lane; ``component_failure_signatures``
(1 failed / 256 passed) is a separate focused set from that same
lane. None of the three was re-derived in this lane.

``fresh_base_retry_ids`` is the one nothing saw. Its writer and its
reader are 2,400 lines apart - the pipeline ``.add``s a component whose
attempt was killed, the scheduler ``.discard``s it and passes
``fresh_from_base=True`` into ``_setup_worktree`` - and a copy makes the
retry resume on the killed attempt's dirty branch with every gate green.
``tests/test_timeout_enforcement.py::TestFreshBaseRetryReachesTheScheduler``
is the test that now fails on it.

The ownership rule, which lived only in ``ComponentPipeline``'s class
docstring until #193 and was stated per field there when it is actually
per OPERATION:

=============================== ========================= =========================
structure                       factory writes            pipeline writes
=============================== ========================= =========================
``worktree_paths``              insert (``_launch_comp``) delete (``_cleanup_...``)
``component_contexts``          none (reads at submit)    set
``fresh_base_retry_ids``        ``.discard``              ``.add``
``component_bases``             set (``_launch_comp``)    none (reads)
``component_failure_signatures``set (contract breaker)    set / pop
``factory_result``              summary + exit code       append completed/failed
=============================== ========================= =========================

Nothing rebinds a field after construction: measured by
``grep -rnE "(self|pipeline|state)\\.(worktree_paths|component_contexts|
fresh_base_retry_ids|component_failure_signatures|factory_result)\\s*="``
over ``kstrl/`` and ``tests/``, which returns only this module's own
field declarations. That is why ``ComponentPipeline`` exposes the five
as read-only properties and needs no setters.

``leaked_component_ids`` is deliberately NOT here. Its writer
(``_run_scheduling_pass``) and its reader (``_cleanup_pass_worktrees``)
are both closures of ``_run_factory_locked`` and it never reaches the
pipeline, so it is a local of one function rather than state shared
between two objects. The membership rule for this dataclass is "passed
from the factory to ``ComponentPipeline``", which a reader can check
against one constructor; "any mutable local of the scheduler" has no
bound.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # kstrl.factory imports this module at run time, so FactoryResult
    # cannot be imported here at run time. ``from __future__ import
    # annotations`` makes the annotation a string and nothing in kstrl/
    # or tests/ calls get_type_hints on it. kstrl/pipeline.py uses the
    # same idiom for the same reason.
    from kstrl.factory import FactoryResult


@dataclass(kw_only=True)
class RunState:
    """One run's shared mutable state. See the module docstring.

    ``kw_only=True`` because three fields are dicts and two of those are
    ``dict[str, ...]``-shaped: mypy --strict accepts a positional
    transposition of same-typed fields (reproduced on #193), and the
    struct is expected to grow.
    """

    factory_result: FactoryResult
    worktree_paths: dict[str, Path] = field(default_factory=dict)
    component_contexts: dict[str, str] = field(default_factory=dict)
    fresh_base_retry_ids: set[str] = field(default_factory=set)
    # #543: what each component's change is judged against, written when
    # its branch is cut (first provisioning, or a retry that recreates the
    # branch) and read by every phase that diffs. A component absent here
    # is judged against the manifest's base branch.
    component_bases: dict[str, str] = field(default_factory=dict)
    # R6.1: structured "<check>:<code>" failure signatures per component
    # (e.g. "linter:E501", "review:scope_creep"), recorded at each
    # failure site from the parser/finding stream and handed to the
    # evolution journal at record_run. In-memory only: the manifest
    # already persists failed_phase/failed_check; the full signature
    # list is a journal concern. This comment moved here from
    # kstrl/factory.py on #193, with the declaration it describes.
    component_failure_signatures: dict[str, list[str]] = field(default_factory=dict)
