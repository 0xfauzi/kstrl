"""Every manifest write site is listed with the lock discipline it runs under (#597).

A live factory run holds ``.kstrl/factory.lock`` and saves its whole
in-memory manifest, so a manifest write another command makes outside
that lock is undone at the run's next save, and one made before a
confirmation is made even when the operator answers Quit. #597 found
four such writers (`ks retry`, `ks inbox retry`, `ks decompose` and
`ks factory --spec`); `tests/test_retry_lock_discipline.py` proves each
one's behaviour. This file is the guard for the fifth: a closed census of
every reference to the names that write or stage a manifest change, so a
new site anywhere in ``kstrl/`` is an unexplained census delta that must
be classified here before it can land.

What the walk counts, by construction rather than by a list of shapes:
every ``ast.Attribute`` whose attribute is one of ``WRITE_NAMES`` (a call
``m.save(p)``, an unbound ``Manifest.save(m, p)`` and an alias
``s = m.save`` alike) and every ``ast.Name`` of one of them (``prepare_retry``
imported and called bare). ``save`` is matched on the attribute name, not
the receiver's type, so non-manifest saves are in the census too and are
classified ``not-a-manifest``: a guess about the receiver would be a guess
that goes blind.

Disclosed blind spots: ``getattr(obj, "save")``, and a manifest file
written without ``Manifest.save`` (only ``kstrl/manifest.py`` writes one
today). Neither is a reference this walk can see.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

KSTRL = Path(__file__).resolve().parents[1] / "kstrl"

#: ``Manifest.save`` writes the manifest; ``Manifest.reset_for_retry`` stages
#: a requeue that a save then writes; ``prepare_retry`` calls both for its
#: caller, so each caller decides the discipline and is listed itself.
WRITE_NAMES = frozenset({"save", "reset_for_retry", "prepare_retry"})

IN_RUN = "in-run"  # inside run_factory's lock: _run_factory_locked and what it drives
TAKES_LOCK = "takes-lock"  # takes the run lock before its first change (#597)
CALLER_HOLDS = "caller-holds-lock"  # prepare_retry: its callers are listed below
COPY = "copy"  # a deep copy that is never saved
NOT_A_MANIFEST = "not-a-manifest"
UNGUARDED = "unguarded"  # a disclosed hole; pinned by the last test

DISCIPLINES = frozenset({IN_RUN, TAKES_LOCK, CALLER_HOLDS, COPY, NOT_A_MANIFEST, UNGUARDED})

#: ``"<path>::<qualified function> <name>"`` -> (references, discipline).
EXPECTED_MANIFEST_WRITE_SITES: dict[str, tuple[int, str]] = {
    # --- outside a factory run: each takes the run lock (#597) ---
    "kstrl/cli.py::retry prepare_retry": (1, TAKES_LOCK),
    "kstrl/cli.py::inbox_retry reset_for_retry": (1, TAKES_LOCK),
    "kstrl/cli.py::inbox_retry save": (1, TAKES_LOCK),
    "kstrl/decompose.py::_decompose_spec_impl save": (1, TAKES_LOCK),
    "kstrl/retry_plan.py::prepare_retry reset_for_retry": (1, CALLER_HOLDS),
    "kstrl/retry_plan.py::prepare_retry save": (1, CALLER_HOLDS),
    "kstrl/retry_plan.py::preview_retry reset_for_retry": (1, COPY),
    # The TUI retry screen resets and saves, then launches a factory that
    # takes the lock itself. Not fixed in #597 (UI work deferred by the owner).
    "kstrl/tui/screens/retry.py::RetryScreen._confirm_retry prepare_retry": (1, UNGUARDED),
    # --- inside run_factory's lock ---
    "kstrl/factory.py::_run_factory_locked save": (6, IN_RUN),
    "kstrl/factory.py::_run_factory_locked._cleanup_pass_worktrees save": (1, IN_RUN),
    "kstrl/factory.py::_run_factory_locked._run_scheduling_pass save": (1, IN_RUN),
    "kstrl/factory.py::_stamp_feature_base save": (1, IN_RUN),
    "kstrl/integration_fix.py::append_fix_component save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._fail_pr_flow save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._park_awaiting_approval save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._park_merge_pending save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._phase_pr save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline._record_merge save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.complete save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.fail save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.fail_scheduler_backstop save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.process_result save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.repoll_merge_pending save": (1, IN_RUN),
    "kstrl/pipeline.py::ComponentPipeline.retry_or_fail save": (1, IN_RUN),
    # --- a ``save`` that is not a manifest's ---
    "kstrl/autonomy.py::commit_transition save": (1, NOT_A_MANIFEST),  # ladder state
    "kstrl/autonomy.py::save_ladder_state save": (1, NOT_A_MANIFEST),  # ladder state
    "kstrl/integration.py::write_integration_prd save": (1, NOT_A_MANIFEST),  # PRD
    "kstrl/integration_fix.py::write_fix_prd save": (1, NOT_A_MANIFEST),  # PRD
    "kstrl/pipeline.py::ComponentPipeline._phase_review save": (1, NOT_A_MANIFEST),  # PRD
    "kstrl/serve.py::_save_pr_count_streak save": (1, NOT_A_MANIFEST),  # serve streak
}


class _SiteWalk(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.scope: list[str] = []
        self.sites: Counter[str] = Counter()

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def _site(self, name: str) -> None:
        where = ".".join(self.scope) or "<module>"
        self.sites[f"{self.module}::{where} {name}"] += 1

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in WRITE_NAMES:
            self._site(node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in WRITE_NAMES:
            self._site(node.id)
        self.generic_visit(node)


def census_of(source: str, module: str) -> Counter[str]:
    walk = _SiteWalk(module)
    walk.visit(ast.parse(source))
    return walk.sites


def census() -> Counter[str]:
    total: Counter[str] = Counter()
    for path in sorted(KSTRL.rglob("*.py")):
        module = path.relative_to(KSTRL.parent).as_posix()
        total.update(census_of(path.read_text(encoding="utf-8"), module))
    return total


def test_the_census_matches_the_tree() -> None:
    found = dict(census())
    expected = {site: count for site, (count, _) in EXPECTED_MANIFEST_WRITE_SITES.items()}
    new = {site: n for site, n in found.items() if site not in expected}
    gone = sorted(site for site in expected if site not in found)
    moved = {s: (expected[s], found[s]) for s in expected if s in found and found[s] != expected[s]}
    assert (new, gone, moved) == ({}, [], {}), (
        "A manifest write site changed. Classify each new one in "
        "EXPECTED_MANIFEST_WRITE_SITES with the lock it runs under: a write "
        "outside a factory run takes the run lock before its first change "
        f"(#597). new={new} gone={gone} (expected, found)={moved}"
    )


def test_every_site_names_a_known_discipline() -> None:
    unknown = {s: d for s, (_, d) in EXPECTED_MANIFEST_WRITE_SITES.items() if d not in DISCIPLINES}
    assert unknown == {}


def test_the_walk_sees_every_shape_of_reference() -> None:
    """Positive control: a walk that stops seeing a shape fails here, not silently."""
    source = (
        "from kstrl.retry_plan import prepare_retry\n"
        "import kstrl.retry_plan as rp\n"
        "class Screen:\n"
        "    def confirm(self, m, p):\n"
        "        m.save(p)\n"
        "        Manifest.save(m, p)\n"
        "        alias = m.save\n"
        "        m.reset_for_retry('c')\n"
        "        prepare_retry(m, 'c', p, p, None)\n"
        "        rp.prepare_retry(m, 'c', p, p, None)\n"
        "def top():\n"
        "    def inner(m, p):\n"
        "        m.save(p)\n"
    )

    assert census_of(source, "kstrl/x.py") == Counter(
        {
            "kstrl/x.py::Screen.confirm save": 3,
            "kstrl/x.py::Screen.confirm reset_for_retry": 1,
            "kstrl/x.py::Screen.confirm prepare_retry": 2,
            "kstrl/x.py::top.inner save": 1,
        }
    )


def test_the_only_unguarded_site_is_the_disclosed_tui_retry() -> None:
    unguarded = {s for s, (_, d) in EXPECTED_MANIFEST_WRITE_SITES.items() if d == UNGUARDED}
    assert unguarded == {"kstrl/tui/screens/retry.py::RetryScreen._confirm_retry prepare_retry"}
