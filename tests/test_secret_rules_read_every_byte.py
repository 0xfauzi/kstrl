"""``ks check`` finds a key in a file git treats as binary (#695).

Both secret rules, ``bad_patterns`` and the ``[policy]`` envelope's
``secret_pattern``, read the lines a change ADDS from ``git diff``. git
prints no lines at all for a path it treats as binary: a NUL byte near the
start of the file, or ``-diff`` or ``binary`` in ``.gitattributes``. A
``diff=<driver>`` attribute, ``diff.external`` and ``color.ui = always``
change the lines it does print. ``diff.dstPrefix`` changes the header path,
so the key is filed under a path that is not in the change. And
``str.splitlines()`` broke a line at a carriage return, a form feed and
other bytes git does not end a line at, so a key after one of them lost its
``+``. Each of these used to hide a key from both rules, and both rows
reported a pass.

Since #646 slice 4 a key is reported once, by the rule that owns it: the
envelope when ``[policy] enabled`` is true, ``bad_patterns`` when it is not.
So each test runs ``ks check`` with the envelope on and with it off, and
each rule is shown reading every byte.

Each test builds a real repository, drives the real command through
``CliRunner`` and reads the ``--json`` document back. Nothing here is
specific to a language: the files are plain bytes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kstrl.cli import cli
from tests.helpers import gitrepo
from tests.helpers.stack_confirmation import confirm_stack, write_stack
from tests.spine_utils import git

_OK_COMMAND = f"{sys.executable} -c 'print(1)'"

#: Built from parts, never as one literal: the assembled spelling is what a
#: secret scanner refuses to let be committed.
KEY = "AKIA" + "Q" * 16

#: An added line holding the key, as git stores it (no trailing newline).
KEY_LINE = f"token = {KEY}".encode()

#: The start of a PNG file: a NUL byte, and bytes that are not utf-8.
IMAGE_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\n"

POLICY = "[policy]\nenabled = true\nlicense_use_network = false\n"
NO_POLICY = "[policy]\nenabled = false\n"


def _repo(
    tmp_path: Path,
    base: dict[str, bytes],
    branch: dict[str, bytes],
    git_config: tuple[tuple[str, str], ...] = (),
    *,
    envelope: bool = True,
) -> Path:
    """A repo whose ``main`` holds ``base`` and whose ``feature`` adds ``branch``."""
    root = tmp_path / ("envelope-on" if envelope else "envelope-off")
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    gitrepo.set_identity(root)
    for key, value in git_config:
        git("config", key, value, cwd=root)
    write_stack(root, {"tests": _OK_COMMAND, "typecheck": _OK_COMMAND, "lint": _OK_COMMAND})
    (root / "kstrl.toml").write_text(
        (root / "kstrl.toml").read_text(encoding="utf-8") + (POLICY if envelope else NO_POLICY),
        encoding="utf-8",
    )
    for files, message in ((base, "init"), (branch, "change")):
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        git("add", "-A", cwd=root)
        git("commit", "-q", "-m", message, cwd=root)
        if message == "init":
            git("checkout", "-q", "-b", "feature", cwd=root)
            confirm_stack(root)
    return root


def _rows(root: Path) -> dict[str, dict[str, Any]]:
    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--json"])
    assert result.exit_code in (0, 1), result.output
    document: dict[str, Any] = json.loads(result.stdout)
    return {
        row["name"]: row
        for row in document["checks"]
        if row["name"] in ("bad_patterns", "policy_envelope")
    }


def _assert_key_found(
    tmp_path: Path,
    base: dict[str, bytes],
    branch: dict[str, bytes],
    path: str,
    line: bytes,
    git_config: tuple[tuple[str, str], ...] = (),
) -> None:
    """The key is reported once, by the rule that owns it (#646 slice 4).

    With the envelope on, ``policy_envelope`` fails and names the line's hash,
    and ``bad_patterns`` passes and says the envelope checked secrets. With
    the envelope off, ``bad_patterns`` fails and names the file.

    The hash is the first 12 hex characters of the sha256 of the line's bytes
    exactly as git stores them, so an approval written for a text line before
    #695 still covers the same line after it (#595).
    """
    digest = hashlib.sha256(line).hexdigest()[:12]
    on = _rows(_repo(tmp_path, base, branch, git_config, envelope=True))
    assert on["policy_envelope"]["passed"] is False
    assert on["policy_envelope"]["details"] == [f"Possible secrets in added lines: {path}#{digest}"]
    assert on["bad_patterns"]["passed"] is True
    assert on["bad_patterns"]["details"] == []
    assert on["bad_patterns"]["message"] == (
        "secrets: checked by policy_envelope; Python rules: scanned 0 of 0 changed Python files"
    )
    off = _rows(_repo(tmp_path, base, branch, git_config, envelope=False))
    assert "policy_envelope" not in off
    assert off["bad_patterns"]["passed"] is False
    assert off["bad_patterns"]["details"] == [f"{path}: possible secret/credential detected"]


@pytest.mark.parametrize(
    ("attributes", "content"),
    [
        pytest.param(b"", KEY_LINE + b"\n", id="text-file-control"),
        pytest.param(b"", KEY_LINE + b"\r\n", id="crlf-control"),
        pytest.param(b"*.dat -diff\n", KEY_LINE + b"\n", id="minus-diff-attribute"),
        pytest.param(b"*.dat binary\n", KEY_LINE + b"\n", id="binary-attribute"),
        pytest.param(b"", IMAGE_HEAD + KEY_LINE + b"\n", id="nul-byte"),
    ],
)
def test_a_key_in_a_file_git_treats_as_binary_is_reported_once(
    tmp_path: Path, attributes: bytes, content: bytes
) -> None:
    base = {"README": b"x\n", ".gitattributes": attributes}

    _assert_key_found(tmp_path, base, {"conf/keys.dat": content}, "conf/keys.dat", KEY_LINE)


def test_a_key_on_a_line_that_is_not_utf_8_is_reported_once(tmp_path: Path) -> None:
    """The bytes around the key are kept as they are: neither refused nor replaced."""
    line = b"\xff " + KEY_LINE + b" \xfe"
    branch = {"assets/logo.dat": IMAGE_HEAD + line + b"\n"}

    _assert_key_found(tmp_path, {"README": b"x\n"}, branch, "assets/logo.dat", line)


@pytest.mark.parametrize(
    "separator",
    [
        pytest.param(b"\r", id="carriage-return"),
        pytest.param(b"\x0c", id="form-feed"),
        pytest.param(b"\x1c", id="file-separator"),
    ],
)
def test_a_key_after_a_byte_that_is_not_a_newline_is_reported_once(
    tmp_path: Path, separator: bytes
) -> None:
    """git ends a line at a newline only; a key after any other control byte
    on the same line is on that line."""
    line = b"junk" + separator + KEY_LINE
    branch = {"assets/logo.dat": IMAGE_HEAD + line + b"\n"}

    _assert_key_found(tmp_path, {"README": b"x\n"}, branch, "assets/logo.dat", line)


@pytest.mark.parametrize(
    ("git_config", "attributes"),
    [
        pytest.param((("diff.hide.textconv", "true"),), b"*.dat diff=hide\n", id="textconv"),
        pytest.param((("diff.external", "true"),), b"", id="external-diff"),
        pytest.param((("color.ui", "always"),), b"", id="color-always"),
        pytest.param((("diff.dstPrefix", "zz/"),), b"", id="dst-prefix"),
    ],
)
def test_a_key_behind_a_git_setting_that_rewrites_the_diff_is_reported_once(
    tmp_path: Path, git_config: tuple[tuple[str, str], ...], attributes: bytes
) -> None:
    base = {"README": b"x\n", ".gitattributes": attributes}
    branch = {"conf/keys.dat": KEY_LINE + b"\n"}

    _assert_key_found(tmp_path, base, branch, "conf/keys.dat", KEY_LINE, git_config)


def test_a_binary_file_with_no_key_passes_the_secret_rule_either_way(tmp_path: Path) -> None:
    """The control: reading every byte does not turn an image into a refusal."""
    image = IMAGE_HEAD + b"\xff\xfe\x80 no key here\n"
    base, branch = {"README": b"x\n"}, {"assets/logo.dat": image}

    on = _rows(_repo(tmp_path, base, branch, envelope=True))
    off = _rows(_repo(tmp_path, base, branch, envelope=False))

    assert on["bad_patterns"]["passed"] is True
    assert on["policy_envelope"]["passed"] is True
    assert on["policy_envelope"]["details"] == []
    assert off["bad_patterns"]["passed"] is True
    assert off["bad_patterns"]["details"] == []


def test_a_bad_patterns_row_that_fails_on_another_rule_still_names_the_owner(
    tmp_path: Path,
) -> None:
    """With the envelope on, a ``bad_patterns`` row that fails for another
    reason (here an empty Python file) still says who checked secrets, and
    the key beside it is reported once, by the envelope (#646 slice 4)."""
    branch = {"conf/keys.dat": KEY_LINE + b"\n", "pkg/empty.py": b""}
    digest = hashlib.sha256(KEY_LINE).hexdigest()[:12]

    on = _rows(_repo(tmp_path, {"README": b"x\n"}, branch, envelope=True))

    assert on["bad_patterns"]["passed"] is False
    assert on["bad_patterns"]["message"] == (
        "1 issues found in changed files; secrets: checked by policy_envelope"
    )
    assert on["bad_patterns"]["details"] == ["pkg/empty.py: empty file"]
    assert on["policy_envelope"]["passed"] is False
    assert on["policy_envelope"]["details"] == [
        f"Possible secrets in added lines: conf/keys.dat#{digest}"
    ]


def test_a_baseline_says_bad_patterns_measured_nothing_when_the_envelope_owns_secrets(
    tmp_path: Path,
) -> None:
    """With the envelope on, ``bad_patterns`` reads no diff content, so on a diff
    with no Python file it measured nothing, and the baseline records it as
    unmeasured in the row's own words (#646 slice 4)."""
    root = _repo(tmp_path, {"README": b"x\n"}, {"conf/keys.dat": KEY_LINE + b"\n"})

    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--write-baseline"])

    assert result.exit_code in (0, 1), result.output
    document = json.loads((root / "scripts/kstrl/baseline.json").read_text(encoding="utf-8"))
    assert "bad_patterns" in document["unmeasured_checks"]
    assert document["unmeasured_reasons"]["bad_patterns"] == (
        "secrets: checked by policy_envelope; Python rules: scanned 0 of 0 changed Python files"
    )


def test_an_empty_diff_says_no_files_with_the_envelope_on(tmp_path: Path) -> None:
    """With the envelope on and nothing in the diff, ``bad_patterns`` says
    there were no files, in the same words as ``diff_scope``; it does not
    claim the envelope checked secrets in a diff that has none (#646 slice 4)."""
    root = _repo(tmp_path, {"README": b"x\n"}, {"conf/keys.dat": b"no key\n"})

    result = CliRunner().invoke(cli, ["check", "--root", str(root), "--base", "feature", "--json"])

    assert result.exit_code in (0, 1), result.output
    rows = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
    assert rows["bad_patterns"]["passed"] is True
    assert rows["bad_patterns"]["message"] == "no files in the diff"
