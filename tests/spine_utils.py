"""Shared builders for the real-git spine tier (R4.2).

The spine tier's contract is "unmocked at the boundary under test": repos
are real ``git init`` repositories, origins are real bare repos, the
engineer is a real ``bash -lc`` subprocess (no ``unittest.mock`` anywhere
in a spine test), and ``gh`` is a real executable stub on PATH whose
behavior is driven by ``GH_SPINE_*`` env vars.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from kstrl.config import KstrlConfig
from kstrl.factory import FactoryConfig
from kstrl.manifest import Component, Manifest
from kstrl.verify import VerifyConfig

STUB_PR_URL = "https://github.com/spine/repo/pull/41"
STUB_PR_NUMBER = 41

# One-iteration fake engineer: emits the completion promise and exits.
COMPLETE_LINE = "echo '<promise>COMPLETE</promise>'"


def logging_engineer(log_path: Path) -> str:
    """Fake engineer that records the worktree it ran in, then completes.

    The worktree path's basename is the component id, so the log is a
    mock-free record of which components the scheduler actually ran.
    """
    return f"pwd >> '{log_path}' && {COMPLETE_LINE}"


def ran_components(log_path: Path) -> list[str]:
    """Component ids the logging engineer actually ran, in order."""
    if not log_path.exists():
        return []
    return [
        Path(line).name
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed: {result.stderr}"
    )
    return result.stdout.strip()


def init_ralph_repo(
    root: Path, comp_ids: tuple[str, ...], with_origin: bool = False,
) -> Path | None:
    """Real git repo shaped like a ralph project.

    ``scripts/kstrl/`` is gitignored so worktree provisioning (R0.4) must
    copy prompt and PRD files in. Returns the bare origin path when
    ``with_origin`` is set, else None.
    """
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "spine@test", cwd=root)
    git("config", "user.name", "Spine Test", cwd=root)
    (root / ".gitignore").write_text("scripts/kstrl/\n.kstrl/\n")
    (root / "README.md").write_text("seed\n")
    git("add", ".gitignore", "README.md", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)

    ralph_dir = root / "scripts" / "kstrl"
    (ralph_dir / "prompt.md").parent.mkdir(parents=True, exist_ok=True)
    (ralph_dir / "prompt.md").write_text(
        "Read the PRD at $prd_path and implement one story.\n"
    )
    for comp_id in comp_ids:
        feature_dir = ralph_dir / "feature" / comp_id
        feature_dir.mkdir(parents=True)
        prd: dict[str, object] = {
            "branchName": f"kstrl/factory/{comp_id}",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }
        (feature_dir / "prd.json").write_text(json.dumps(prd))

    if with_origin:
        origin = root.parent / "origin.git"
        git("init", "-q", "--bare", str(origin), cwd=root.parent)
        git("remote", "add", "origin", str(origin), cwd=root)
        git("push", "-q", "-u", "origin", "main", cwd=root)
        return origin
    return None


def component(comp_id: str, dependencies: list[str] | None = None) -> Component:
    return Component(
        id=comp_id, title=comp_id.upper(), description="",
        dependencies=dependencies or [],
        prd_path=f"scripts/kstrl/feature/{comp_id}/prd.json",
        branch_name=f"kstrl/factory/{comp_id}",
    )


def make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="spine",
        base_branch="main", single_pr=False, components=components,
    )


def factory_config(**overrides: object) -> FactoryConfig:
    config = FactoryConfig(
        use_worktrees=True, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        merge_timeout=2.0,
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=10.0,
        ),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def base_config(root: Path, agent_cmd: str = COMPLETE_LINE) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0, agent_cmd=agent_cmd,
        kstrl_branch="", kstrl_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def write_stub_gh(bin_dir: Path) -> Path:
    """Executable ``gh`` stub; behavior driven by GH_SPINE_* env vars.

    GH_SPINE_CREATE / GH_SPINE_MERGE: "ok" (default) or "fail".
    GH_SPINE_VIEW_STATE: "MERGED" (default), "OPEN", or "CLOSED".
    """
    gh = bin_dir / "gh"
    gh.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "auth" ]; then exit 0; fi
        if [ "$1" = "pr" ]; then
          case "$2" in
            create)
              if [ "${{GH_SPINE_CREATE:-ok}}" = "fail" ]; then
                echo "spine stub: pr create failed" >&2; exit 1
              fi
              echo "{STUB_PR_URL}"; exit 0 ;;
            merge)
              if [ "${{GH_SPINE_MERGE:-ok}}" = "fail" ]; then
                echo "spine stub: pr merge failed" >&2; exit 1
              fi
              exit 0 ;;
            view)
              printf '{{"state": "%s"}}\\n' "${{GH_SPINE_VIEW_STATE:-MERGED}}"
              exit 0 ;;
          esac
        fi
        exit 0
    """))
    gh.chmod(0o755)
    return gh
