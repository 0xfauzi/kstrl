"""The records a run writes carry the kstrl version that wrote them (#451).

Before #451 no run record said which kstrl wrote it, so a field missing
because the record is older and a field missing because of a defect
looked the same. These tests drive the real ``run_factory`` (only the
component worker is stubbed) and read each record back from disk, then
drive the real ``ks status`` in a subprocess over stamped and unstamped
records.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl import __version__
from kstrl.evolution import (
    EXPERIMENTS_HEADER,
    SPEC_ISSUES_EVENT,
    EvolutionConfig,
    EvolutionJournal,
    experiment_rows,
)
from kstrl.launch_record import read_launch_record
from kstrl.manifest import Manifest
from kstrl.version import UNSTAMPED, kstrl_version, version_of_source
from tests.helpers.gitrepo import git_in, set_identity
from tests.test_event_stream import _events_file, _run_stub_factory


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _manifest_file(root: Path) -> Path:
    return root / "scripts" / "kstrl" / "manifest.json"


def _status(root: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "kstrl", "status", "--no-tui", "--ui", "plain", "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report = proc.stdout + proc.stderr
    assert proc.returncode == 0, report
    return report


#: A stamp no kstrl this test runs under can produce, so a report that
#: shows it read it from the record rather than from its own process.
OTHER_KSTRL = "0.0.1+g0123456789ab"


def _restamp(root: Path, stamp: str | None) -> None:
    """Rewrite the stamp on the run's events and manifest.

    ``None`` deletes the key, which is how a kstrl before #451 wrote them.
    """
    events_file = _events_file(root)
    lines = _lines(events_file)
    for line in lines:
        del line["kstrl_version"]
        if stamp is not None:
            line["kstrl_version"] = stamp
    events_file.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    manifest = json.loads(_manifest_file(root).read_text(encoding="utf-8"))
    del manifest["kstrlVersion"]
    if stamp is not None:
        manifest["kstrlVersion"] = stamp
    _manifest_file(root).write_text(json.dumps(manifest), encoding="utf-8")


def _strip_stamps(root: Path) -> None:
    """Rewrite the run's events and manifest as a kstrl before #451 wrote them."""
    _restamp(root, None)


def _empty_run_dir(root: Path) -> None:
    for path in _events_file(root).parent.rglob("*"):
        if path.is_file():
            path.unlink()


