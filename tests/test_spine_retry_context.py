"""Spine tier II (R4.2): retry-context propagation, end to end.

tests/test_verify.py proves check_diff_scope's failure DETAILS carry the
base branch and the full allowed-paths list (R0.4), and unit tests prove
IterationContext formatting - but nothing proved the whole pipe: a real
engineer failing Phase 1 in a real worktree, the factory rebuilding the
retry prompt, and the NEXT engineer invocation actually receiving it.

Here the engineer is a real ``bash -lc`` subprocess that writes the
prompt it received on stdin to a file (mock-free capture at the exact
boundary the retry context must cross). Attempt 1 commits an
out-of-scope file and fails diff-scope; the test asserts attempt 2's
prompt contains the failure details INCLUDING the base branch and the
complete allowed-paths list (wave-2 behavior), then attempt 2 fixes the
scope violation and the component completes.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from kstrl.factory import run_factory
from kstrl.manifest import ComponentStatus
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.spine_utils import (
    base_config,
    component,
    factory_config,
    init_kstrl_repo,
    make_manifest,
)

pytestmark = pytest.mark.spine

COMP = "comp-a"


class TestRetryContextPropagation:
    def test_diff_scope_failure_details_reach_attempt_two_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_kstrl_repo(root, (COMP,))
        # PRD with an allowedPaths scope: only src/ may change.
        prd_path = root / "scripts" / "kstrl" / "feature" / COMP / "prd.json"
        prd_path.write_text(json.dumps({
            "branchName": f"kstrl/factory/{COMP}",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
            "allowedPaths": ["src/"],
        }))

        cap_dir = tmp_path / "prompts"
        cap_dir.mkdir()
        marker = tmp_path / "first-attempt-done"
        # Attempt 1: capture the prompt, commit an in-scope file AND an
        # out-of-scope one. Attempt 2: capture the prompt, then remove
        # the out-of-scope file (the branch is resumed with attempt 1's
        # commit, so the fix must revert it).
        engineer = textwrap.dedent(f"""\
            if [ -f '{marker}' ]; then
              cat > '{cap_dir}/attempt2.prompt'
              git rm -q evil.txt
              git commit -q -m 'remove out-of-scope file'
            else
              touch '{marker}'
              cat > '{cap_dir}/attempt1.prompt'
              mkdir -p src
              echo ok > src/ok.txt
              echo evil > evil.txt
              git add src/ok.txt evil.txt
              git commit -q -m 'in-scope and out-of-scope files'
            fi
            echo '<promise>COMPLETE</promise>'
        """)

        manifest = make_manifest([component(COMP)])
        progress_path = tmp_path / "progress.jsonl"
        result = run_factory(
            manifest,
            factory_config(
                max_retries=1,
                progress_log_path=progress_path,
                verify_config=VerifyConfig(
                    test_command="true", typecheck_command="true",
                    lint_command="true", check_diff_scope=True,
                    check_bad_patterns=False, subprocess_timeout=10.0,
                ),
            ),
            base_config(root, engineer),
            PlainUI(no_color=True),
            root,
        )

        # The retry recovered: attempt 2 fixed the scope violation.
        assert result.exit_code == 0
        assert result.completed == [COMP]
        comp = manifest.get_component(COMP)
        assert comp is not None
        assert comp.status == ComponentStatus.COMPLETED.value
        assert comp.retries == 1

        # Attempt 1 ran with no inherited context.
        attempt1 = (cap_dir / "attempt1.prompt").read_text()
        assert "PREVIOUS ATTEMPT CONTEXT" not in attempt1

        # Attempt 2's prompt carries the diff-scope failure verbatim,
        # INCLUDING the base branch and the complete allowed-paths list
        # (R0.4: without them the retry agent guessed the base and
        # reverted base-branch content, failing again).
        attempt2 = (cap_dir / "attempt2.prompt").read_text()
        assert "PREVIOUS ATTEMPT CONTEXT" in attempt2
        assert "## Verification Failures" in attempt2
        assert "diff_scope: FAIL" in attempt2
        assert "Base branch: main" in attempt2
        assert "Allowed paths (complete list): src/" in attempt2
        assert "evil.txt" in attempt2
        # The anti-footgun instruction rides along with the base branch.
        assert "do NOT `git checkout main -- <path>`" in attempt2

        # The journal agrees with the prompts: one failed verification,
        # one retry, one passing verification.
        events = [
            json.loads(line)
            for line in progress_path.read_text().splitlines()
            if line.strip()
        ]
        verifications = [
            e["data"]["passed"] for e in events
            if e["event"] == "verification_result"
        ]
        assert verifications == [False, True]
        retries = [e for e in events if e["event"] == "component_retrying"]
        assert len(retries) == 1
        assert retries[0]["data"]["reason"] == "Mechanical verification failed"
