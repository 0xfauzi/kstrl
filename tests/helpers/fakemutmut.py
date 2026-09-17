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
junit rows through :func:`junit` names that section in its docstring.

The fake cannot read configuration from the environment: `run_scrubbed`
(`kstrl/verify.py`) scrubs everything outside
`SCRUB_ENV_ALLOWED_NAMES`/`SCRUB_ENV_ALLOWED_PREFIXES`. Every path this
script needs - where to record argv, what junitxml to print, which file
to corrupt - is baked into its body with an f-string instead of read from
an environment variable.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from tests.helpers.executables import write_executable

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


#: ``{recdir}``, ``{mutate}``, ``{sleep_seconds}`` and ``{run_exit}`` are
#: filled in by :func:`put_mutmut_on_path`. Two subcommands only, matching
#: what `check_diff_mutation` actually spawns
#: (``kstrl/verify.py::_mutation_spawns``); anything else exits 97 so an
#: unexpected subcommand is loud rather than silently a no-op.
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
    touch .mutmut-cache
    if [ -n "{mutate}" ]; then
      cp "{mutate}" "{mutate}.bak"
      printf 'MUTANT\\n' >> "{mutate}"
    fi
    if [ {sleep_seconds} -gt 0 ]; then
      sleep {sleep_seconds}
    fi
    exit {run_exit}
    ;;
  junitxml)
    shift
    for a in "$@"; do
      printf '%s\\n' "$a" >> "{recdir}/argv-junitxml.txt"
    done
    cat "{recdir}/junit.xml"
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
    """
    recdir = tmp_path / "mutmut-record"
    recdir.mkdir()
    (recdir / "junit.xml").write_text(junit, encoding="utf-8")
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    body = _FAKE_MUTMUT.format(
        recdir=recdir,
        mutate=mutate,
        sleep_seconds=int(sleep),
        run_exit=run_exit,
    )
    write_executable(bindir / "mutmut", body)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return recdir


#: ``{recdir}``, ``{stderr}`` and ``{exit_code}`` filled in by
#: :func:`put_failing_mutmut`. Unlike :data:`_FAKE_MUTMUT`, the `run`
#: branch here does NOT touch ``.mutmut-cache`` or any ``.bak`` - real
#: mutmut's own fatal-before-anything-runs failures (measurements.md 2a:
#: the missing ``whatthepatch`` extra) touch neither.
_FAILING_MUTMUT = """#!/bin/sh
case "$1" in
  run)
    shift
    for a in "$@"; do
      printf '%s\\n' "$a" >> "{recdir}/argv-run.txt"
    done
    echo {stderr} 1>&2
    exit {exit_code}
    ;;
  junitxml)
    shift
    for a in "$@"; do
      printf '%s\\n' "$a" >> "{recdir}/argv-junitxml.txt"
    done
    exit 0
    ;;
  *)
    exit 97
    ;;
esac
"""


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
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    body = _FAILING_MUTMUT.format(
        recdir=recdir,
        stderr=shlex.quote(stderr),
        exit_code=exit_code,
    )
    write_executable(bindir / "mutmut", body)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return recdir
