"""A fake `mutmut` executable for R8.5 Layer 2 (#152) end-to-end tests.

Real mutmut is not installed in this repo (measurements.md: "mutmut is
NOT installed in this repo or on PATH"), so `tests/test_diff_mutation.py`
drives `kstrl.verify`'s real spawn plumbing against a small shell script
instead of the real tool - the house pattern (`tests/helpers/fakegh.py`)
rather than mocking `subprocess.run` or `shutil.which`
(`scripts/precommit/check_no_mocks.py` is in the pre-commit set and would
refuse a mock here).

Every SHAPE the script prints - the junitxml this module builds, and the
run/report exit codes callers choose - is copied from real mutmut 2.5.1
runs recorded in `measurements.md` section 2e; a test that installs canned
junit rows through :func:`junit` names that section in its docstring. One
detail was imagined rather than measured until #152 round 2: the backup
file's permission bits. Real `mutmut.mutate_file` writes the backup with
`open(path + '.bak', 'w')` (the umask default), not a copy of the
source's mode, and `mutmut.run_mutation`'s `finally` restores it with
`shutil.move` (a rename), which carries that mode onto the target. The
fake now creates the backup the same way (a shell redirection, not `cp`)
so a test can see the mode loss `cp` was hiding.

The fake cannot read configuration from the environment: `run_scrubbed`
(`kstrl/verify.py`) scrubs everything outside
`SCRUB_ENV_ALLOWED_NAMES`/`SCRUB_ENV_ALLOWED_PREFIXES`. Every path this
script needs - where to record argv, what junitxml to print, which file
to corrupt - is baked into its body with an f-string instead of read from
an environment variable.

#152 simplify pass, D2: this module used to carry TWO shell templates,
`_FAKE_MUTMUT` and `_FAILING_MUTMUT`, differing only in whether the `run`
branch does the cache/mutate/sleep/restore work and prints a stderr line
first, and whether `junitxml` prints the canned report or exits 0 with
nothing - the two failure-before-anything-runs facts measurements.md 2a
records. One template now carries both as shell-side `if` gates
(`{cache}`, `{has_stderr}`) rather than two copies of the case statement
around them.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from tests.helpers.executables import put_on_path

#: mutmut 2.5.1's junitxml rendering, per mutant status (measurements.md
#: section 2e): a killed mutant is a bare ``<testcase>``; a survivor
#: carries ``<failure type="failure" message="bad_survived">``; an
#: untested mutant, under ``--untested-policy=error``, carries
#: ``<error type="error" message="untested"/>``; a timeout carries
#: ``<error type="timeout" message="bad_timeout"/>``.
_STATUS_ELEMENT: dict[str, str] = {
    "killed": "",
    "survived": '<failure type="failure" message="bad_survived"></failure>',
    "untested": '<error type="error" message="untested"/>',
    "timeout": '<error type="timeout" message="bad_timeout"/>',
}


def junit(*mutants: tuple[int, str, int, str]) -> str:
    """``(id, file, line, status)`` rows rendered as mutmut 2.5.1 renders
    them (measurements.md section 2e).

    ``status`` is one of ``"killed"``, ``"survived"``, ``"untested"``,
    ``"timeout"``. Every test in ``tests/test_diff_mutation.py`` that
    calls this points its docstring at the same section.
    """
    cases = [
        f'<testcase name="Mutant #{mutant_id}" file="{path}" line="{line}">'
        f"{_STATUS_ELEMENT[status]}</testcase>"
        for mutant_id, path, line, status in mutants
    ]
    body = "".join(cases)
    count = len(mutants)
    return (
        '<?xml version="1.0" ?>'
        f'<testsuites disabled="0" errors="0" failures="0" tests="{count}" time="0.0">'
        f'<testsuite name="mutmut" tests="{count}">{body}</testsuite>'
        "</testsuites>"
    )


#: One template for both installers below (#152 simplify pass, D2).
#: ``{recdir}``, ``{mutate}``, ``{sleep_seconds}``, ``{run_exit}``,
#: ``{restore}``, ``{cache}``, ``{has_stderr}`` and ``{stderr}`` are all
#: filled in by :func:`put_mutmut_on_path` and :func:`put_failing_mutmut`
#: - every placeholder on every call, whether or not that installer's
#: shape uses it, so ``str.format`` never raises ``KeyError`` on the
#: branch it does not take.
#:
#: ``{cache}`` (``1``/``0``) gates the whole cache/mutate/sleep/restore
#: block in ``run`` and the choice, in ``junitxml``, between printing the
#: canned report and exiting 0 with nothing - real mutmut's shape when it
#: fails FATALLY before doing any work at all (measurements.md 2a: the
#: missing ``whatthepatch`` extra touches neither ``.mutmut-cache`` nor
#: any ``.bak``). ``{has_stderr}`` (``1``/``0``) gates one ``echo`` line;
#: ``{stderr}`` is already shell-quoted by the Python side
#: (:func:`shlex.quote`), so it is spelled UNQUOTED in the template - a
#: second layer of shell quoting around an already-quoted value would
#: escape the embedded double quote real mutmut's own remedy line carries
#: (``'pip install --force-reinstall mutmut[patch]"'``).
#:
#: Two subcommands only, matching what `check_diff_mutation` actually
#: spawns (``kstrl/verify.py::_mutation_spawns``); anything else exits 97
#: so an unexpected subcommand is loud rather than silently a no-op.
_FAKE_MUTMUT = """#!/bin/sh
case "$1" in
  run)
    shift
    for a in "$@"; do
      printf '%s\\n' "$a" >> "{recdir}/argv-run.txt"
      case "$a" in
        --use-patch-file=*)
          cp "${{a#--use-patch-file=}}" "{recdir}/patch.diff"
          ;;
      esac
    done
    if [ {has_stderr} -eq 1 ]; then
      echo {stderr} 1>&2
    fi
    if [ {cache} -eq 1 ]; then
      touch .mutmut-cache
      if [ -n "{mutate}" ]; then
        cat "{mutate}" > "{mutate}.bak"
        printf 'MUTANT\\n' >> "{mutate}"
      fi
      if [ {sleep_seconds} -gt 0 ]; then
        sleep {sleep_seconds}
      fi
      if [ -n "{mutate}" ] && [ {restore} -eq 1 ]; then
        mv "{mutate}.bak" "{mutate}"
      fi
    fi
    exit {run_exit}
    ;;
  junitxml)
    shift
    for a in "$@"; do
      printf '%s\\n' "$a" >> "{recdir}/argv-junitxml.txt"
    done
    if [ {cache} -eq 1 ]; then
      cat "{recdir}/junit.xml"
    else
      exit 0
    fi
    ;;
  *)
    exit 97
    ;;
