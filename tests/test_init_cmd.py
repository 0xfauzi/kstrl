"""Direct tests for ``run_init`` - the scaffold `ks init` writes.

Nothing exercised ``run_init`` itself before this file: the wizard tests
cover ``plan_scaffold`` and the agent-settings write, and the TUI screen
test patches ``run_init`` out. Both issues fixed here (#201 .gitignore,
#256 next steps) are properties of what init WRITES and PRINTS, so they
are tested against the real function with a real UI.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from kstrl import init_cmd
from kstrl.appendio import append_records
from kstrl.cli import cli
from kstrl.init_cmd import (
    DEFAULT_MEMORY,
    GITIGNORE_BLOCK_MARKER,
    NEXT_STEPS,
    run_init,
)
from kstrl.init_wizard import plan_scaffold
from kstrl.ui.plain import PlainUI
from kstrl.ui.rich_ui import RichUI


def run_init_capturing(root: Path, *, upgrade_prompts: bool = False) -> tuple[int, str]:
    """Run init against ``root``, returning (exit code, printed output)."""
    buffer = io.StringIO()
    code = run_init(root, PlainUI(no_color=True, file=buffer), upgrade_prompts=upgrade_prompts)
    return code, buffer.getvalue()


class TestGitignoreScaffold:
    def test_the_block_holds_kstrl_state_and_no_build_output(self, tmp_path: Path) -> None:
        """#696 slice 6: kstrl names no tool's build output; what a [stack]
        check writes is measured on the base instead."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        rules = [
            line
            for line in (tmp_path / ".gitignore").read_text().splitlines()
            if line and not line.startswith("#")
        ]
        assert rules == [".kstrl/", ".DS_Store"]

    def test_lockfile_is_never_ignored(self, tmp_path: Path) -> None:
        """#201: uv.lock belongs in version control, not in .gitignore."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        run_init_capturing(tmp_path)

        assert "uv.lock" not in (tmp_path / ".gitignore").read_text()

    def test_rerun_does_not_duplicate_the_block(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        run_init_capturing(tmp_path)
        after_first = (tmp_path / ".gitignore").read_text()
        _, output = run_init_capturing(tmp_path)

        assert (tmp_path / ".gitignore").read_text() == after_first
        assert after_first.count(GITIGNORE_BLOCK_MARKER) == 1
        assert "already has the kstrl block" in output

    def test_existing_gitignore_is_appended_to_not_rewritten(self, tmp_path: Path) -> None:
        existing = "# mine\nsecrets.env\ndist/\n"
        (tmp_path / ".gitignore").write_text(existing)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

        _, output = run_init_capturing(tmp_path)

        content = (tmp_path / ".gitignore").read_text()
        assert content.startswith(existing)
        assert GITIGNORE_BLOCK_MARKER in content
        assert ".kstrl/" in content
        assert "Appended the kstrl block" in output

    def test_append_separates_from_a_file_with_no_trailing_newline(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.env")

        run_init_capturing(tmp_path)

        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert lines[0] == "secrets.env"
        assert GITIGNORE_BLOCK_MARKER in lines

    def test_empty_gitignore_gains_the_block_without_leading_blanks(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("")

        run_init_capturing(tmp_path)

        assert (tmp_path / ".gitignore").read_text().startswith(GITIGNORE_BLOCK_MARKER)

    def test_a_rule_written_between_the_read_and_the_write_is_not_glued_to(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The read-then-write window ``appendio`` closes (#352).

        ``_gitignore_state`` reads the file and ``_ensure_gitignore``
        appends to it, and until the append was routed the separator was
        decided from that earlier read. The two disagree whenever the
        file changes in between, which is a user saving in an editor or
        a second tool appending a rule.

        The window is simulated at the seam rather than raced, because a
        race that reproduces one run in a thousand is not a test: the
        state read reports the file as empty, and the file on disk holds
        an unterminated rule by the time of the write. Before the
        routing that produced ``secrets.env# kstrl`` on one line, so the
        user's rule and the block header were both wrong. Now the probe
        happens on the handle being written through, so the rule keeps
        its own line.
        """
        path = tmp_path / ".gitignore"
        path.write_text("secrets.env", encoding="utf-8")
        real_state = init_cmd._gitignore_state

        def stale_read(root: Path) -> tuple[str, str | None]:
            action, _ = real_state(root)
            return action, ""

        monkeypatch.setattr(init_cmd, "_gitignore_state", stale_read)
        run_init_capturing(tmp_path)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "secrets.env"
        assert GITIGNORE_BLOCK_MARKER in lines

    def test_user_edits_below_the_block_survive_a_rerun(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)
        path = tmp_path / ".gitignore"
        path.write_text(path.read_text() + "\n# added later\nmy-scratch/\n")
        before = path.read_text()

        run_init_capturing(tmp_path)

        assert path.read_text() == before


