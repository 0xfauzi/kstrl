"""#286: what the scaffold ledger says about a prompt file on disk.

`ks init` writes ``scripts/kstrl/prompt.md`` through
``_create_if_missing``, which by design never overwrites, and
``run_loop`` prefers that file over ``DEFAULT_PROMPT``. Before this,
``DEFAULT_PROMPT_VERSION`` was read nowhere in ``kstrl/``, so an H3
version bump reached greenfield inits and the missing-file fallback and
no already-initialised project at all.

The mechanism keys on the file's SHA-256 against a ledger of every body
the harness has ever shipped, so it classifies files written long before
the mechanism existed. Two kinds of test live here:

- **Real-ledger tests** use ``SCAFFOLDED_TEMPLATES`` as shipped. They
  cover the current / unrecognised / absent paths and the ledger's own
  invariants.
- **Synthetic-ledger tests** monkeypatch in a two-row template built
  from literal bodies. Producing a genuinely stale file against the real
  ledger would mean shipping a 4KB historical prompt body as a fixture,
  or reading it back out of git history, which a shallow CI checkout
  does not have. The classifier is indifferent to which ledger it reads,
  so the synthetic one exercises the same code path with bodies the test
  can state outright.

The 1.1.1 row was validated during development against a real
installation: the owner's writers-room project carries a prompt.md whose
digest is exactly that row.

What kstrl DOES about a stale file - the `ks init` report, the upgrade
and its guards, and the operator-facing surfaces - is in
tests/test_prompt_upgrade.py, which imports the fixtures below.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from kstrl import init_cmd
from kstrl.init_cmd import (
    DEFAULT_PROMPT,
    DEFAULT_PROMPT_VERSION,
    SCAFFOLDED_TEMPLATES,
    ScaffoldedTemplate,
    classify_scaffold,
    classify_scaffolded_path,
    staleness_notice,
)
from tests.test_init_cmd import run_init_capturing

OLD_BODY = "# old engineer instructions\n"
NEW_BODY = "# new engineer instructions\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SYNTHETIC = ScaffoldedTemplate(
    filename="prompt.md",
    constant_name="DEFAULT_PROMPT",
    body=NEW_BODY,
    history=(
        (_sha256(OLD_BODY), "9.0.0"),
        (_sha256(NEW_BODY), "9.1.0"),
    ),
)


@pytest.fixture
def synthetic_ledger(monkeypatch: pytest.MonkeyPatch) -> ScaffoldedTemplate:
    """Swap the shipped ledger for a two-row one built from literals."""
    monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (SYNTHETIC,))
    return SYNTHETIC


def _prompt_at(root: Path, body: str) -> Path:
    path = root / "scripts" / "kstrl" / "prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class TestClassification:
    def test_stale_file_is_recognised_as_the_body_it_came_from(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        state = classify_scaffolded_path(_prompt_at(tmp_path, OLD_BODY))
        assert state is not None
        assert state.status == "stale"
        assert state.shipped_label == "9.0.0"

    def test_current_file_is_not_stale(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        state = classify_scaffolded_path(_prompt_at(tmp_path, NEW_BODY))
        assert state is not None
        assert state.status == "current"

    def test_current_file_is_not_stale_against_the_real_ledger(self, tmp_path: Path) -> None:
        """The bytes `ks init` writes today must classify as current, or
        every freshly-initialised project would be warned at once."""
        state = classify_scaffolded_path(_prompt_at(tmp_path, DEFAULT_PROMPT))
        assert state is not None
        assert state.status == "current"
        assert state.shipped_label == DEFAULT_PROMPT_VERSION

    def test_a_body_kstrl_never_shipped_is_unrecognised(self, tmp_path: Path) -> None:
        """The stand-in for "no version stamp": nothing in the file says
        where it came from, and its digest matches nothing we shipped.
        An edited prompt and a prompt from a build outside this history
        are indistinguishable, so neither is claimed to be stale."""
        state = classify_scaffolded_path(_prompt_at(tmp_path, DEFAULT_PROMPT + "\nmy own rule\n"))
        assert state is not None
        assert state.status == "unrecognised"
        assert state.shipped_label is None

    def test_missing_file_is_absent(self, tmp_path: Path) -> None:
        state = classify_scaffolded_path(tmp_path / "scripts" / "kstrl" / "prompt.md")
        assert state is not None
        assert state.status == "absent"

    def test_unreadable_file_makes_no_claim(self, tmp_path: Path) -> None:
        path = tmp_path / "prompt.md"
        path.write_bytes(b"\xff\xfe not utf-8")
        state = classify_scaffolded_path(path)
        assert state is not None
        assert state.status == "unrecognised"

    def test_a_file_kstrl_does_not_scaffold_is_not_classified(self, tmp_path: Path) -> None:
        """``--prompt`` and ``PROMPT_FILE`` can point anywhere; a file
        the operator named themselves is not a scaffold to speak about."""
        path = tmp_path / "my_prompt.md"
        path.write_text(DEFAULT_PROMPT)
        assert classify_scaffolded_path(path) is None

    def test_classify_scaffold_covers_every_template(self, tmp_path: Path) -> None:
        states = classify_scaffold(tmp_path)
        assert [s.template.filename for s in states] == [t.filename for t in SCAFFOLDED_TEMPLATES]
        assert {s.status for s in states} == {"absent"}


class TestWarningText:
    def test_stale_file_warns_and_names_both_versions(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        notice = staleness_notice(_prompt_at(tmp_path, OLD_BODY))
        assert notice is not None
        assert "9.0.0" in notice.headline
        assert "9.1.0" in notice.headline
        assert "every change to that template since 9.0.0" in notice.advice
        assert "ks init --upgrade-prompts" in notice.advice

    def test_current_file_says_nothing(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        assert staleness_notice(_prompt_at(tmp_path, NEW_BODY)) is None

    def test_edited_file_says_nothing(self, tmp_path: Path) -> None:
        """A warning on a prompt somebody customised on purpose is noise,
        and noise gets ignored."""
        assert staleness_notice(_prompt_at(tmp_path, DEFAULT_PROMPT + "\nmine\n")) is None

    def test_missing_file_says_nothing(self, tmp_path: Path) -> None:
        """``run_loop`` already announces its own DEFAULT_PROMPT fallback."""
        assert staleness_notice(tmp_path / "scripts" / "kstrl" / "prompt.md") is None

    def test_a_relocated_prompt_is_not_told_to_run_a_no_op(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        """`ks init` only ever scaffolds and upgrades under
        scripts/kstrl/, so naming --upgrade-prompts to somebody whose
        [paths] prompt lives elsewhere would be advice that silently does
        nothing to their file."""
        path = tmp_path / "prompts" / "prompt.md"
        path.parent.mkdir(parents=True)
        path.write_text(OLD_BODY)
        notice = staleness_notice(path)
        assert notice is not None
        assert "9.0.0" in notice.headline
        assert "not the copy under scripts/kstrl/" in notice.advice
        assert "Run `ks init --upgrade-prompts`" not in notice.advice

    def test_the_oldest_body_names_itself_not_the_one_above_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Several rows back, the message must still name the row the
        file actually matches, not merely the previous one."""
        older = "# older\n"
        three_rows = ScaffoldedTemplate(
            filename="prompt.md",
            constant_name="DEFAULT_PROMPT",
            body=NEW_BODY,
            history=(
                (_sha256(older), "8.0.0"),
                (_sha256(OLD_BODY), "9.0.0"),
                (_sha256(NEW_BODY), "9.1.0"),
            ),
        )
        monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (three_rows,))
        notice = staleness_notice(_prompt_at(tmp_path, older))
        assert notice is not None
        assert "shipped at 8.0.0" in notice.headline
        assert "this kstrl ships 9.1.0" in notice.headline
        assert "since 8.0.0" in notice.advice


