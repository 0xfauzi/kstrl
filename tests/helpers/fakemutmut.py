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

#391: the shapes below are now grounded in
`tests/test_mutmut_format_fidelity.py`'s recorded report as well as
measurements.md - that file's own docstring names the exact commands
that produced the recording. The `run` branch's ``--tests-dir`` refusal
and the ``cache-before-run.txt`` recording are new in that round; see
their own comments below for what each models and what it only
observes.

Round 2 of #391: the `junitxml` branch's "no cache on disk" check is
also an OBSERVATION of real mutmut, like `cache-before-run.txt`, and not
a shape invented for the fake. With mutmut 2.5.1 in a throwaway venv, on
a git fixture where `mutmut run` had already written a 36864-byte
`.mutmut-cache`: `rm -f .mutmut-cache && mutmut junitxml
--untested-policy=error --suspicious-policy=error` exits 0 and prints
`mutmut cache is out of date, clearing it...` followed by a testsuites
report with zero tests. Before this round the fake decided whether to
print a report by reading the build-time `{cache}` template flag rather
than by looking at whether `.mutmut-cache` is actually on disk when
`junitxml` runs, so nothing in the suite could see the ORDER of the two
`.mutmut-cache` deletes in `kstrl/verify.py::_mutmut_measure` - a delete
planted between the run spawn and the report spawn passed every test.
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

#: A fixed placeholder for the ``<system-out>`` child mutmut 2.5.1
#: appends to EVERY testcase, killed ones included (measurements 2c and
#: ``<lane>/c-junit-complete.xml``: all eleven testcases in the recording
#: carry one). Only the ELEMENT and its position (after the status
#: element) are copied from the recording; the source-line content is
#: not - callers that need a specific line write their own testcase.
_SYSTEM_OUT = "<system-out>    mutated source line</system-out>"


def junit(*mutants: tuple[int, str, int, str]) -> str:
    """``(id, file, line, status)`` rows rendered as mutmut 2.5.1 renders
    them (measurements.md section 2e).

    ``status`` is one of ``"killed"``, ``"survived"``, ``"untested"``,
    ``"timeout"``. Every test in ``tests/test_diff_mutation.py`` that
    calls this points its docstring at the same section.

    Element order inside a testcase is the status element FIRST, then
    ``<system-out>`` (:data:`_SYSTEM_OUT`) - measured in
    ``<lane>/c-junit-complete.xml``, and pinned by
    ``tests/test_mutmut_format_fidelity.py``.
    """
    cases = [
        f'<testcase name="Mutant #{mutant_id}" file="{path}" line="{line}">'
        f"{_STATUS_ELEMENT[status]}{_SYSTEM_OUT}</testcase>"
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
#: Two subcommands only, matching what both mutation checks actually
#: spawn (``kstrl/verify.py::_mutmut_run_spawn`` and
#: ``_mutmut_report_spawn``); anything else is RECORDED then exits 97, so
#: a driver that still asks mutmut for a text report (``mutmut results``)
#: is caught rather than silently a no-op.
#:
#: ``{recdir}/cache-before-run.txt`` (#391) is an OBSERVATION, not
#: invented tool behaviour, unlike every other shape in this module:
#: it records whether ``.mutmut-cache`` was present when ``run`` started,
#: so a test can prove kstrl deleted a stale cache before spawning. The
#: ``--tests-dir`` refusal right after it IS modelled on real mutmut:
#: without that flag and with neither ``tests/`` nor ``test/`` in the
#: cwd, mutmut 2.5.1 raises the ``FileNotFoundError`` below verbatim
#: (measurements.md 1b).
_FAKE_MUTMUT = """#!/bin/sh
case "$1" in
  run)
    shift
    if [ -f .mutmut-cache ]; then
      printf 'present\\n' >> "{recdir}/cache-before-run.txt"
    else
      printf 'absent\\n' >> "{recdir}/cache-before-run.txt"
    fi
    has_tests_dir=0
    for a in "$@"; do
      printf '%s\\n' "$a" >> "{recdir}/argv-run.txt"
      case "$a" in
        --tests-dir=*)
          has_tests_dir=1
          ;;
        --use-patch-file=*)
          cp "${{a#--use-patch-file=}}" "{recdir}/patch.diff"
          ;;
      esac
    done
    if [ $has_tests_dir -eq 0 ] && [ ! -d tests ] && [ ! -d test ]; then
      echo 'FileNotFoundError: No test folders found in current' \\
        'folder. Run this where there is a "tests" or "test" folder.' 1>&2
      exit 1
    fi
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
    if [ ! -f .mutmut-cache ]; then
      printf 'mutmut cache is out of date, clearing it...\\n'
      printf '<?xml version="1.0" ?>\\n'
      printf '<testsuites disabled="0" errors="0" failures="0" tests="0" time="0.0"/>\\n'
      exit 0
    fi
    if [ {cache} -eq 1 ]; then
      cat "{recdir}/junit.xml"
    else
      exit 0
    fi
    ;;
  *)
    printf '%s\\n' "$1" >> "{recdir}/argv-other.txt"
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
