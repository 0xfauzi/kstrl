"""R10.6 (#227): the contract the dogfood workflow keeps.

Parsed, not grepped. A substring assertion passes on a file whose YAML is
broken, which is the guard-goes-blind shape this repository has eleven logged
instances of. Every assertion here reads the PARSED document, and the ones
about shell behaviour EXECUTE the step's own script under ``bash -e`` rather
than reasoning about what it would do.

Split out of ``tests/test_sense_dampener.py`` in review round 1, when that file
crossed 700 lines and this half doubled: the arithmetic tests need no YAML and
these need no dampener arithmetic.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kstrl import dampener_report

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/sense-dampener.yml"


def _workflow() -> dict[str, Any]:
    import yaml

    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _steps() -> list[dict[str, Any]]:
    jobs = _workflow()["jobs"]
    return [step for job in jobs.values() for step in job["steps"]]


def _runs() -> list[str]:
    return [str(step["run"]) for step in _steps() if "run" in step]


def test_the_workflow_triggers_on_pull_requests_only() -> None:
    document = _workflow()
    # YAML 1.1 reads a bare `on` as the boolean true, so the key is True and
    # not "on". Read both rather than pretend one of them.
    triggers = document.get("on", document.get(True))

    assert list(triggers) == ["pull_request"]
    assert triggers["pull_request"]["types"] == ["opened", "synchronize", "reopened"]
    # pull_request_target would run the pull request's own test suite with a
    # write token. It is refused outright, not merely absent by accident.
    assert "pull_request_target" not in triggers


def test_the_workflow_asks_for_least_privilege() -> None:
    assert _workflow()["permissions"] == {"contents": "read", "pull-requests": "write"}


def test_the_workflow_runs_once_per_pull_request() -> None:
    """The job runs the whole test suite a second time on every push. One run
    per pull request, superseded ones cancelled, is what keeps that bounded."""
    concurrency = _workflow()["concurrency"]

    assert concurrency["cancel-in-progress"] is True
    assert "pull_request.number" in concurrency["group"]


#: Checkout, uv install and `uv sync` measured at 9 seconds on run
#: 34130546828 with a warm uv cache. Ten minutes is room, not an estimate.
INSTALL_HEADROOM_SECONDS = 600

VERIFY_PATH = Path(__file__).resolve().parents[1] / "kstrl/verify.py"

#: Source in which the counter below MUST find exactly one spender. Without it
#: a counter that stopped matching would report 0, the arithmetic would demand
#: nothing, and the cap would be pinned against a multiplication by zero.
_SPENDER_CONTROL = "check_test_suite(path, cmd, config.subprocess_timeout, tool)\n"


def _timeout_spenders(source: str) -> int:
    """How many subprocesses one sense run can each be given the FULL budget.

    Counted out of ``kstrl/verify.py`` rather than written down, because the
    number written down was wrong: round 1's comment said three gates may each
    spend it and set a cap covering one. Today it is five - the three gates and
    the two halves of the dead-code phase - and a sixth arriving has to move
    the cap or fail here.
    """
    found = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if isinstance(argument, ast.Attribute) and argument.attr == "subprocess_timeout":
                found += 1
    return found


def test_every_job_is_bounded_above_the_worst_case_it_can_spend() -> None:
    """The cap has to sit above what one run can spend, with room to install.

    ``timeout-minutes: 20`` against ``KSTRL_TIMEOUT_VERIFY: 1800`` was 1200
    seconds of job against 1800 seconds of subprocess: GitHub cancels the job
    first, and a cancelled job produces no sense.md, no sense.err, no comment
    and no step summary. The measured=False mechanism this feature is built
    around would have been unreachable in the only place the workflow runs it.

    Round 1 raised it to 40 and pinned it against ONE gate's budget, which is
    the same defect one multiplication out: the comment above it said three
    gates may each spend the budget, that is 90 minutes, and 40 was green.
    So the multiplier is COUNTED here, and it is five rather than three.
    """
    assert _timeout_spenders(_SPENDER_CONTROL) == 1, (
        "the spender counter matched nothing in its own control, so the "
        "arithmetic below is a multiplication by whatever it returns"
    )
    spenders = _timeout_spenders(VERIFY_PATH.read_text(encoding="utf-8"))
    assert spenders >= 3, spenders

    verify_timeout = float(_sense_step()["env"]["KSTRL_TIMEOUT_VERIFY"])
    for job in _workflow()["jobs"].values():
        assert isinstance(job["timeout-minutes"], int)
        assert (
            job["timeout-minutes"] * 60 >= spenders * verify_timeout + INSTALL_HEADROOM_SECONDS
        ), (
            f"{spenders} subprocesses can each spend {verify_timeout}s, so the job "
            f"needs {(spenders * verify_timeout + INSTALL_HEADROOM_SECONDS) / 60} "
            f"minutes and has {job['timeout-minutes']}"
        )


def test_the_checkout_is_deep_enough_to_reach_the_base() -> None:
    """`ks sense` asks git for the diff STRICTLY and exits 2 when it cannot get
    one, so a shallow clone makes this job fail on every pull request."""
    checkouts = [s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout@")]

    assert len(checkouts) == 1
    assert checkouts[0]["with"]["fetch-depth"] == 0
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_the_workflow_finds_its_comment_by_the_marker_constant() -> None:
    """Compared against the CONSTANT, so moving the marker in one place and
    not the other is a red test rather than a second comment on every PR."""
    assert any(dampener_report.MARKDOWN_MARKER in run for run in _runs())


def test_the_comment_lookup_picks_one_id_across_every_page() -> None:
    """``gh api --paginate`` applies ``--jq`` once PER PAGE, so a ``first``
    inside the filter is the first match on that page rather than the first
    overall.

    Measured against gh 2.73.0 on a 3-comment pull request forced to
    ``per_page=1``: ``[.[] | select(...)] | first | .id`` printed three ids on
    three lines. Past the 30-comment default page size that hands ``gh api
    --method PATCH .../issues/comments/$id`` a multi-line id, so the update
    fails and the dampener starts posting a second comment on every push.
    """
    lookup = [run for run in _runs() if "--paginate" in run]

    assert len(lookup) == 1
    assert "| first |" not in lookup[0]
    # The filter emits every match; exactly one shell stage picks one of them.
    assert "head -n 1" in lookup[0]


def test_the_workflow_is_advisory() -> None:
    """The contract in one line: no step asks the dampener to fail the job.

    Graduating to blocking is adding this flag, deliberately, in its own
    change. It must not arrive by accident.

    Over the whole of every ``run``, not only the sense invocation, so a second
    ks sense call somewhere else in the file is caught too. The cost is that no
    step script may so much as SPELL the flag; the failure step below names the
    graduation in prose instead, and the explanation of what the flag does
    lives in YAML comments, which are not part of any ``run``.
    """
    assert not any("--fail-on-regression" in run for run in _runs())


def test_the_workflow_pins_the_verify_timeout_and_the_compare_flags() -> None:
    """The literal 1800, and the two flags the report shape depends on.

    A baseline and a comparison measured at different verify timeouts are not a
    comparison: this repository's suite times out at the default 300s, and a
    timed-out check contributes no signatures at all. What this does NOT check
    is that the committed baseline was written at this timeout - that needs the
    digest, and it is
    ``tests/test_sense_committed_baseline.py::test_the_committed_baseline_matches_this_repository``.
    The old name for this test claimed the second thing and asserted the first.
    """
    sense_steps = [s for s in _steps() if "uv run ks sense" in str(s.get("run", ""))]

    assert len(sense_steps) == 1
    assert sense_steps[0]["env"]["KSTRL_TIMEOUT_VERIFY"] == "1800"
    assert "--compare-baseline" in sense_steps[0]["run"]
    assert "--format markdown" in sense_steps[0]["run"]


def test_the_comment_step_is_guarded_for_forks() -> None:
    """A fork pull request gets a read-only token whatever `permissions:`
    says. The guard is what keeps that a skipped step rather than a red job,
    and the step summary is what its author reads instead."""
    comment_steps = [s for s in _steps() if "issues/comments" in str(s.get("run", ""))]
    summary_steps = [s for s in _steps() if "GITHUB_STEP_SUMMARY" in str(s.get("run", ""))]

    assert len(comment_steps) == 1
    assert "head.repo.full_name == github.repository" in comment_steps[0]["if"]
    assert summary_steps and all("if" not in step for step in summary_steps)


def test_the_job_fails_on_any_nonzero_sense_exit() -> None:
    """The documented graduation to blocking has to actually block.

    ``docs/dampener.md`` step 2 says adding ``--fail-on-regression`` is the
    whole change. With the failing step keyed on ``rc == '2'`` it was not: the
    flag makes ks sense exit 1, the step records rc=1, and the job stays green.
    """
    failing = [s for s in _steps() if "steps.sense.outputs.rc" in str(s.get("if", ""))]

    assert len(failing) == 1
    assert failing[0]["if"] == "steps.sense.outputs.rc != '0'"
    assert "exit 1" in failing[0]["run"]
    assert "cat sense.err" in failing[0]["run"]


# --- what the step scripts DO, run rather than reasoned about -------------


def _bash(script: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run a script the way the Actions default shell does: ``bash -e``.

    ``-e`` and nothing else, because that is what GitHub passes
    (``bash -e {0}``) and the absence of ``pipefail`` is exactly what two of
    these tests are about.
    """
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def _sense_step() -> dict[str, Any]:
    steps = [s for s in _steps() if "uv run ks sense" in str(s.get("run", ""))]
    assert len(steps) == 1
    return steps[0]