# The ledger exactly as this PR records it. Its whole value is the OLD
# rows: they are the only thing that can recognise a body already on an
# operator's disk. So the snapshot pins the prefix and the test below
# permits growth and nothing else, which is what makes "append only" a
# mechanism rather than a sentence in a comment.
_RECORDED_HISTORY: dict[str, tuple[tuple[str, str], ...]] = {
    "prompt.md": (
        (
            "15810563f3843b6634f6207d052710d72aa4fda0aa32ac86aa7718de86d34140",
            "pre-1.0.0 (2026-01-15)",
        ),
        (
            "9eb6d8f4c956d6fcacf2f39eed4a696e2755ce2ae0e24f53d08e98227fc37fc3",
            "pre-1.0.0 (2026-01-28)",
        ),
        (
            "5ec3e510a0dbd6ff41b181259b707f33a715715648cee0607ae5db6cf9992046",
            "pre-1.0.0 (2026-05-27)",
        ),
        ("a4a3a090139c370d7eecd12e3ef98055352110722750bb7b4cbf9bc50b1b9125", "1.0.0"),
        ("aa7fa6acb045dc6105d1a4c4ce8b687e1e04289c7b751eb0373b7c59dca3f7ae", "1.1.0"),
        # The body #276 measured a wasted engineer iteration on, at
        # roughly $4. Recognising it on disk is why the ledger exists.
        ("4f7370f5f4efb2d9b89ce6ae09fcbf7e5c3c8fb3db22cdeb07a9221ccbc638dc", "1.1.1"),
        ("9bde9b20785f3740396906d1d199c2228c553c11ae956dc2f85d8aa2439fb49b", "1.2.0"),
        ("392eb698daf71d486a9d4573698df3bb2b3ca4be87c178657accc8a66c54f384", "1.3.0"),
    ),
    "understand_prompt.md": (
        ("5514376b0beeb484755d2d7d5effbe9a749b2d0972ddd30e7911e47bcf73e4ff", "2026-01-14"),
        ("1e700b55db8316392de146c549ef9fe9acf503af5c6ba2780f9d341728ac39c4", "2026-01-15"),
        ("fd02d9e3f2e559db5625c4db2d81ef0d24df481a4f4d4f5506fddd9b0962c53a", "2026-07-20"),
        ("cfd43bfeb80eaaf559ccb32d993fc2c5b2471ff90c7816648743135c2aa29688", "2026-07-21"),
    ),
    "feature_understand_prompt.md": (
        ("5096447a6228e93d7d824ff5e1a334ef3eaf9edc9314a3fb7c6f7f04936cf06f", "2026-01-28"),
        ("e05fedd0ea1aff624966f4ee1e572c1af6f3926dd1b38b64678fdd6525a6f31a", "2026-07-20"),
        ("eb3637acf1918da23e27ad3f4d30bab32b1edd797b4bd1b5587b82b656affb09", "2026-07-21"),
    ),
}


