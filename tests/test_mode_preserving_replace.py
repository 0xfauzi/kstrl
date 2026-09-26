"""A closed-by-construction census of ``os.replace`` and ``shutil.move``
call sites outside ``kstrl/atomicio.py`` (#152 simplify pass, Group C1).

The mode-restore this round adds to ``_restore_mutated_sources``
(``kstrl/verify.py``) fixes mutmut and nothing else. ``tests/test_atomicio.py``
keys its own guard on ``tempfile.mkstemp``, so a hand-rolled
``os.replace``/``shutil.move`` over an existing FILE is invisible to it,
and this round's own ``os.replace(bak, target)`` is the first such site
outside ``kstrl/atomicio.py``. CLAUDE.md's rule (the most-repeated defect
in this repo, "a static guard fails in the skip direction") applies here
in full: prefer CLOSED BY CONSTRUCTION over a ledger of give-ups, so this
walks EVERY ``os.replace``/``shutil.move`` call in ``kstrl/`` rather than
maintaining a list of the ones somebody already noticed, and it CLEARS
sites, so where it cannot prove a site compliant it FLAGS rather than
passes - the direction a clearing guard must be narrow in
(``tests/helpers/astwalk/__init__.py``, "thirteen of the sixteen migrated
guards flag while three clear").

Compliance, for a site outside ``kstrl/atomicio.py``, is not something
this AST walk can prove on its own: "the destination's mode is captured
before and restored after" is a RUNTIME property no static check can
verify by reading the call site alone. What the walk CAN do, and does,
is total the acquisition - every site that exists today - so a NEW one
shows up as an unexplained census delta rather than silence, and the
per-site reason a human already read into :data:`REPLACE_SITES` is
re-justified in this docstring rather than merely asserted.

Five sites exist today (measured: ``uv run python3`` AST-walking
``kstrl/`` for ``os.replace``/``shutil.move`` calls, resolving import
aliases the way ``tests/helpers/astwalk`` does): one inside
``kstrl/atomicio.py`` itself (the canonical mode-preserving helper, exempt
by construction - it is the thing every OTHER writer in the package is
supposed to route through), and four outside it:

- ``kstrl/verify.py::_restore_mutated_sources`` - ``os.replace(bak, target)``.
  Accompanied by a mode capture and restore: :func:`kstrl.verify._target_modes`
  reads every target's permission bits BEFORE any spawn, in
  ``_mutmut_run_spawn``, and :func:`kstrl.verify._restore_mutated_sources`
  reapplies them UNCONDITIONALLY in the same ``finally`` that performs this
  replace (#152's own defect C). This is the one PROVEN compliant by
  reading the surrounding function, not merely declared so.
- ``kstrl/workqueue.py::WorkQueue.enqueue`` - ``os.replace(str(staging),
  str(directory))``. The destination NEVER pre-exists: `directory.exists()`
  is checked and refused (``raise QueueError``) immediately above, before
  ``staging`` is even built, so there is no prior mode for this replace to
  overwrite. No capture/restore is needed because there is nothing to
  restore.
- ``kstrl/workqueue.py::WorkQueue.transition`` (approximate name) -
  ``os.replace(str(source_dir), str(target_dir))``. Same shape: `if
  target_dir.exists(): raise QueueError(...)` guards it immediately above.
- ``kstrl/statedir.py::_move_control_file`` - ``os.replace(src, dst)``.
  Its own single caller, ``migrate_control_state``, checks ``dst_present``
  and ``continue``s BEFORE calling this function at all, so ``dst`` never
  pre-exists at the point this line runs either.

SYMLINK IDENTITY is the other half of the property this file's subject
shares with ``kstrl/atomicio.py`` (`os.replace` swaps the directory entry,
so replacing over a symlinked destination turns it into a regular file),
and is handled, or explicitly declined, at each of the four non-atomicio
sites: the three "destination never pre-exists" sites have no destination
to convert (there is nothing at the target path for `os.replace` to swap
out from under); `kstrl/verify.py::_restore_mutated_sources`'s own
docstring carries an explicit DECLINE paragraph (#152 simplify pass, C1)
rather than silence, arguing the same two reasons rehearsed there:
mutmut itself would mutate through a symlinked target the same way this
function's restore would, and `_preexisting_backups` already refuses the
one shape (a `.bak` already on disk) that would make restoring through a
symlink actively destructive.
"""

from __future__ import annotations

import ast

from tests.helpers import astwalk

#: ``os.replace`` and ``shutil.move`` - the two stdlib calls that swap a
#: directory entry and so can silently carry a source file's permission
#: bits onto an existing destination (measured: mutmut 2.5.1's own
#: ``mutate_file``/``run_mutation`` do exactly this, #152's own defect C).
REPLACE_TARGETS = ("os.replace", "shutil.move")