def _comment_step() -> dict[str, Any]:
    steps = [s for s in _steps() if "issues/comments" in str(s.get("run", ""))]
    assert len(steps) == 1
    return steps[0]


BASELINE_IN_TREE = "scripts/kstrl/sense-baseline.json"


def _clone_with_origin(tmp_path: Path, baseline_on_base: str | None) -> Path:
    """A checkout whose ``origin/main`` is a real remote-tracking ref.

    What actions/checkout leaves behind at ``fetch-depth: 0``, reduced to the
    part these tests read: a bare origin carrying the base branch, and a clone
    detached from it the way a pull_request merge ref is. ``baseline_on_base``
    is the file the BASE ref carries, or None for a base branch that has none.
    """
    from tests.spine_utils import git as run_git

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    run_git("init", "-q", "-b", "main", cwd=seed)
    run_git("config", "user.email", "kstrl@example.com", cwd=seed)
    run_git("config", "user.name", "kstrl", cwd=seed)
    if baseline_on_base is not None:
        (seed / "scripts" / "kstrl").mkdir(parents=True)
        (seed / BASELINE_IN_TREE).write_text(baseline_on_base, encoding="utf-8")
        run_git("add", "-A", cwd=seed)
    run_git("commit", "-q", "--allow-empty", "-m", "base", cwd=seed)
    run_git("clone", "-q", "--bare", str(seed), str(origin), cwd=tmp_path)

    clone = tmp_path / "checkout"
    run_git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    run_git("config", "user.email", "kstrl@example.com", cwd=clone)
    run_git("config", "user.name", "kstrl", cwd=clone)
    return clone