esac
"""


def put_mutmut_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    junit: str,
    run_exit: int = 0,
    sleep: float = 0.0,
    mutate: str = "",
    restore: bool = False,
) -> Path:
    """Install a fake `mutmut`; return the directory it records into.

    Every test in ``tests/test_diff_mutation.py`` binds that return value
    to ``recdir`` and reads ``recdir / "argv-run.txt"`` etc.

    ``<dir>/argv-run.txt``      the argv of every `run` invocation, one token per line
    ``<dir>/patch.diff``        a copy of the --use-patch-file the run was handed
    ``<dir>/argv-junitxml.txt`` the argv of every `junitxml` invocation
    ``junit``                   what `junitxml` prints on stdout
    ``mutate``                  a file (relative to the check's cwd) to back up
                                 and corrupt before sleeping - the SIGTERM-leaves-
                                 a-mutant-on-disk shape measured in measurements 2h
    ``restore``                 model mutmut's own ``finally: move(bak, filename)``
                                 (``mutmut.run_mutation``) by renaming the backup
                                 back over the target before exiting
    """
    recdir = tmp_path / "mutmut-record"
    recdir.mkdir()
    (recdir / "junit.xml").write_text(junit, encoding="utf-8")
    body = _FAKE_MUTMUT.format(
        recdir=recdir,
        mutate=mutate,
        sleep_seconds=int(sleep),
        run_exit=run_exit,
        restore=1 if restore else 0,
        cache=1,
        has_stderr=0,
        stderr=shlex.quote(""),
    )
    put_on_path(tmp_path, monkeypatch, "mutmut", body)
    return recdir


def put_failing_mutmut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stderr: str,
    exit_code: int = 1,
) -> Path:
    """Install a fake `mutmut` whose `run` subcommand fails FATALLY and
    immediately - no cache, no ``.bak``, nothing written - printing
    ``stderr`` and exiting ``exit_code``. For the mutmut failure modes
    measured BEFORE mutmut does any work at all (measurements.md 2a).

    Records `run` and `junitxml` invocations the same way
    :func:`put_mutmut_on_path` does, into the same two file names, so a
    test can assert which one (if either) ran.
    """
    recdir = tmp_path / "mutmut-record"
    recdir.mkdir(exist_ok=True)
    body = _FAKE_MUTMUT.format(
        recdir=recdir,
        mutate="",
        sleep_seconds=0,
        run_exit=exit_code,
        restore=0,
        cache=0,
        has_stderr=1,
        stderr=shlex.quote(stderr),
    )
    put_on_path(tmp_path, monkeypatch, "mutmut", body)
    return recdir