class TestNextSteps:
    def test_leads_with_the_spec_workflow(self, tmp_path: Path) -> None:
        """#256: the two commands implementing the README headline."""
        _, output = run_init_capturing(tmp_path)

        assert "ks decompose --spec" in output
        assert "ks factory --spec" in output
        spec_at = output.index("ks decompose --spec")
        assert spec_at < output.index("ks run [iterations]")

    def test_single_component_path_is_labelled_as_such(self, tmp_path: Path) -> None:
        _, output = run_init_capturing(tmp_path)

        assert "ks run [iterations]" in output
        assert "no PR" in output

    def test_the_block_fits_an_eighty_column_terminal(self) -> None:
        """Rich word-wraps to the console width, so a longer line
        arrives split across two rows with its comment orphaned.
        80 columns is the narrowest terminal kstrl designs for."""
        longest = max(NEXT_STEPS.splitlines(), key=len)

        assert len(longest) <= 80, longest

    def test_names_the_free_measurement(self, tmp_path: Path) -> None:
        _, output = run_init_capturing(tmp_path)

        assert "ks check" in output
        assert "ks understand [iterations]" in output
        assert "ks feature [iterations]" in output


class TestRichRendering:
    """The default UI is Rich, so the block has to survive Rich.

    tests/test_rich_ui.py owns the invariant for every method; this is
    the one command whose transcript regressed on it (#256 review).
    """

    def test_the_block_reaches_the_default_ui_intact(self, tmp_path: Path) -> None:
        buffer = io.StringIO()
        run_init(tmp_path, RichUI(no_color=True, file=buffer))

        assert "ks run [iterations]" in buffer.getvalue()