def _run_sense_step(clone: Path, tmp_path: Path, stand_in: str) -> tuple[str, str, Path]:
    """Execute the real sense step with `uv run ks sense` replaced.

    Returns the recorded ``rc=`` line, sense.md, and the clone. The stand-in is
    a shell command, so a test decides what the sensor "did" without paying for
    one; what is under test is the shell around it.
    """
    script = str(_sense_step()["run"]).replace("uv run ks sense", stand_in)
    output = tmp_path / "gh-output"
    output.write_text("", encoding="utf-8")

    completed = _bash(script, clone, GITHUB_OUTPUT=str(output), BASE_REF="main")

    assert completed.returncode == 0, completed.stderr
    report = clone / "sense.md"
    return (
        output.read_text(encoding="utf-8"),
        report.read_text(encoding="utf-8") if report.exists() else "",
        clone,
    )


def test_the_sense_step_records_a_failure_it_cannot_itself_report(tmp_path: Path) -> None:
    """Why a separate failing step is needed at all, measured on the real script.

    ``set +e`` then ``set -e`` means the step's own status is 0 whatever ks
    sense did. So the exit code only reaches the job through GITHUB_OUTPUT, and
    a step that keys on the wrong value there is a silently green dampener.
    """
    clone = _clone_with_origin(tmp_path, baseline_on_base='{"schema_version": 1}\n')

    recorded, _, _ = _run_sense_step(clone, tmp_path, "false")

    assert "rc=1" in recorded


