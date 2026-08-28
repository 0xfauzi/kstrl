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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_kstrl_repo(root, (COMP,))
        # PRD with an allowedPaths scope: only src/ may change.
        prd_path = root / "scripts" / "kstrl" / "feature" / COMP / "prd.json"
        prd_path.write_text(
            json.dumps(
                {
                    "branchName": f"kstrl/factory/{COMP}",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                    "allowedPaths": ["src/"],
                }
            )
        )

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
                    test_command="true",
                    typecheck_command="true",
                    lint_command="true",
                    check_diff_scope=True,
                    check_bad_patterns=False,
                    subprocess_timeout=10.0,
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
        assert "## Current failures (measured in attempt 1, engineer)" in attempt2
        assert "diff_scope: FAIL" in attempt2
        assert "Base branch: main" in attempt2
        assert "Allowed paths (complete list): src/" in attempt2
        assert "evil.txt" in attempt2
        # The anti-footgun instruction rides along with the base branch.
        assert "do NOT `git checkout main -- <path>`" in attempt2

        # The journal agrees with the prompts: one failed verification,
        # one retry, one passing verification.
        events = [
            json.loads(line) for line in progress_path.read_text().splitlines() if line.strip()
        ]
        verifications = [e["data"]["passed"] for e in events if e["event"] == "verification_result"]
        assert verifications == [False, True]
        retries = [e for e in events if e["event"] == "component_retrying"]
        assert len(retries) == 1
        assert retries[0]["data"]["reason"] == "Mechanical verification failed"

    def test_fixed_failure_is_not_re_rendered_on_the_next_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R10.2 (#223): three attempts, two different failures.

        Attempt 1 fails diff-scope. Attempt 2 fixes the scope violation
        and fails the linter instead. Attempt 3's prompt must carry
        attempt 2's failure and NOT attempt 1's: the old renderer put
        both under "Fix ALL issues listed above", so the agent was told
        to re-fix a violation it had already fixed. Attempt 1's scope
        violation is caught by the in-loop guard, which ranks as
        engineer; Phase 1 running in attempt 2 proves the engineer loop
        finished, so it is retired rather than merely un-re-measured.
        """
        monkeypatch.setenv("KSTRL_KNOWLEDGE_ENABLED", "0")
        root = tmp_path / "repo"
        init_kstrl_repo(root, (COMP,))
        prd_path = root / "scripts" / "kstrl" / "feature" / COMP / "prd.json"
        prd_path.write_text(
            json.dumps(
                {
                    "branchName": f"kstrl/factory/{COMP}",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                    "allowedPaths": ["src/"],
                }
            )
        )

        cap_dir = tmp_path / "prompts"
        cap_dir.mkdir()
        m1 = tmp_path / "attempt-1-done"
        m2 = tmp_path / "attempt-2-done"
        engineer = textwrap.dedent(f"""\
            if [ -f '{m2}' ]; then
              cat > '{cap_dir}/attempt3.prompt'
              git rm -q src/LINTFAIL
              git commit -q -m 'fix the lint failure'
            elif [ -f '{m1}' ]; then
              touch '{m2}'
              cat > '{cap_dir}/attempt2.prompt'
              git rm -q evil.txt
              touch src/LINTFAIL
              git add src/LINTFAIL
              git commit -q -m 'fix scope, break lint'
            else
              touch '{m1}'
              cat > '{cap_dir}/attempt1.prompt'
              mkdir -p src
              echo ok > src/ok.txt
              echo evil > evil.txt
              git add src/ok.txt evil.txt
              git commit -q -m 'in-scope and out-of-scope files'
            fi
            echo '<promise>COMPLETE</promise>'
        """)
        # Fails only while the marker file is committed, so attempt 2
        # trips the linter and attempt 3 does not.
        lint = (
            "if [ -f src/LINTFAIL ]; then echo 'src/LINTFAIL:1:1: E501 line too long'; exit 1; fi"
        )

        manifest = make_manifest([component(COMP)])
        result = run_factory(
            manifest,
            factory_config(
                max_retries=2,
                verify_config=VerifyConfig(
                    test_command="true",
                    typecheck_command="true",
                    lint_command=lint,
                    check_diff_scope=True,
                    check_bad_patterns=False,
                    subprocess_timeout=10.0,
                ),
            ),
            base_config(root, engineer),
            PlainUI(no_color=True),
            root,
        )

        assert result.exit_code == 0
        assert result.completed == [COMP]
        comp = manifest.get_component(COMP)
        assert comp is not None
        assert comp.retries == 2

        # Attempt 2 saw the scope failure, as before R10.2.
        attempt2 = (cap_dir / "attempt2.prompt").read_text()
        assert "diff_scope: FAIL" in attempt2
        assert "evil.txt" in attempt2

        # Attempt 3 sees the linter failure it must actually fix, and
        # the fixed scope violation is counted, not re-rendered.
        attempt3 = (cap_dir / "attempt3.prompt").read_text()
        assert "## Current failures (measured in attempt 2, verification)" in attempt3
        assert "linter: FAIL" in attempt3
        assert "diff_scope" not in attempt3
        assert "evil.txt" not in attempt3
        assert "## Not re-measured" not in attempt3
        # The scope violation was caught by the in-loop guard, which
        # ranks as engineer; Phase 1 running in attempt 2 proves the
        # engineer loop finished, so it is safely retired.
        assert (
            "1 earlier finding(s) from engineer passed or were "
            "re-measured in attempt 2 and are omitted." in attempt3
        )
        assert "Fix ALL issues listed above" not in attempt3