class TestAStubRunStampsEveryRecord:
    def test_every_event_line_carries_the_stamp(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        lines = _lines(_events_file(root))
        assert [line["event"] for line in lines[:2]] == ["factory_started", "run_plan"]
        assert {line["kstrl_version"] for line in lines} == {kstrl_version()}

    def test_the_launch_record_carries_the_stamp(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        launch = json.loads((_events_file(root).parent / "launch.json").read_text("utf-8"))
        assert launch["kstrlVersion"] == kstrl_version()

    def test_the_manifest_run_stamp_carries_the_stamp(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        manifest = json.loads(_manifest_file(root).read_text(encoding="utf-8"))
        assert manifest["runId"] == _events_file(root).parent.name
        assert manifest["kstrlVersion"] == kstrl_version()
        assert Manifest.load(_manifest_file(root)).kstrl_version == kstrl_version()

    def test_every_journal_row_carries_the_stamp(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        rows = _lines(root / ".kstrl" / "evolution.jsonl")
        assert "component_result" in {row["event_type"] for row in rows}
        assert {row["kstrl_version"] for row in rows} == {kstrl_version()}

    def test_the_experiments_row_carries_the_stamp(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        text = (root / ".kstrl" / "experiments.tsv").read_text(encoding="utf-8")
        assert text.splitlines()[0] == EXPERIMENTS_HEADER
        assert experiment_rows(text)[-1]["kstrl_version"] == kstrl_version()


def _repo_with_a_commit(path: Path) -> str:
    """A git repository at ``path`` with one commit; returns its HEAD."""
    path.mkdir(parents=True, exist_ok=True)
    git_in(path, "init", "-q")
    set_identity(path)
    git_in(path, "commit", "-q", "--allow-empty", "-m", "c")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


class TestTheStampHasOneSource:
    def test_a_checkout_stamps_the_package_version_and_its_commit(self, tmp_path: Path) -> None:
        head = _repo_with_a_commit(tmp_path)
        assert version_of_source(tmp_path) == f"{__version__}+g{head[:12]}"

    def test_a_worktree_checkout_stamps_its_commit(self, tmp_path: Path) -> None:
        """A git worktree of kstrl has a .git FILE, not a directory. It is
        still a checkout, so it stamps its commit, not the bare package
        version that would pass for an installed kstrl."""
        head = _repo_with_a_commit(tmp_path / "main")
        git_in(tmp_path / "main", "worktree", "add", "-q", "--detach", str(tmp_path / "wt"))
        assert (tmp_path / "wt" / ".git").is_file()
        assert version_of_source(tmp_path / "wt") == f"{__version__}+g{head[:12]}"

    def test_an_installed_kstrl_never_stamps_the_project_commit(self, tmp_path: Path) -> None:
        """kstrl installed in a project's virtualenv sits inside that
        project's repository. The stamp is the package version alone,
        not the project's HEAD."""
        _repo_with_a_commit(tmp_path)
        site_packages = tmp_path / ".venv" / "lib" / "python3.11" / "site-packages"
        site_packages.mkdir(parents=True)
        assert version_of_source(site_packages) == __version__

    def test_a_checkout_whose_commit_cannot_be_read_says_so(self, tmp_path: Path) -> None:
        """Not the bare package version, which would pass for an installed kstrl."""
        (tmp_path / ".git").mkdir()
        assert version_of_source(tmp_path) == f"{__version__}+commit-unknown"

    def test_the_stamp_ignores_the_repository_kstrl_runs_in(self, tmp_path: Path) -> None:
        """kstrl runs with its working directory inside the operator's
        project. A fresh process started there stamps what this process
        stamps, never the project's HEAD."""
        project_head = _repo_with_a_commit(tmp_path / "project")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from kstrl.version import kstrl_version; print(kstrl_version())",
            ],
            cwd=tmp_path / "project",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == kstrl_version()
        assert project_head[:12] not in proc.stdout


class TestAReaderOfAnUnstampedRecord:
    def test_status_prints_the_stamp_of_the_run_it_shows(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        report = _status(root)
        assert kstrl_version() in report
        assert UNSTAMPED not in report

    def test_status_says_an_unstamped_run_was_written_before_stamping(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        _strip_stamps(root)
        report = _status(root)
        assert UNSTAMPED in report
        assert "comp-a: completed" in report

    def test_status_prints_the_recorded_stamp_not_its_own(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        _restamp(root, OTHER_KSTRL)
        report = _status(root)
        assert OTHER_KSTRL in report
        assert kstrl_version() not in report

    def test_status_with_no_event_stream_reads_the_manifest_stamp(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        run_id = _events_file(root).parent.name
        _strip_stamps(root)
        _empty_run_dir(root)
        report = _status(root)
        assert run_id in report
        assert UNSTAMPED in report

    def test_status_with_no_event_stream_prints_the_manifest_stamp(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        _restamp(root, OTHER_KSTRL)
        _empty_run_dir(root)
        report = _status(root)
        assert OTHER_KSTRL in report

    def test_a_launch_record_written_before_stamping_still_reads(self, tmp_path: Path) -> None:
        manifest_file = _manifest_file(_run_stub_factory(tmp_path, ["comp-a"]))
        manifest = Manifest.load(manifest_file)
        record = _events_file(tmp_path).parent / "launch.json"
        payload = json.loads(record.read_text(encoding="utf-8"))
        del payload["kstrlVersion"]
        record.write_text(json.dumps(payload), encoding="utf-8")
        assert read_launch_record(tmp_path, manifest, manifest_file) is not None

    def test_a_manifest_whose_stamp_is_not_a_string_is_refused(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        data = json.loads(_manifest_file(root).read_text(encoding="utf-8"))
        data["kstrlVersion"] = 3
        _manifest_file(root).write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="kstrlVersion must be a string"):
            Manifest.load(_manifest_file(root))

    def test_an_experiments_file_with_the_older_header_reads_the_new_stamp(
        self, tmp_path: Path
    ) -> None:
        older = EXPERIMENTS_HEADER.rsplit("\t", 1)[0]
        legacy_row = "\t".join(["run-0", "t", "p", *["0"] * 11])
        experiments = tmp_path / ".kstrl" / "experiments.tsv"
        experiments.parent.mkdir(parents=True)
        experiments.write_text(f"{older}\n{legacy_row}\n", encoding="utf-8")
        _run_stub_factory(tmp_path, ["comp-a"])
        rows = experiment_rows(experiments.read_text(encoding="utf-8"))
        assert [row.get("kstrl_version") for row in rows] == [None, kstrl_version()]

    def test_a_pre_r3_1_file_keeps_the_rows_every_later_kstrl_wrote(self, tmp_path: Path) -> None:
        """A file started before R3.1 holds 11-field rows, then 14-field
        rows from R3.1 to #451, then 15-field rows. All three are runs."""
        columns = EXPERIMENTS_HEADER.split("\t")
        rows = [
            "\t".join([rid, *["0"] * (width - 1)]) for rid, width in (("run-0", 11), ("run-1", 14))
        ]
        experiments = tmp_path / ".kstrl" / "experiments.tsv"
        experiments.parent.mkdir(parents=True)
        experiments.write_text("\n".join(["\t".join(columns[:11]), *rows]) + "\n", encoding="utf-8")
        _run_stub_factory(tmp_path, ["comp-a"])
        read = experiment_rows(experiments.read_text(encoding="utf-8"))
        assert [row["run_id"] for row in read[:2]] == ["run-0", "run-1"]
        assert [row.get("kstrl_version") for row in read] == [None, None, kstrl_version()]

    def test_any_row_the_journal_writes_is_stamped(self, tmp_path: Path) -> None:
        """The reason every journal row builder is exempt in the census."""
        journal = EvolutionJournal(EvolutionConfig(journal_path=tmp_path / "evolution.jsonl"))
        journal.append_entries([{"event_type": SPEC_ISSUES_EVENT}])
        assert _lines(tmp_path / "evolution.jsonl") == [
            {"event_type": SPEC_ISSUES_EVENT, "kstrl_version": kstrl_version()}
        ]