#: Every site outside ``kstrl/atomicio.py`` today, keyed by module and
#: expression (never by line - an edit above the site must not fail this
#: pin for a reason that is not the guard's subject), with the one-line
#: reason a human read into the module docstring above. A NEW site is not
#: in this dict, so :func:`test_every_replace_site_is_accounted_for`
#: FLAGS it rather than silently passing - the direction a clearing guard
#: must fail in.
REPLACE_SITES: dict[str, str] = {
    "verify.py os.replace(bak, target)": (
        "accompanied by _target_modes (capture, before any spawn) and "
        "_restore_mutated_sources (unconditional restore, same finally) - "
        "#152's own defect C fix"
    ),
    "workqueue.py os.replace(str(staging), str(directory))": (
        "destination never pre-exists: `if directory.exists(): raise "
        "QueueError(...)` guards immediately above"
    ),
    "workqueue.py os.replace(str(source_dir), str(target_dir))": (
        "destination never pre-exists: `if target_dir.exists(): raise "
        "QueueError(...)` guards immediately above"
    ),
    "statedir.py os.replace(src, dst)": (
        "destination never pre-exists: migrate_control_state checks "
        "dst_present and `continue`s before calling this function at all"
    ),
}

#: Calls this walk cannot decide are or are not `os.replace`/`shutil.move`
#: (measured, not guessed: `uv run pytest tests/test_mode_preserving_replace.py`
#: prints exactly this set if it ever changes). Two shapes, matching
#: `tests/test_atomicio.py`'s own disclosed blind spot: a receiver the
#: walk cannot trace to `os`/`shutil` whose CALL shares a leaf name with
#: the target set (`str.replace`, `datetime.replace`, `dict.move` are
#: not real methods but `.replace(...)`/`.move(...)` reads identically
#: to the AST until the receiver is known - measured: 21 of these are
#: plain `.replace(...)` calls on a string or a datetime, none of them
#: anywhere near a filesystem path), and a callee with NO identifier at
#: all (`TABLE[key](...)`, `initial_screens_for_kind(...)`), which is a
#: candidate for EVERY target set because there is nothing in the AST to
#: compare - the same four `gateparse.py`/`tui/app.py` sites
#: `tests/test_atomicio.py::EXPECTED_UNDECIDED_CALLS` already discloses
#: for `tempfile.mkstemp`. A NEW entry here is either a call this walk
#: needs help resolving (make it resolvable, or add a reason and enrol
#: it) or a genuine fifth `os.replace`/`shutil.move` site hiding behind
#: an unresolvable receiver - read it before assuming the former.
EXPECTED_UNDECIDED_REPLACE_CALLS: tuple[str, ...] = (
    "agents/codex.py text.replace",
    "autonomy.py datetime.now(UTC).replace",
    "autonomy.py trigger.label.replace",
    "baseline_report.py ' '.join(text.split()).replace",
    "decompose.py comp_id.replace",
    "decompose.py comp_id.replace('-', ' ').replace",
    "doctor.py now.isoformat().replace",
    # #433 inc3: str.replace spelling a slug as words, here and in the four
    # tui/ rows below, except inbox.py's `moment`, a datetime's tzinfo.
    "evolve_report.py name.replace",
    "gateparse.py TOOL_PARSERS[chosen]",
    "gateparse.py TOOL_PARSERS[name]",
    "inbox.py datetime.now(UTC).replace",
    "inbox.py parsed.replace",
    "init_cmd.py text.replace",
    "knowledge.py raw_output[:200].replace",
    "licensing.py low.replace",
    "observability.py datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace",
    "parsers.py failure.rule_or_test.replace",
    "playbook.py datetime.now(UTC).replace",
    "playbook.py datetime.now(UTC).replace(microsecond=0).isoformat().replace",
    # #526: `retry_plan._opt` spells a run limit as an option name, a str.replace.
    "retry_plan.py name.replace",
    "serve.py (local + timedelta(days=1)).replace",
    "statedir.py datetime.now(UTC).replace",
    "statedir.py datetime.now(UTC).replace(microsecond=0).isoformat().replace",
    "tui/app.py initial_screens_for_kind(kind, observe_only=False)",
    "tui/app.py initial_screens_for_kind(kind, observe_only=True)",
    # #433 inc4: the ledger's own timestamp parse, a datetime's tzinfo,
    # same shape as observability.py's and inbox.py's `moment` above.
    "tui/delivery.py datetime.strptime(observed_at, '%Y-%m-%dT%H:%M:%SZ').replace",
    "tui/inbox_consequences.py str(kind).replace",
    "tui/screens/decompose.py key.replace",
    "tui/screens/inbox.py key.replace",
    "tui/screens/inbox.py moment.replace",
    "verify.py text.replace",
    "verify.py text.replace('\\r\\n', '\\n').replace",
    "workqueue.py parsed.replace",
)


