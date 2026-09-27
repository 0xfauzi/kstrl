"""`ks factory --spec` keeps its run lock held through decompose and into the run (#597).

Split out of ``tests/test_retry_lock_discipline.py`` (the file-length
ratchet: that file already carries the other four #597 lock-discipline
proofs and this one would have crossed 800 lines). Same technique: the
real CLI, a real git repository, a stub agent, no LLM.

``held_or_acquired_run_lock`` (``kstrl/factory.py``) is used both by
``ks decompose`` (which owns no caller's lock, acquires its own around
the manifest-write block and releases it there) and by `ks factory
--spec` (which hands in the lock it already took above the manifest
load, before the architect spends anything, #597) - so decompose's own
manifest-write block must not release a lock it does not own. The test
below drives `ks factory --spec` end to end with an agent that serves
both the architect's single decompose call and the engineer worker that
follows (the CLI passes one ``--agent-cmd`` for the whole run), telling
them apart by a call counter on disk since the calls differ in role, not
in the argv the CLI invokes them with; the engineer's own invocation
probes ``.kstrl/factory.lock`` with ``fcntl`` to say whether the lock
the CLI took is still held once it starts.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from tests.test_build_manifest_preflight import MANIFESTS, greenfield, run_ks
from tests.test_decompose import _single_component_output, _story

#: The --spec path's own lock-probing agent (mirrors
#: ``tests/test_retry_lock_discipline.py``'s ``_LOCK_PROBING_ENGINEER``):
#: the SAME ``--agent-cmd`` serves both the architect's decompose call and
#: every engineer worker of the run that follows, so this tells them apart
#: by a call counter on disk rather than by content. The first invocation
#: (the architect) echoes back the canned single-component manifest JSON;
#: every later invocation (the engineer) probes the run lock and records
#: "held" or "free" before completing.
_SPEC_LOCK_PROBING_AGENT = """
import fcntl, pathlib, sys

calls_path, reply_path, lock_path, seen_path = sys.argv[1:5]
sys.stdin.read()

calls = pathlib.Path(calls_path)
n = int(calls.read_text()) if calls.exists() else 0
calls.write_text(str(n + 1))

if n == 0:
    sys.stdout.write(pathlib.Path(reply_path).read_text())
else:
    with open(lock_path, "a+", encoding="utf-8") as fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            verdict = "free"
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            verdict = "held"
    with open(seen_path, "a", encoding="utf-8") as out:
        out.write(verdict + "\\n")
    print("<promise>COMPLETE</promise>")
"""


class TestSpecFactoryHoldsTheLockThroughDecomposeIntoTheRun:
    def test_a_spec_run_holds_the_lock_from_decompose_into_the_engineer(
        self, tmp_path: Path
    ) -> None:
        """`ks factory --spec` keeps the lock it took held through decompose and into the run.

        ``held_or_acquired_run_lock`` (factory.py) must not release a lock
        it does not own: `ks factory --spec` takes the run lock before
        calling ``decompose_spec`` (cli.py's own ``_resolve_factory_run_lock``,
        above the manifest load and before the architect spends anything,
        #597) and hands it in, so decompose's own use of the context
        manager - where ``owns = run_lock is None`` is False here - must
        leave it held for the paid engineer run that follows. A probing
        engineer that finds the lock free here is the regression: the
        caller's own lock quietly released once the architect returned,
        so the run that follows executes with no lock held at all.
        """
        root = greenfield(tmp_path, extra={"pyproject.toml": MANIFESTS["pyproject.toml"]})
        reply = tmp_path / "architect.json"
        reply.write_text(_single_component_output([_story()]), encoding="utf-8")
        calls = tmp_path / "calls.txt"
        seen = tmp_path / "lock_seen_by_engineer.txt"
        engineer = tmp_path / "spec_engineer.py"
        engineer.write_text(_SPEC_LOCK_PROBING_AGENT, encoding="utf-8")
        agent = " ".join(
            shlex.quote(part)
            for part in (
                sys.executable,
                str(engineer),
                str(calls),
                str(reply),
                str(root / ".kstrl" / "factory.lock"),
                str(seen),
            )
        )

        proc = run_ks(
            root,
            "factory",
            "--spec",
            str(root / "spec.md"),
            "--project-name",
            "demo",
            "--root",
            str(root),
            "--agent-cmd",
            agent,
            "--yes",
            "--no-tui",
            "--max-parallel",
            "1",
            "--max-retries",
            "0",
            "--no-prs",
            "--review-mode",
            "skip",
            "--contract-check",
            "skip",
            "--test-command",
            "true",
            "--typecheck-command",
            "true",
            "--lint-command",
            "true",
            "--ui",
            "plain",
            "--no-color",
        )

        # comp-a's PRD story still does not pass, so Phase 1 fails it (1),
        # never a lock refusal (2) - decompose itself was never contested.
        assert proc.returncode == 1, proc.stdout
        assert "Starting:" in proc.stdout, proc.stdout
        assert seen.read_text(encoding="utf-8").split() == ["held"], proc.stdout
