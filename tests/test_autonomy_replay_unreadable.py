"""`ks autonomy replay` names the cause when the run history cannot be read (#352).

Split out of tests/test_cli.py, which the file-length ratchet holds at 800
lines: this class shares no helper with the rest of that file.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from kstrl.cli import cli


class TestAutonomyReplayNamesAnUnreadableFile:
    """#352: exit 2 with a cause, rather than exit 2 saying the history is short.

    ``load_runs`` swallowed the read error and returned no runs, so an
    encoding or permission problem printed "VERDICT: INSUFFICIENT DATA"
    and exited 2. The code is unchanged, because nothing was replayed
    either way; what is new is the line above it.
    """

    def test_an_undecodable_file_reports_the_cause(self, tmp_path: Path) -> None:
        path = tmp_path / "experiments.tsv"
        path.write_bytes(b"run_id\ttimestamp\nrun-1\xff\t2026-01-01\n")

        result = CliRunner().invoke(
            cli,
            ["autonomy", "replay", "--experiments", str(path), "--no-color"],
        )

        assert result.exit_code == 2
        assert "could not read the recorded run history" in result.output
        assert "INSUFFICIENT DATA" not in result.output

    def test_a_field_over_the_csv_field_limit_reports_the_cause(self, tmp_path: Path) -> None:
        """#352 round 2, F1: ``_csv.Error`` is neither an ``OSError`` nor
        a ``ValueError``, so it escaped this handler as a traceback.

        ``experiment_rows`` refuses on the ``ValueError`` path now, which
        is the path this handler already takes.
        """
        from kstrl.evolution import EXPERIMENTS_HEADER

        width = len(EXPERIMENTS_HEADER.split("\t"))
        fields = ["run-1", "2026-01-01T00:00:00", "x" * 200_000] + ["0"] * (width - 3)
        path = tmp_path / "experiments.tsv"
        path.write_text(EXPERIMENTS_HEADER + "\n" + "\t".join(fields) + "\n", encoding="utf-8")

        result = CliRunner().invoke(
            cli,
            ["autonomy", "replay", "--experiments", str(path), "--no-color"],
        )

        assert result.exit_code == 2
        assert "could not read the recorded run history" in result.output
        assert "field larger" in result.output
        assert "INSUFFICIENT DATA" not in result.output