def _non_atomicio_replace_sites() -> astwalk.Sites:
    """Every ``os.replace``/``shutil.move`` call in ``kstrl/`` outside
    ``kstrl/atomicio.py``, resolved the way ``tests/test_atomicio.py``'s
    ``mkstemp`` census resolves ``tempfile.mkstemp`` - import aliases,
    rebinds, and attribute chains included - and everything it cannot
    resolve reported UNDECIDED rather than silently dropped.

    Built on :func:`astwalk.resolved_calls` rather than
    :func:`astwalk.calls_to` directly: ``calls_to``'s own ``seen`` rows
    carry only the RESOLVED DOTTED NAME (``"os.replace"``), which is the
    same string for workqueue.py's two distinct call sites and would
    collapse them into one row after ``without_line_numbers()`` drops the
    number that was the only thing telling them apart. ``resolved_calls``
    hands back the NODE, so the row here is the module label plus the
    unparsed call expression - distinct for each site, stable across an
    edit that moves the line, and the same key shape
    :data:`REPLACE_SITES` is written against.
    """
    seen: dict[str, str] = {}
    undecided: list[str] = []
    for source_file in astwalk.package_sources():
        if source_file.name == "atomicio.py":
            continue
        tree = astwalk.parsed(source_file)
        where = astwalk.label(source_file)
        module = astwalk.module_name(source_file)
        for node, _dotted in astwalk.resolved_calls(tree, REPLACE_TARGETS, module=module):
            key = f"{where} {ast.unparse(node)}"
            seen[key] = key
        undecided.extend(
            astwalk.calls_to(tree, REPLACE_TARGETS, where=where, module=module).undecided
        )
    return astwalk.Sites(
        tuple(sorted(seen.values())), tuple(sorted(undecided))
    ).without_line_numbers()


class TestEveryReplaceSiteIsAccountedFor:
    """The mechanised half of this file's own docstring: a site this walk
    finds and :data:`REPLACE_SITES` does not name is a FLAG, never a
    silent pass."""

    def test_every_replace_site_is_accounted_for(self) -> None:
        found = _non_atomicio_replace_sites()
        assert found.undecided == EXPECTED_UNDECIDED_REPLACE_CALLS, (
            "the undecided set moved. A NEW row here is either a call this "
            "walk needs help resolving, or a genuine os.replace/shutil.move "
            "site hiding behind an unresolvable receiver - read it before "
            "assuming the former, then update EXPECTED_UNDECIDED_REPLACE_CALLS "
            f"with the reason. Found: {found.undecided}"
        )
        # `found.seen` rows are already "<label> <expression>" (line
        # numbers stripped by `.without_line_numbers()`), the same key
        # shape `REPLACE_SITES` uses, so no further splitting is needed.
        found_keys = frozenset(found.seen)
        known = frozenset(REPLACE_SITES)
        unexplained = found_keys - known
        assert unexplained == frozenset(), (
            "a NEW os.replace/shutil.move site exists outside kstrl/atomicio.py "
            f"and is not in REPLACE_SITES: {sorted(unexplained)}. Either move the "
            "write through kstrl.atomicio, or add a mode capture/restore beside "
            "it and record the reason as a new REPLACE_SITES row (never edit or "
            "drop an existing one - see CLAUDE.md's guard-census rule)."
        )
        stale = known - found_keys
        assert stale == frozenset(), (
            f"REPLACE_SITES names a site the walk no longer finds: {sorted(stale)}. "
            "If the site moved rather than disappeared, update its row; if it was "
            "deleted, remove the row (this is the one direction removal is safe: "
            "the code that needed the exemption is gone)."
        )

    def test_the_net_actually_fires(self) -> None:
        """The control :func:`assert_census`-style guards require: proof
        this walk's predicate matches something, so an empty result above
        cannot be confused with a switched-off walk. A throwaway module
        with a bare `os.replace` call, no import alias tricks."""
        tree = ast.parse("import os\nos.replace('a', 'b')\n")
        found = astwalk.calls_to(tree, REPLACE_TARGETS, where="control.py", module="control")
        assert found.seen == ("control.py:2 os.replace",), found
        resolved = astwalk.resolved_calls(tree, REPLACE_TARGETS, module="control")
        assert len(resolved) == 1
        assert ast.unparse(resolved[0][0]) == "os.replace('a', 'b')"

    def test_atomicio_py_itself_is_excluded_from_the_census(self) -> None:
        """`kstrl/atomicio.py`'s own `os.replace` is the canonical
        mode-preserving helper every other writer routes through - it is
        not a fifth site to account for, it is the reason the other four
        do not all need to be atomicio.py themselves. Proven by re-running
        the walk WITHOUT the exclusion and checking atomicio.py's site
        shows up there (so the exclusion is doing something, not skipping
        a module that had nothing anyway)."""
        atomicio = next(p for p in astwalk.package_sources() if p.name == "atomicio.py")
        tree = astwalk.parsed(atomicio)
        found = astwalk.calls_to(
            tree, REPLACE_TARGETS, where=astwalk.label(atomicio), module="kstrl.atomicio"
        )
        assert found.seen != (), "atomicio.py's own os.replace went undetected by the walk"