class TestUnreadableGitignore:
    def test_a_non_utf8_gitignore_is_left_alone(self, tmp_path: Path) -> None:
        """UnicodeDecodeError is a ValueError, not an OSError, so an
        unguarded read_text killed `ks init` with a traceback instead of
        an exit code (#201 review)."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_bytes(b"build\xff/\n")

        code, output = run_init_capturing(tmp_path)

        assert code == 0
        assert "could not be read as text" in output
        assert gitignore.read_bytes() == b"build\xff/\n"

    def test_a_gitignore_directory_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").mkdir()

        code, output = run_init_capturing(tmp_path)

        assert code == 0
        assert "could not be read as text" in output
        assert (tmp_path / ".gitignore").is_dir()


class TestScaffoldContract:
    def test_plan_scaffold_lists_exactly_what_run_init_writes(self, tmp_path: Path) -> None:
        """The wizard preview and the write cannot drift apart silently."""
        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        written = {p for p in tmp_path.rglob("*") if p.is_file()}
        assert written == {entry.path for entry in plan_scaffold(tmp_path)}

    def test_ks_init_scaffolds_golden_patterns(self, tmp_path: Path) -> None:
        """R10.8. The file is scaffolded once and then belongs to the
        operator: a second init must not revert what they wrote into it."""
        golden = tmp_path / "scripts" / "kstrl" / "golden-patterns.md"

        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        assert golden.exists()
        assert "## Follow these" in golden.read_text(encoding="utf-8")

        edited = "# Golden patterns\n\n## Follow these\n\n- atomic writes: kstrl/atomicio.py\n"
        golden.write_text(edited, encoding="utf-8")
        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        assert golden.read_text(encoding="utf-8") == edited

    def test_ks_init_scaffolds_memory(self, tmp_path: Path) -> None:
        """R10.9, the same contract as golden patterns: scaffolded once,
        then the operator's. A second init must not revert what they
        wrote, because `/memory` comments (R10.10) accumulate there."""
        memory = tmp_path / "scripts" / "kstrl" / "memory.md"

        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        assert memory.exists()
        body = memory.read_text(encoding="utf-8")
        assert body == DEFAULT_MEMORY
        # `## Guidance` is the LAST section, which is what makes R10.10's
        # append land in the right place. Pinned here rather than left to
        # the constant, because a section added after it is invisible.
        assert body.rstrip("\n").endswith("## Guidance")

        edited = DEFAULT_MEMORY + "- never touch migrations\n"
        memory.write_text(edited, encoding="utf-8")
        code, _ = run_init_capturing(tmp_path)

        assert code == 0
        assert memory.read_text(encoding="utf-8") == edited

    def test_plan_stops_calling_gitignore_an_append_once_init_ran(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("secrets.env\n")

        run_init_capturing(tmp_path)

        planned = {e.path.name: e for e in plan_scaffold(tmp_path)}
        assert planned[".gitignore"].action == "keep"

    def test_every_command_named_in_next_steps_is_real(self) -> None:
        """The block is the first thing a new user reads; a renamed
        command or flag must not leave it printing something that errors."""
        checked = 0
        for line in NEXT_STEPS.splitlines():
            match = re.search(r"\bks ([a-z]+)(.*)", line)
            if not match:
                continue
            name, rest = match.group(1), match.group(2)
            command = cli.commands.get(name)
            assert command is not None, f"`ks {name}` is not a command"
            known = {opt for param in command.params for opt in param.opts}
            assert set(re.findall(r"--[a-z-]+", rest)) <= known, line
            checked += 1

        assert checked >= 5


def section_of(text: str, line: str) -> str | None:
    """The last ``##`` heading at or above ``line`` in ``text``.

    None when ``line`` is not in the file at all, so a case cannot pass
    by asserting a section for a line that never landed.
    """
    current: str | None = None
    for candidate in text.splitlines():
        if candidate.startswith("## "):
            current = candidate
        if candidate == line:
            return current
    return None


class TestTheAppendLandsUnderGuidance:
    """R10.9 round 1, nit 9. ``## Guidance`` last, against a REAL file.

    ``TestScaffoldContract.test_ks_init_scaffolds_memory`` pins
    ``DEFAULT_MEMORY.rstrip("\n").endswith("## Guidance")``, which is a
    pin on what kstrl SHIPS. Nothing read the headings of the file on
    the operator's disk: ``grep -rn "Guidance" kstrl/`` returns only
    ``init_cmd.py``. So the invariant #231's tail append depends on was
    stated in a comment, in the runbook and in the README, and held by
    nothing that runs.

    These two cases run the real append against a real ``ks init``
    scaffold. The first is the invariant. The second pins what
    ``append_records`` itself does: a BLIND TAIL APPEND takes every
    section added after ``## Guidance`` from then on, and no gate in
    this repository goes red on it. #231 closed the blind spot for the
    writer it added (``/memory`` reads the headings and inserts into the
    ``## Guidance`` section, held by ``tests/test_steering.py`` cases 3
    and 4 and by its plant 6); ``append_records`` below is unchanged and
    still has the property this class names.
    """

    def test_a_real_append_lands_under_guidance(self, tmp_path: Path) -> None:
        code, _ = run_init_capturing(tmp_path)
        memory = tmp_path / "scripts" / "kstrl" / "memory.md"
        assert code == 0

        repaired = append_records(memory, "- never touch migrations\n", repair="", lock=True)

        body = memory.read_text(encoding="utf-8")
        assert repaired is False, "the scaffold ends in a newline, so no repair pad is needed"
        assert section_of(body, "- never touch migrations") == "## Guidance"

    def test_a_section_after_guidance_takes_the_appends_and_nothing_notices(
        self, tmp_path: Path
    ) -> None:
        """What a blind tail append does, stated rather than implied.

        Not an xfail: nothing here is expected to be fixed by a later
        widening of a walk. It is a property of ``append_records``
        appending to a file whose last heading the operator controls.
        #231's writer does not use ``append_records``; it reads the
        headings before it writes, held by ``tests/test_steering.py``.
        """
        code, _ = run_init_capturing(tmp_path)
        memory = tmp_path / "scripts" / "kstrl" / "memory.md"
        assert code == 0
        memory.write_text(
            memory.read_text(encoding="utf-8") + "\n## Notes\n\n- my own scratch\n",
            encoding="utf-8",
        )

        append_records(memory, "- never touch migrations\n", repair="", lock=True)

        body = memory.read_text(encoding="utf-8")
        assert section_of(body, "- never touch migrations") == "## Notes"
        assert "## Guidance" in body, "the section is still there, it just stopped being last"


class TestExitCodes:
    def test_missing_directory_returns_2(self, tmp_path: Path) -> None:
        code, output = run_init_capturing(tmp_path / "nope")

        assert code == 2
        assert "Directory not found" in output

    def test_unparseable_prd_returns_1(self, tmp_path: Path) -> None:
        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)
        (kstrl_dir / "prd.json").write_text("{not json")

        code, output = run_init_capturing(tmp_path)

        assert code == 1
        assert "Invalid JSON" in output