def _scaffolded_by_init() -> set[tuple[str, str]]:
    """``(filename, constant_name)`` for every prompt template
    ``run_init`` actually writes, read out of its own source.

    The same discipline H3 applies one level up with
    ``test_no_unenrolled_prompt_constants``: without it, the next
    ``_create_if_missing(kstrl_dir / "x_prompt.md", DEFAULT_X_PROMPT,
    ui)`` would be un-ledgered, un-warned and un-upgradable, and nothing
    would fail. Reading the source rather than the ledger is the point,
    because the ledger is the thing under test.
    """
    source = Path(init_cmd.__file__).read_text(encoding="utf-8")
    found: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_create_if_missing"):
            continue
        if len(node.args) < 2:
            continue
        target, content = node.args[0], node.args[1]
        # `kstrl_dir / "<name>"`, and a bare constant for the body.
        if not (isinstance(target, ast.BinOp) and isinstance(target.right, ast.Constant)):
            continue
        if not isinstance(content, ast.Name) or not content.id.endswith("_PROMPT"):
            continue
        found.add((str(target.right.value), content.id))
    return found


class TestLedgerIntegrity:
    """The ledger is the mechanism. If it can drift, there is no plan."""

    def test_every_live_body_is_the_newest_row(self) -> None:
        for template in SCAFFOLDED_TEMPLATES:
            digest, _label = template.history[-1]
            assert _sha256(template.body) == digest, (
                f"{template.constant_name} changed without a new row in "
                f"SCAFFOLDED_TEMPLATES (kstrl/init_cmd.py). APPEND "
                f"({_sha256(template.body)!r}, <label>) to its history; "
                "never edit or drop an older row, because an old row is "
                "the only thing that can recognise a copy already on "
                "someone's disk."
            )

    def test_recorded_rows_are_never_edited_or_dropped(self) -> None:
        """Append-only, enforced rather than asked for."""
        assert {t.filename for t in SCAFFOLDED_TEMPLATES} == set(_RECORDED_HISTORY)
        for template in SCAFFOLDED_TEMPLATES:
            recorded = _RECORDED_HISTORY[template.filename]
            assert template.history[: len(recorded)] == recorded, (
                f"{template.constant_name}'s recorded history changed. A "
                "new body is an APPEND: add a row at the end here and in "
                "kstrl/init_cmd.py, and leave every earlier row exactly "
                "as it is. Editing or dropping one destroys the only "
                "record that can recognise a copy already on an "
                "operator's disk."
            )

    def test_history_rows_are_unique_and_non_empty(self) -> None:
        for template in SCAFFOLDED_TEMPLATES:
            digests = [row[0] for row in template.history]
            assert digests, f"{template.constant_name} has no history"
            assert len(set(digests)) == len(digests)
            labels = [row[1] for row in template.history]
            assert len(set(labels)) == len(labels)

    def test_every_template_init_scaffolds_is_enrolled(self) -> None:
        enrolled = {(t.filename, t.constant_name) for t in SCAFFOLDED_TEMPLATES}
        assert _scaffolded_by_init() == enrolled, (
            "run_init and SCAFFOLDED_TEMPLATES disagree about which "
            "prompt templates exist. Add the new one to the ledger with "
            "its shipped history, or fix the filename/constant pairing; "
            "an un-enrolled template reproduces #286 for itself."
        )

    def test_filenames_match_what_init_scaffolds(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)
        for template in SCAFFOLDED_TEMPLATES:
            assert (tmp_path / "scripts" / "kstrl" / template.filename).exists()