def test_the_baseline_comes_from_the_base_ref_and_not_from_the_branch(
    tmp_path: Path,
) -> None:
    """A branch cannot supply the yardstick it is judged by.

    Round 2 of review on #357: `--compare-baseline` was passed bare, so the
    path resolved inside the checkout - which on a pull_request event is the
    merge ref, carrying the pull request's own copy. A branch that rewrote its
    baseline and committed it was compared against its own signatures.

    So the branch here does exactly that, with content that could not be
    mistaken for the base's, and the file the step hands the sensor has to be
    the BASE's bytes. The stand-in copies its `--compare-baseline` argument to
    a known name, which is how the test reads what the sensor was given.
    """
    clone = _clone_with_origin(tmp_path, baseline_on_base='{"from": "the base ref"}\n')
    (clone / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    (clone / BASELINE_IN_TREE).write_text('{"from": "the branch"}\n', encoding="utf-8")
    from tests.spine_utils import git as run_git

    run_git("add", "-A", cwd=clone)
    run_git("commit", "-q", "-m", "rewrite my own baseline", cwd=clone)

    stand_in = "sh -c 'cp \"$2\" handed-to-the-sensor.json' --"
    recorded, report, _ = _run_sense_step(clone, tmp_path, stand_in)

    assert "rc=0" in recorded
    handed = (clone / "handed-to-the-sensor.json").read_text(encoding="utf-8")
    assert handed == '{"from": "the base ref"}\n'
    assert report == ""


def test_the_report_names_the_commit_the_baseline_came_from(tmp_path: Path) -> None:
    """The comparison prints the path it was given, so the file is named after
    the base commit. Without it the report says which FILE supplied the
    baseline and never which ref, and the two are the whole question here."""
    from tests.spine_utils import git as run_git

    clone = _clone_with_origin(tmp_path, baseline_on_base="{}\n")
    base_sha = run_git("rev-parse", "origin/main", cwd=clone).strip()

    stand_in = "sh -c 'echo \"$2\"' --"
    _, _, _ = _run_sense_step(clone, tmp_path, stand_in)

    assert (clone / "sense.md").read_text(encoding="utf-8").strip() == (
        f"baseline-from-{base_sha}.json"
    )


def test_a_base_ref_with_no_baseline_reports_that_and_stays_green(
    tmp_path: Path,
) -> None:
    """The bootstrap case, which is this pull request's own.

    A base branch with no baseline gives nothing to compare against. That is
    not a regression and not a failure, so the step records rc=0 and writes a
    report saying so - marker first, because the comment step finds its own
    earlier comment by that line and would otherwise post a second one.
    """
    clone = _clone_with_origin(tmp_path, baseline_on_base=None)

    recorded, report, _ = _run_sense_step(clone, tmp_path, "false")

    assert "rc=0" in recorded
    assert report.splitlines()[0] == dampener_report.MARKDOWN_MARKER
    assert "No baseline on `main`" in report
    assert not list(clone.glob("baseline-from-*.json"))


def test_the_step_refuses_a_base_ref_it_cannot_resolve(tmp_path: Path) -> None:
    """The control for the three above: without it, a step whose git commands
    all failed would still write a report and record rc=0, and every pull
    request would get "no baseline on the base ref" forever."""
    clone = _clone_with_origin(tmp_path, baseline_on_base="{}\n")
    script = str(_sense_step()["run"]).replace("uv run ks sense", "false")
    output = tmp_path / "gh-output"
    output.write_text("", encoding="utf-8")

    completed = _bash(script, clone, GITHUB_OUTPUT=str(output), BASE_REF="no-such-branch")

    assert completed.returncode == 1
    assert "cannot resolve origin/no-such-branch" in completed.stderr
    assert not (clone / "sense.md").exists()


def test_the_comment_step_posts_nothing_when_there_is_no_report(tmp_path: Path) -> None:
    """``gh api -F body=@sense.md`` on an empty file sends {"body": ""}, which
    GitHub rejects with 422. The job would go red on the comment step while the
    real diagnostic sat in sense.err."""
    (tmp_path / "sense.md").write_text("", encoding="utf-8")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gh").write_text("#!/bin/sh\necho CALLED\nexit 0\n", encoding="utf-8")
    (stub / "gh").chmod(0o755)

    completed = _bash(
        str(_comment_step()["run"]),
        tmp_path,
        PATH=f"{stub}:{os.environ['PATH']}",
        REPO="o/r",
        NUMBER="1",
    )

    assert completed.returncode == 0
    assert "CALLED" not in completed.stdout
    assert "no report to post" in completed.stderr


def test_a_failed_comment_lookup_refuses_rather_than_posting_a_duplicate(
    tmp_path: Path,
) -> None:
    """#260's rule: an unreadable lookup is a refusal, not an empty read.

    Reproduced first in the shape the earlier script had. Under ``bash -e``
    with no pipefail, ``id="$(false | head -n 1)"`` yields rc 0 and an empty
    id, which falls into the POST branch and adds a second comment on every
    push.
    """
    assert _bash('id="$(false | head -n 1)"; echo "rc=$? id=[$id]"', tmp_path).stdout.strip() == (
        "rc=0 id=[]"
    )

    (tmp_path / "sense.md").write_text("body\n", encoding="utf-8")
    stub = tmp_path / "bin"
    stub.mkdir()
    # Fails on the lookup, would succeed on a POST. So a script that ignored
    # the lookup's status would exit 0 having commented.
    (stub / "gh").write_text(
        '#!/bin/sh\ncase "$*" in *--paginate*) exit 1 ;; *) echo POSTED ;; esac\n',
        encoding="utf-8",
    )
    (stub / "gh").chmod(0o755)

    completed = _bash(
        str(_comment_step()["run"]),
        tmp_path,
        PATH=f"{stub}:{os.environ['PATH']}",
        REPO="o/r",
        NUMBER="1",
    )

    assert completed.returncode == 1
    assert "POSTED" not in completed.stdout
    assert "refusing to post" in completed.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="the Actions shell is bash")
def test_the_comment_step_updates_the_id_the_lookup_found(tmp_path: Path) -> None:
    """The control for the two tests above: with the lookup working, the script
    reaches PATCH and does not POST. Without it, a script that refused
    everything would pass both of them."""
    (tmp_path / "sense.md").write_text("body\n", encoding="utf-8")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gh").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--paginate*) echo 5561736414 ;;\n"
        '  *PATCH*) echo "PATCHED $*" ;;\n'
        '  *) echo "POSTED $*" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    (stub / "gh").chmod(0o755)

    completed = _bash(
        str(_comment_step()["run"]),
        tmp_path,
        PATH=f"{stub}:{os.environ['PATH']}",
        REPO="o/r",
        NUMBER="1",
    )

    assert completed.returncode == 0
    assert "PATCHED" in completed.stdout
    assert "5561736414" in completed.stdout
    assert "POSTED" not in completed.stdout


def test_the_comment_step_sets_pipefail() -> None:
    """Belt over braces, and the reason is written down rather than assumed:
    the lookup no longer pipes, so pipefail protects any pipeline a later edit
    adds to this step."""
    assert "set -o pipefail" in str(_comment_step()["run"])
