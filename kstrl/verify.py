"""Phase 1: Mechanical verification - independent checks after agent execution."""

from __future__ import annotations

import os
import py_compile
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kstrl import git, licensing

if TYPE_CHECKING:
    from kstrl.fixtures import FixturesConfig
from kstrl.adequacy import (
    AdequacyConfig,
    Mutant,
    MutationScore,
    PatchCoverage,
    coverage_targets,
    evaluate_layer0,
    is_test_path,
    layer0_blocks,
    measure_patch_coverage,
    mutation_patch,
    parse_mutant_report,
    score_mutants,
)
from kstrl.atomicio import atomic_write_text
from kstrl.config import component_progress_path, relative_to_root
from kstrl.findings import Finding
from kstrl.gateparse import (
    GATE_LINT,
    GATE_TEST,
    GATE_TYPECHECK,
    parse_gate_output,
    validate_tool,
)
from kstrl.guards import path_is_allowed
from kstrl.jsonread import read_json
from kstrl.parsers import (
    ParsedOutput,
    add_source_context,
    generate_fix_hint,
)
from kstrl.policy import (
    DEFAULT_SECRET_PATTERNS,
    PolicyConfig,
    PolicyConfigError,
    PolicyViolation,
    _scan_secrets,
    classify_license,
    evaluate_policy,
    parse_added_lines,
)
from kstrl.prd import PRD
from kstrl.procdispose import drain_or_abandon
from kstrl.procgroup import signal_process_tree
from kstrl.statedir import STATE_DIR_NAME

# R2.6 env scrub: verification subprocesses execute agent-authored code
# (the project's tests, linters run over agent files, CLI fixtures), so
# they must never inherit the harness's secrets. Allowlist, not denylist:
# only names below (or matching a prefix below) pass through, everything
# else - ANTHROPIC_API_KEY, OPENAI_API_KEY, cloud credentials, gh tokens -
# is dropped. The set was determined empirically: `uv run pytest` with a
# fresh venv succeeds under env -i with only PATH/HOME/TMPDIR/TERM/LANG
# (uv locates its cache via HOME); the rest are the locale, venv, uv, and
# CPython knobs a project's own commands legitimately consume, plus the
# XDG cache/data paths uv honors when set.
SCRUB_ENV_ALLOWED_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        "TERM",
        "VIRTUAL_ENV",
        "CI",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    }
)
SCRUB_ENV_ALLOWED_PREFIXES: tuple[str, ...] = ("LC_", "UV_", "PYTHON")

# Belt over the allowlist's braces: an allowed prefix must never smuggle a
# secret through (UV_PUBLISH_TOKEN matches UV_*). Any name containing one
# of these fragments is dropped even when the allowlist admits it.
_SCRUB_ENV_SENSITIVE_FRAGMENTS: tuple[str, ...] = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)


def scrubbed_subprocess_env() -> dict[str, str]:
    """Allowlist-filtered copy of ``os.environ`` for verification subprocesses."""
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name not in SCRUB_ENV_ALLOWED_NAMES and not name.startswith(SCRUB_ENV_ALLOWED_PREFIXES):
            continue
        if any(frag in name for frag in _SCRUB_ENV_SENSITIVE_FRAGMENTS):
            continue
        env[name] = value
    return env


_SCRUB_TERM_GRACE_SECONDS = 5.0


def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal the child's whole process group, direct-child fallback.

    A one-line forward to :func:`kstrl.procgroup.signal_process_tree`,
    kept as a name because ``tests/test_hitl_env_scrub.py`` pins this
    module's timeout behaviour through it and because the name says what
    the verification path is doing at the point it does it.

    #308 lifted the pid/pgid GUARD out of here and left the routine
    around it, so ``os.killpg`` itself stayed spelled in this module and
    in ``agents.proc`` as well as in ``procgroup``. #329 is what that
    costs: three spellings is how a fourth arrives unguarded. The whole
    routine now has one home.
    """
    signal_process_tree(proc, sig)


class ChildOutputDecodeError(RuntimeError):
    """A verification child produced bytes that are not valid utf-8.

    :func:`run_scrubbed` chose ``encoding="utf-8"``, so it is the one place
    that can name this fault; every caller would otherwise meet a bare
    ``UnicodeDecodeError`` (a ``ValueError``) that no handler here was written
    for, and the mechanical verifier would die with a traceback instead of
    returning a verdict (#416). Every caller answers for it in its own result
    type, as it answers for a timeout: the check ran, measured nothing, and
    fails closed. The two exceptions, measured rather than asserted away
    (#416's simplify review): ``contract._abort_merge`` and the prune call in
    ``contract._remove_temp_worktree``, whose results were never read and
    which swallow it so cleanup is not blocked. kstrl does not weaken the
    decode with ``errors=`` to make it go away (#409).
    """


def run_scrubbed(
    cmd: str | list[str],
    *,
    cwd: Path,
    timeout: float,
    term_grace: float = _SCRUB_TERM_GRACE_SECONDS,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a verification subprocess: scrubbed env, own process group.

    Drop-in for the ``subprocess.run(..., capture_output=True, text=True,
    timeout=...)`` calls verification used to make, with two differences
    (R2.6): the child gets :func:`scrubbed_subprocess_env` instead of the
    harness environment, and on timeout the ENTIRE process group is
    signalled (SIGTERM, grace, SIGKILL) so a test that backgrounds a
    server cannot leak it past the deadline. A string ``cmd`` runs through
    the shell exactly as before; a list does not.

    ``extra_env`` carries values KSTRL ITSELF CHOSE for one command,
    layered on top of the scrub, never values inherited from the
    operator's environment - so the scrub's guarantee (no secret reaches
    a verification subprocess) is unchanged.
    ``tests/test_patch_coverage.py::test_extra_env_does_not_reopen_the_scrub``
    is what holds that. Its only caller today is the patch-coverage check
    (:func:`_coverage_report`, via :func:`check_patch_coverage`), which
    points ``COVERAGE_FILE`` at a throwaway directory so pytest-cov's
    data file cannot land in the tree being measured; see that function
    for the alternative (``--cov-config``) this rejects and why.

    Raises :class:`subprocess.TimeoutExpired` after the group is dead so
    existing callers' timeout handling keeps working unchanged, and
    :class:`ChildOutputDecodeError` when the child's bytes are not valid
    utf-8 - every one of this function's 18 call sites answers for it
    exactly as it answers for a timeout (#416).

    THE TIMEOUT PATH LETS GO THROUGH ``procdispose`` (#326). It used to
    drain the pipes itself and, when that drain expired, set
    ``stdout, stderr = "", ""`` and drop the child on the floor: no
    close of the two pipe ends, and no register, so the only thing left
    holding the pid was ``Popen.__del__``. That is not a fallback under
    ``PYTHONWARNINGS=error``, which is a setting this codebase already
    records crashing a daemon: ``__del__`` calls ``_warn`` BEFORE
    ``_active.append`` (CPython 3.12.8 ``subprocess.py`` lines 1139 and
    1145), the warn raises, ``__del__`` aborts, and the child stays a
    zombie for the life of the process. This is the widest window of the
    three sites that had it - it needs no D-state child, only something
    outside the group holding a pipe write end, which a forked
    grandchild does routinely, and it runs once per verification command
    per iteration rather than once per timed-out run.
    """
    env = scrubbed_subprocess_env()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        cmd,
        shell=isinstance(cmd, str),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=term_grace)
        except subprocess.TimeoutExpired:
            pass
        # SIGKILL the group even when the direct child honored SIGTERM: a
        # grandchild that ignored it can hold the pipes open and would
        # otherwise block the drain below indefinitely.
        _signal_process_group(proc, signal.SIGKILL)
        stdout, stderr = drain_or_abandon(proc, term_grace)
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from None
    except UnicodeDecodeError as exc:
        # The child ran and has already been waited on: CPython's own
        # `_communicate` waits before it decodes, so `proc` is reaped and
        # both pipes are at EOF here (measured, #416's altitude review -
        # instrumented run: `poll() == 0` on entry, `drain_or_abandon`
        # returns ("", "")). The call stays anyway, because this module's
        # disposal rule is uniform across every non-completed-read exit
        # (#326) rather than reasoned per site, and re-deriving "this one
        # needs no disposal" per exit is exactly the per-site reasoning
        # that rule exists to remove. Then the named error, so 18 call
        # sites can answer for it (#416).
        drain_or_abandon(proc, term_grace)
        raise ChildOutputDecodeError(
            f"the command produced bytes that are not valid utf-8, so its "
            f"output could not be read: {exc}"
        ) from exc
    except BaseException:
        # The rule `procgroup._read_ps` already states and this module
        # did not: every exit that is not a completed read leaves a
        # child behind, so every one of them goes through the same
        # disposal. Catching only `TimeoutExpired` made this the widest
        # remaining hole of the #326 class rather than a fixed site - a
        # KeyboardInterrupt out of `ks verify`, or a MemoryError on a
        # capture big enough to matter, left the child unsignalled,
        # unreaped, unregistered and holding both pipe ends, on the
        # highest-frequency spawn in the factory.
        drain_or_abandon(proc, term_grace)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


@dataclass
class CheckResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    parsed: ParsedOutput | None = None
    # R8.1: typed findings this mechanical check produced, lifted into the
    # component's finding stream by the pipeline so a machine-made gate
    # decision lands in the audit trail (PR body, journal) and not only in
    # the retry context. Empty for checks that emit prose only.
    findings: list[Finding] = field(default_factory=list)
    # #227: whether this row is a MEASUREMENT. False when the check ran and
    # measured nothing anyway - it timed out, or its detector is not
    # installed. Those still produce a row, so :class:`NotMeasured` cannot
    # carry them: the sidecar is for checks that produce NO row, and turning
    # one of these into a gap would make `result.passed` true on a timeout.
    #
    # Read by :mod:`kstrl.dampener` and nothing else today. It changes no
    # existing behaviour and no published surface: `passed` still decides the
    # verdict, the report table and the `ks sense --json` check objects are
    # untouched. What it buys is that a signature's disappearance can be told
    # apart from the sensor's, which a fallback signature cannot say for
    # itself: `signature_slug` strips digits, so "timed out after 300.0s" and
    # "timed out after 1800.0s" are the same string.
    measured: bool = True


#: Why a check that was ASKED FOR produced no measurement. Stable
#: tokens: they reach `ks sense --json` and `events.jsonl`, so a reader
#: keys on these and not on the prose beside them.
#:
#: A check the operator did not ask for - ``[verify] mutation_testing``
#: left false - records nothing at all. That is the line the sidecar
#: draws, and it is what keeps the default quiet: silence is a complete
#: answer to a question nobody asked, and an incomplete one to a
#: question they did.
NOT_MEASURED_READ_ONLY = "read_only"
NOT_MEASURED_TOOL_MISSING = "tool_missing"
NOT_MEASURED_NO_TARGET = "no_target"
NOT_MEASURED_TIMED_OUT = "timed_out"
NOT_MEASURED_COMMAND_FAILED = "command_failed"
NOT_MEASURED_NO_MUTANTS = "no_mutants"


@dataclass(frozen=True)
class NotMeasured:
    """A check that was asked for and produced no measurement (#306).

    The SIDECAR. Deliberately not a :class:`CheckResult`: it never
    reaches ``checks``, so ``all(c.passed ...)``, ``report_lines``'
    verdict column, ``ks sense --json``'s ``checks`` array and
    :func:`kstrl.review.build_review_prompt` cannot read it as a pass -
    which is the whole of #306. Equally it never reaches
    :meth:`VerificationResult.as_context`, so it is not retry context:
    no engineer iteration is spent on a missing binary it cannot
    install.

    What it buys back is the diagnostic the omission alone destroyed.
    Absence from ``checks`` is honest but mute, and SEVEN states produce
    that absence, which an operator who set ``mutation_testing = true``
    cannot otherwise tell apart from a working gate. Six of them carry
    one of these records and are separated by ``reason``, one of the
    ``NOT_MEASURED_*`` constants above. The seventh, the check being
    turned off, records nothing at all, on purpose: a question nobody
    asked needs no answer.

    ``detail`` is prose for a human and is never parsed.
    """

    check: str
    reason: str
    detail: str

    def as_line(self) -> str:
        """The report-table rendering, for :meth:`VerificationResult.report_lines`.

        Not every terminal surface: the factory's Phase 1 warning
        prefixes the component id and names the reason, because it is
        one line inside a multi-component run rather than a row under a
        table that already says which component it is.
        """
        return f"  {self.check}  not measured  {self.detail}"

    def as_token(self) -> str:
        """The ``events.jsonl`` rendering: ``"<check>:<reason>"``.

        Here rather than in the emitters because there are two of them,
        in :mod:`kstrl.pipeline` and :mod:`kstrl.feature_verify`, and a
        format spelled twice is one an edit can change in one place
        only. Same ``<check>:<code>`` shape
        :func:`kstrl.evolution.split_signature` already reads, so a
        consumer of that file meets one convention rather than two.
        """
        return f"{self.check}:{self.reason}"

    def to_dict(self) -> dict[str, str]:
        """The ``ks sense --json`` rendering."""
        return {"check": self.check, "reason": self.reason, "detail": self.detail}


def _capped_detail_lines(check: CheckResult, limit: int | None) -> list[str]:
    """``check``'s details, indented, truncated to ``limit`` if given.

    The truncation is never silent: what is dropped is counted on a final
    line, because a report that quietly shows you 12 of 400 failures is a
    report you would act on wrongly.
    """
    lines = [f"      {line}" for detail in check.details for line in detail.splitlines()]
    if limit is None or len(lines) <= limit:
        return lines
    return [*lines[:limit], f"      ... {len(lines) - limit} more line(s) not shown"]


@dataclass
class VerificationResult:
    """Aggregated result of all mechanical checks."""

    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    #: Checks that were asked for and measured nothing (#306). The
    #: sidecar: see :class:`NotMeasured` for why it is beside ``checks``
    #: rather than in it.
    not_measured: list[NotMeasured] = field(default_factory=list)

    def as_context(self) -> str:
        """Format failures for injection into retry prompt."""
        lines: list[str] = []
        for check in self.checks:
            if not check.passed:
                lines.append(f"- {check.name}: FAIL - {check.message}")
                for detail in check.details[:10]:
                    lines.append(f"  {detail}")
        return "\n".join(lines)

    def report_lines(
        self,
        *,
        durations: bool = True,
        max_detail_lines: int | None = None,
    ) -> list[str]:
        """One line per check, then the indented details of each failure.

        The TERMINAL rendering of this object, in one place: ``ks sense``
        and ``ks feature``'s #288 report print the same table, and before
        this existed they printed it from two copies of the same
        f-string.

        ``durations=False`` drops the wall-clock column. `ks feature`
        needs that: its narration sits inside a longer flow that
        ``tests/test_feature_run.py`` compares BYTE FOR BYTE between a
        recorded and an unrecorded run, and a timing makes two runs of
        the same work disagree. The figure is not lost there - it goes on
        the ``VerificationResultEvent``.

        ``max_detail_lines`` caps the details PER CHECK and appends a
        line saying how many were dropped, so a truncation is never
        silent. `ks feature` needs that too, and for a different reason:
        under the embedded TUI every one of these lines becomes a
        ``Log`` event on the run bus, and a failing gate's details are
        ``ParsedOutput.format_for_prompt`` - every parsed failure with a
        source-context snippet. A 40-failure suite is hundreds of events
        per report, up to ``2 + repair_max_runs`` times a run, which is
        the event-stream flood ``commandrun._StreamFilterSink`` exists to
        prevent. ``as_context`` already truncates at 10 for the same
        reason. None (the default, and ``ks sense``) prints everything:
        there the measurement IS the whole output.
        """
        width = max((len(check.name) for check in self.checks), default=0)
        lines: list[str] = []
        for check in self.checks:
            verdict = "pass" if check.passed else "FAIL"
            timing = f"  ({check.duration_seconds:.2f}s)" if durations else ""
            lines.append(f"  {check.name.ljust(width)}  {verdict}  {check.message}{timing}")
            if check.passed:
                continue
            lines.extend(_capped_detail_lines(check, max_detail_lines))
        # The sidecar, below the table and outside it (#306). Rendered
        # here rather than by each caller for the reason the table is:
        # `ks sense` and `ks feature` must not be able to disagree about
        # whether they mention what was not measured. No verdict column
        # and no duration - there is no verdict, and nothing was timed.
        lines.extend(gap.as_line() for gap in self.not_measured)
        return lines


def _optional_str(value: object) -> str | None:
    """A toml scalar as a string, with the empty string meaning unset."""
    return str(value) or None


#: Every live ``[verify]`` toml key and how its value is coerced onto the
#: dataclass. A table rather than a per-key ``if``: the chain it replaced
#: was fourteen near-identical branches, and its cyclomatic complexity
#: was already twice the repo's ratchet limit before #258 added three
#: more keys to it. Order is the dataclass's, so a reader can diff the
#: two lists by eye. Anything absent from this table is not a toml key.
_VERIFY_TOML_FIELDS: tuple[tuple[str, Callable[[Any], object]], ...] = (
    ("test_command", _optional_str),
    ("typecheck_command", _optional_str),
    ("lint_command", _optional_str),
    # validate_tool already maps the empty string to None (auto) and
    # raises on anything it does not recognise, so it needs no coercion
    # in front of it.
    ("test_tool", partial(validate_tool, GATE_TEST)),
    ("typecheck_tool", partial(validate_tool, GATE_TYPECHECK)),
    ("lint_tool", partial(validate_tool, GATE_LINT)),
    ("check_diff_scope", bool),
    ("check_bad_patterns", bool),
    ("dead_code_cleanup", bool),
    ("dead_code_command", _optional_str),
    ("mutation_testing", bool),
    ("mutation_threshold", float),
    ("mutation_timeout", float),
    ("subprocess_timeout", float),
    ("require_self_critique", bool),
    ("self_critique_min_bullets", int),
    ("progress_file_path", _optional_str),
)


@dataclass
class VerifyConfig:
    """Configuration for mechanical verification."""

    test_command: str | None = None
    typecheck_command: str | None = None
    lint_command: str | None = None
    # Which parser reads each gate's output (#258). None is auto: every
    # parser registered for the gate runs and their findings are unioned,
    # which is what makes a chained command
    # (`uv run pytest && npm run test`) yield BOTH toolchains' failures.
    # Set one to pin the gate to a single parser. Accepted values are
    # kstrl.gateparse.GATE_TOOLS[<gate>]; anything else raises on load.
    test_tool: str | None = None
    typecheck_tool: str | None = None
    lint_tool: str | None = None
    check_diff_scope: bool = True
    check_bad_patterns: bool = True
    dead_code_cleanup: bool = False
    dead_code_command: str | None = None
    mutation_testing: bool = False
    mutation_threshold: float = 50.0
    mutation_timeout: float = 600.0
    subprocess_timeout: float = 300.0
    # Mechanical enforcement of the engineer prompt's "## Self-Critique"
    # mandate. Off by default to keep this opt-in; set to True (or
    # KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE=1) to fail Phase 1 when an
    # iteration's progress.txt entry omits the block.
    require_self_critique: bool = False
    self_critique_min_bullets: int = 3
    # Where check_self_critique looks for the engineer's progress log.
    # None (the default) derives it from the component's PRD
    # (config.component_progress_path), which is where the engineer was
    # actually told to write and the only location inside the
    # component's allowedPaths. An explicit value wins for every
    # component. It is None-defaulted rather than carrying a separate
    # "was it set?" flag because every scalar field of this dataclass is
    # a documented kstrl.toml key (scripts/gen_docs.py probes for that).
    progress_file_path: str | None = None

    @classmethod
    def from_env(cls) -> VerifyConfig:
        """Load verify config from environment variables."""
        return cls(
            test_command=os.environ.get("KSTRL_VERIFY_TEST_CMD"),
            typecheck_command=os.environ.get("KSTRL_VERIFY_TYPECHECK_CMD"),
            lint_command=os.environ.get("KSTRL_VERIFY_LINT_CMD"),
            test_tool=validate_tool(GATE_TEST, os.environ.get("KSTRL_VERIFY_TEST_TOOL")),
            typecheck_tool=validate_tool(
                GATE_TYPECHECK, os.environ.get("KSTRL_VERIFY_TYPECHECK_TOOL")
            ),
            lint_tool=validate_tool(GATE_LINT, os.environ.get("KSTRL_VERIFY_LINT_TOOL")),
            dead_code_cleanup=os.environ.get("KSTRL_DEAD_CODE_CLEANUP", "") == "1",
            dead_code_command=os.environ.get("KSTRL_DEAD_CODE_CMD"),
            mutation_testing=os.environ.get("KSTRL_MUTATION_TESTING", "") == "1",
            mutation_threshold=float(os.environ.get("KSTRL_MUTATION_THRESHOLD", "50")),
            mutation_timeout=float(os.environ.get("KSTRL_MUTATION_TIMEOUT", "600")),
            subprocess_timeout=float(os.environ.get("KSTRL_TIMEOUT_VERIFY", "300")),
            require_self_critique=os.environ.get("KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE", "") == "1",
            self_critique_min_bullets=int(
                os.environ.get("KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS", "3"),
            ),
            progress_file_path=os.environ.get("KSTRL_VERIFY_PROGRESS_FILE"),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> VerifyConfig:
        """Load verify config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "verify")
        for key, coerce in _VERIFY_TOML_FIELDS:
            if key in section:
                setattr(config, key, coerce(section[key]))
        # Env overrides. Each var is applied only when it is explicitly
        # set in the environment: the previous compare-against-default
        # heuristic silently dropped an env value that happened to equal
        # the dataclass default (e.g. KSTRL_MUTATION_THRESHOLD=50 could
        # not override a toml mutation_threshold), breaking the
        # env-beats-toml precedence contract (R2.1).
        env = cls.from_env()
        env_var_to_field = {
            "KSTRL_VERIFY_TEST_CMD": "test_command",
            "KSTRL_VERIFY_TYPECHECK_CMD": "typecheck_command",
            "KSTRL_VERIFY_LINT_CMD": "lint_command",
            "KSTRL_VERIFY_TEST_TOOL": "test_tool",
            "KSTRL_VERIFY_TYPECHECK_TOOL": "typecheck_tool",
            "KSTRL_VERIFY_LINT_TOOL": "lint_tool",
            "KSTRL_DEAD_CODE_CLEANUP": "dead_code_cleanup",
            "KSTRL_DEAD_CODE_CMD": "dead_code_command",
            "KSTRL_MUTATION_TESTING": "mutation_testing",
            "KSTRL_MUTATION_THRESHOLD": "mutation_threshold",
            "KSTRL_MUTATION_TIMEOUT": "mutation_timeout",
            "KSTRL_TIMEOUT_VERIFY": "subprocess_timeout",
            "KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE": "require_self_critique",
            "KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS": "self_critique_min_bullets",
            "KSTRL_VERIFY_PROGRESS_FILE": "progress_file_path",
        }
        for env_var, field_name in env_var_to_field.items():
            if env_var in os.environ:
                setattr(config, field_name, getattr(env, field_name))
        return config


# Engineer prompt mandates the EXACT heading `## Self-Critique`.
# Accept also `- **Self-Critique:**` (common bullet-in-list form) and
# `## Self Critique` (loose hyphen-space variant). Reject prose like
# "the self-critique above" so we don't false-positive on body text.
# Both forms must START the line after at most a list marker + whitespace.
_SELF_CRITIQUE_HEADING_RE = re.compile(
    r"""^
    (?:
        \#{2,3}\s+                  # H2 / H3: '## ' or '### '
      | [\-*]\s+\*{2}\s*            # '- **' or '* **'
    )
    Self[-\s]Critique
    (?:
        \s*\*{2}                    # '**' (close bold)
      | \s*:                        # ':'
      | \s*\*{2}\s*:                # '**:'
      | \s*$                        # end-of-line
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An iteration entry boundary in progress.txt. The engineer prompt's
# documented format starts each appended entry with
# `## [YYYY-MM-DD] - [Story ID]`; agents also commonly write
# `## Iteration N`. Exactly two hashes: H3 sub-headings inside an
# entry must not be mistaken for a new entry.
_ITERATION_HEADING_RE = re.compile(
    r"""^\#\#\s+
    (?:
        \[?\d{4}-\d{2}-\d{2}        # '## [YYYY-MM-DD] - ...' (documented form)
      | Iteration\b                 # '## Iteration N' (loose variant)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An UNINDENTED bullet opening with a closed bold label, e.g.
# `- **Learnings:**` or `- **Interpretations** (only if ...): ...`.
# In the engineer prompt's entry format these are sibling sections of
# `- **Self-Critique:**`, so one of them terminates the bullet count.
# Applied to the raw line: the Self-Critique block's own nested bullets
# are indented and therefore never match.
_SECTION_BULLET_RE = re.compile(r"^[\-*]\s+\*{2}[^*]+\*{2}")

# Thematic break: the engineer prompt's entry format ends each entry
# with `---`.
_ENTRY_SEPARATOR_RE = re.compile(r"^-{3,}$")


def _self_critique_text(progress_path: Path, start: float) -> str | CheckResult:
    """The progress file's text, or the failing check explaining why not.

    Two handlers because the remedies differ: "could not read" sends the
    operator to the file's permissions, and this file opened fine - the
    agent wrote bytes that are not UTF-8, which is a fact about the
    agent's output rather than about the disk. Before #320 the decode was
    not caught at all and a Phase 1 gate died with a traceback instead of
    reporting red.

    Split out of :func:`check_self_critique` so the second handler does
    not push that function past the complexity ratchets.
    """
    try:
        return progress_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Could not read progress file: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except UnicodeDecodeError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Progress file is not valid UTF-8: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )


def check_self_critique(
    progress_path: Path,
    min_bullets: int = 3,
) -> CheckResult:
    """Confirm the CURRENT (latest) progress.txt entry contains a
    Self-Critique block with at least ``min_bullets`` bullet points.

    Shape check only (H4): this verifies that a Self-Critique block of
    the right shape exists in the right place. It does NOT verify the
    substance of the bullets - vacuous-but-plausible failure modes
    pass. Substance is the reviewer's job.

    Format assumption (from the engineer prompt's Progress Format):
    each iteration appends an entry starting with an H2 heading of the
    form `## [YYYY-MM-DD] - [Story ID]` (the loose `## Iteration N`
    variant is also recognized), containing `- **Self-Critique:**` (or
    `## Self-Critique`) followed by bullets, sibling bold-label
    sections such as `- **Interpretations:**`, and a closing `---`.

    The check first locates the latest iteration boundary (the LAST
    line matching ``_ITERATION_HEADING_RE``), then requires a
    Self-Critique heading within that entry - a block written by an
    EARLIER iteration does not satisfy the check for the current one.
    If no iteration heading exists anywhere, the whole file is treated
    as a single entry (fallback for free-form progress files; per-
    iteration association is not possible there).

    Bullet counting stops at the next `##` heading, a `---` entry
    separator, or an unindented bold-label bullet (a sibling section
    like `- **Interpretations:**`), so bullets belonging to later
    sections do not inflate the count. Consequence of the format
    assumption: critique bullets themselves must either be indented
    under the `- **Self-Critique:**` bullet (the documented format) or
    not open with a bold label, otherwise they read as a sibling
    section and the check fails loudly rather than over-counting.

    Without this mechanical check, the engineer prompt's mandate to
    list >=3 failure modes can silently rot - the only enforcement
    path otherwise is the reviewer noticing, which is unreliable.
    """
    start = time.monotonic()
    text = _self_critique_text(progress_path, start)
    if isinstance(text, CheckResult):
        return text

    lines = text.splitlines()
    # Locate the latest iteration entry: entries are appended, so the
    # LAST iteration heading starts the current iteration's entry.
    entry_start = 0
    entry_found = False
    for i in range(len(lines) - 1, -1, -1):
        if _ITERATION_HEADING_RE.match(lines[i]):
            entry_start = i
            entry_found = True
            break

    # Find the LAST self-critique heading WITHIN the latest entry, so
    # an earlier iteration's block cannot satisfy the current one and
    # repeated blocks inside one entry resolve to the newest.
    heading_idx: int | None = None
    for i in range(len(lines) - 1, entry_start - 1, -1):
        if _SELF_CRITIQUE_HEADING_RE.match(lines[i]):
            heading_idx = i
            break

    if heading_idx is None:
        where = (
            f"in the latest iteration entry (line {entry_start + 1}: "
            f"{lines[entry_start].strip()[:60]!r})"
            if entry_found
            else "in progress file"
        )
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"No '## Self-Critique' block found {where}. "
                "Engineer prompt mandates >=3 failure-mode bullets "
                "before declaring done."
            ),
            duration_seconds=time.monotonic() - start,
        )

    # Count bullets after the heading until the entry's content ends:
    # next `##` heading, `---` separator, or a sibling bold-label
    # bullet section (e.g. `- **Interpretations:**`).
    bullet_count = 0
    bullet_lines: list[str] = []
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        # Stop at next major heading
        if stripped.startswith("##"):
            break
        # Stop at the entry separator
        if _ENTRY_SEPARATOR_RE.match(stripped):
            break
        # Stop at the next sibling section: an UNINDENTED bold-label
        # bullet (matched on the raw line so the block's own indented
        # bullets never terminate the count).
        if _SECTION_BULLET_RE.match(line):
            break
        # Count substantive bullets (require non-trivial content after the marker)
        if stripped.startswith("- ") or stripped.startswith("* "):
            body = stripped[2:].strip()
            if body and not body.lower().startswith(("tbd", "todo", "n/a")):
                bullet_count += 1
                bullet_lines.append(body[:80])

    if bullet_count < min_bullets:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"Self-Critique block has {bullet_count} bullets; minimum required is {min_bullets}"
            ),
            details=bullet_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="self_critique",
        passed=True,
        message=f"{bullet_count} failure modes listed",
        duration_seconds=time.monotonic() - start,
    )


def _tamper_changes(prd: PRD, pre_run_prd_path: Path | None) -> list[str]:
    """How ``prd`` differs from the pre-run copy in ways no engineer may.

    Defence in depth for #264's carve-out, kept deliberately after #269
    made the SCOPE half of this comparison unnecessary. The plan-time
    snapshot (``kstrl.scope``) settles what a component may write, so an
    ``allowedPaths`` the agent edits is inert and is no longer compared:
    see that module for why comparing a value the agent can rewrite is
    the weaker answer.

    What the snapshot does NOT cover is everything else that reads this
    file, and a lot does: ``check_prd_stories`` below, the approved
    fixtures oracle, the acceptance criteria handed to the reviewer, the
    R10.3 set-point sensor. None can be served from a snapshot, because
    the agent setting ``passes`` is the whole job, so the live file has
    to be trusted and a comparison is the only answer available for it.
    Drop this and an agent can delete an acceptance criterion or neuter
    an executable oracle and pass a gate it authored.

    WHICH fields are the engineer's to write is the PRD's own business
    and lives in ``PRD.tamper_changes``, beside the fields it pins.

    Empty when there is nothing to compare, which is not the same as
    waving something through:

    - ``pre_run_prd_path`` is None. The caller has no trustworthy copy
      to offer: ``ks sense`` judges an operator's own working tree.
    - The pre-run copy will not load. A harness or operator condition,
      not something an agent can arrange from inside its worktree.
    - It is the SAME file, which is ``use_worktrees=False``: both reads
      return the same document and the comparison is empty by
      arithmetic rather than by a special case. That mode has no
      isolation boundary, so this check cannot be what gives it one.
      Scope is the part of the answer that does survive there, because
      #269 reads it before the agent starts.
    """
    if pre_run_prd_path is None:
        return []
    try:
        pre_run = PRD.load(pre_run_prd_path)
    except (OSError, ValueError):
        return []
    return prd.tamper_changes(pre_run)


#: H3 (#303): fragments check_prd_stories' tamper branch assembles;
#: versioned as one body (docs/adversarial-roadmap.md, H3a sweep row).
PRD_TAMPER_PROMPT_VERSION = "1.0.0"

PRD_TAMPER_FIELDS_PROMPT = (
    "It {changes}. A component may set `passes` "
    "and `notes` on its own stories and nothing else: it may "
    "not rewrite the criteria or the fixtures it is judged "
    "against."
)
PRD_TAMPER_GATES_PROMPT = (
    "Every gate that reads this file - these stories, the "
    "approved fixtures, the criteria the reviewer is given - "
    "is judging a document the component rewrote. Restore it "
    "to what the run started with; do not treat this as "
    "permission to change what the component is measured "
    "against."
)


def check_prd_stories(prd_path: Path, pre_run_prd_path: Path | None = None) -> CheckResult:
    """Re-read PRD from disk and verify all stories have passes=true.

    ``pre_run_prd_path`` (#269) is the copy of the same PRD the run
    started with, which lives outside every worktree and so is not
    agent-writable. Given one, this check also refuses a PRD the
    component rewrote in its own favour (``_tamper_changes``).

    This is the check that carries that refusal, rather than
    ``diff_scope``, for two reasons. It is a statement about the
    STORIES, which is what this check reads and what a rewrite attacks;
    scope stopped being the question when #269 made the plan-time
    snapshot the only scope source. And ``diff_scope`` is switchable off
    (``[verify] check_diff_scope``), while this one runs whenever there
    is a PRD at all: defence in depth an unrelated toggle can disable is
    not defence in depth.
    """
    start = time.monotonic()
    try:
        prd = PRD.load(prd_path)
    except Exception as exc:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"Failed to load PRD: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    tampered = _tamper_changes(prd, pre_run_prd_path)
    if tampered:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message="The PRD is not the one this run started with; failing closed",
            details=[
                PRD_TAMPER_FIELDS_PROMPT.format(changes="; ".join(tampered)),
                PRD_TAMPER_GATES_PROMPT,
            ],
            duration_seconds=time.monotonic() - start,
        )

    failing = [s for s in prd.user_stories if not s.passes]
    if failing:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"{len(failing)} stories not marked as passing",
            details=[f"{s.id}: {s.title}" for s in failing],
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="prd_stories",
        passed=True,
        message=f"All {len(prd.user_stories)} stories passing",
        duration_seconds=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# Resolved verification commands (#261)
# ---------------------------------------------------------------------------
#
# The single source of truth for "what will Phase 1 actually run". Both
# the gate (``check_test_suite`` / ``check_typecheck`` / ``check_linter``)
# and the engineer prompt (``loop.run_loop``) answer that question by
# calling the resolvers below, so the agent cannot be told a command the
# gate will not run.
#
# ``ks init`` used to scaffold a second, hardcoded copy of these commands
# into the generated CLAUDE.md. Every copy disagreed with the gate from
# the moment init finished, and loop.run_loop prepends CLAUDE.md into the
# engineer prompt, so the harness mechanically fed the agent the wrong
# commands. The copy is gone; this module is the only source.

#: Gate default when ``[verify] test_command`` is unset.
DEFAULT_TEST_COMMAND = "uv run pytest"

#: Gate default when ``[verify] lint_command`` is unset.
DEFAULT_LINT_COMMAND = "uv run ruff check ."

#: Gate fallback when ``[verify] typecheck_command`` is unset AND the
#: project does not scope mypy itself. ``_default_typecheck_command``
#: prefers ``uv run mypy`` (no path) whenever pyproject.toml does.
DEFAULT_TYPECHECK_COMMAND = "uv run mypy ."

#: What ``_default_typecheck_command`` uses instead when the project has
#: scoped mypy via ``[tool.mypy] files`` or ``packages``.
SCOPED_TYPECHECK_COMMAND = "uv run mypy"

# Harness-authored instruction text injected into the engineer prompt on
# every iteration, so it is enrolled in the H3 version/hash snapshot
# (tests/test_prompt_versions.py) exactly like DEFAULT_PROMPT. Only the
# TEMPLATE is snapshotted: the three command values are the operator's,
# interpolated at run time, and H3 cannot and should not pin those.
VERIFY_COMMANDS_PROMPT_VERSION = "1.0.0"

VERIFY_COMMANDS_PROMPT = """\
# Verification Commands (resolved by kstrl)

These are the exact commands kstrl's mechanical verification gate runs on your
work, resolved from this project's `kstrl.toml` `[verify]` section. Run them
yourself before you report a story complete. They are authoritative: ignore any
other verification command list, including one written in the project context
above.

- Test: `{test}`
- Typecheck: `{typecheck}`
- Lint: `{lint}`

A command may chain several toolchains. Run all of it."""


def _default_typecheck_command(cwd: Path) -> str:
    """Choose a sensible default mypy invocation for ``cwd``.

    Generic ``uv run mypy .`` is hostile to projects whose pyproject.toml
    deliberately scopes mypy via ``[tool.mypy] files`` or ``packages``:
    the ``.`` argument overrides those settings and pulls in test files
    or vendored code that the project never intended to typecheck. When
    the project has configured its own mypy scope, defer to it by
    invoking ``uv run mypy`` with no path argument (mypy then reads the
    config). When no such config is present, fall back to the broad
    ``uv run mypy .`` so a green-field project still gets coverage.

    This is the Gap 2 fix from the end-to-end factory validation run:
    the factory's verify command was overriding the project's own
    typecheck scope, leading to Phase 1 failures on diffs that were
    actually fine. Gap 2 landed on the gate and not on ``ks init``, which
    kept scaffolding ``mypy src/ --strict`` into CLAUDE.md - the very
    shape it identified as wrong. #261 closed that half.
    """
    import tomllib

    pyproject = cwd / "pyproject.toml"
    if pyproject.is_file():
        # The read is outside the guard for the same reason it is in
        # ``config.load_toml_document``: an I/O fault is not a parse
        # fault. Here it makes no difference to the caller, since both
        # end at the same default, but a rule applied at one of two
        # sites and not the other is a rule the next author has to guess
        # at.
        try:
            raw = pyproject.read_bytes()
        except OSError:
            return DEFAULT_TYPECHECK_COMMAND
        try:
            data = tomllib.loads(raw.decode())
        except Exception:
            # ``Exception``, not an enumeration of what tomllib is
            # believed to raise: see ``kstrl.config.load_toml_document``
            # for the argument and ``tests/test_toml_readers.py`` for
            # the guard. The one fact local to THIS site is that a
            # pyproject.toml is not the operator's kstrl.toml, so it
            # fails to a documented default rather than to an error,
            # which is why catching the whole class costs nothing here.
            return DEFAULT_TYPECHECK_COMMAND
        mypy_section = data.get("tool", {}).get("mypy", {})
        if isinstance(mypy_section, dict):
            # Acknowledged edge case: this heuristic does not consult
            # ``[[tool.mypy.overrides]]`` (per-module relaxation) or
            # modules-only configs. If a project relaxes via overrides
            # but doesn't set ``files``/``packages``, the broad
            # ``uv run mypy .`` default would override the relaxation.
            # Real-world rare. Users can always override explicitly via
            # ``--typecheck-command`` or env var.
            if mypy_section.get("files") or mypy_section.get("packages"):
                return SCOPED_TYPECHECK_COMMAND
    return DEFAULT_TYPECHECK_COMMAND


def resolve_test_command(command: str | None) -> str:
    """The exact test command Phase 1 will run."""
    return command or DEFAULT_TEST_COMMAND


def resolve_typecheck_command(command: str | None, cwd: Path) -> str:
    """The exact typecheck command Phase 1 will run in ``cwd``."""
    return command or _default_typecheck_command(cwd)


def resolve_lint_command(command: str | None) -> str:
    """The exact lint command Phase 1 will run."""
    return command or DEFAULT_LINT_COMMAND


@dataclass(frozen=True)
class ResolvedVerifyCommands:
    """The concrete commands Phase 1 runs, after config and defaults.

    Every field is a shell command line, so a chained polyglot command
    (``uv run pytest -q && cd web && npm run test``) survives verbatim:
    the resolver never splits or rewrites what the operator configured.
    """

    test: str
    typecheck: str
    lint: str

    def format_for_prompt(self) -> str:
        """Render the block injected into the engineer prompt.

        Stated as authoritative because an agent working in a project
        scaffolded before #261 may also be shown a stale CLAUDE.md list,
        and has to know which one binds.
        """
        return VERIFY_COMMANDS_PROMPT.format(
            test=self.test,
            typecheck=self.typecheck,
            lint=self.lint,
        )


def pin_verify_commands(config: VerifyConfig, cwd: Path) -> VerifyConfig:
    """A copy of ``config`` whose three command fields are already resolved.

    Resolution is not a pure function of the config: ``resolve_typecheck_command``
    falls back to ``_default_typecheck_command(cwd)``, which re-reads
    ``cwd/pyproject.toml`` and answers ``uv run mypy`` when
    ``[tool.mypy] files`` or ``packages`` is present and ``uv run mypy .``
    when it is not. Adding a mypy scope is an ordinary engineer story, so
    a caller that resolves more than once during a run can get two
    different commands from one config (#288 review round 2).

    Pinning is what makes every later resolution the identity: once the
    fields are non-None, ``resolve_*_command`` returns them unchanged. So
    the report's announcement, the command that actually runs, and the
    ``VERIFY_COMMANDS_PROMPT`` block ``build_project_context`` renders
    for the engineer are provably one string per command for the whole
    run, rather than three independent reads that agree by luck.

    Do NOT use this on the factory's per-component path without thinking:
    there each component has its own worktree, and the right pyproject to
    resolve against is that worktree's, not the caller's ``cwd``.
    """
    resolved = resolve_verify_commands(config, cwd)
    return replace(
        config,
        test_command=resolved.test,
        typecheck_command=resolved.typecheck,
        lint_command=resolved.lint,
    )


def resolve_verify_commands(config: VerifyConfig, cwd: Path) -> ResolvedVerifyCommands:
    """Resolve ``config`` against ``cwd`` into the commands Phase 1 runs.

    ``cwd`` is the directory the gate will run in (the component's
    worktree under the factory), because the typecheck default is a
    function of that directory's pyproject.toml.
    """
    return ResolvedVerifyCommands(
        test=resolve_test_command(config.test_command),
        typecheck=resolve_typecheck_command(config.typecheck_command, cwd),
        lint=resolve_lint_command(config.lint_command),
    )


# A CLAUDE.md verification bullet in the shape ``ks init`` used to
# generate: ``- **Test**: `uv run pytest tests/ -v --tb=short```. Matched
# anywhere in the file rather than under a specific heading, because the
# heading text varies ("## Verification Commands", "## Verification
# commands") while the bullet shape does not.
_CLAUDE_MD_COMMAND_RE = re.compile(
    r"^\s*[-*]\s+\*{2}(Test|Typecheck|Lint)\*{2}\s*:\s*`([^`]+)`\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScrubbedProjectContext:
    """CLAUDE.md text with stale verification bullets removed (#261)."""

    text: str
    #: One human-readable line per removed bullet, for ``ui.warn``.
    divergences: list[str]


def scrub_stale_verify_commands(
    claude_md: str,
    commands: ResolvedVerifyCommands,
) -> ScrubbedProjectContext:
    """Drop CLAUDE.md verification bullets that disagree with the gate.

    Projects scaffolded before #261 carry a generated ``## Verification
    Commands`` section whose three bullets disagree with what the gate
    runs. ``loop.run_loop`` prepends CLAUDE.md into the engineer prompt,
    so those bullets are instructions the agent follows and then fails
    Phase 1 on.

    Removal is per-bullet and only when the stated command differs from
    the resolved one, so a project whose CLAUDE.md happens to be correct
    is left byte-identical, and surrounding prose always survives. The
    file on disk is never modified: this scrubs the in-memory copy that
    goes into the prompt, and every removal is reported so the operator
    can delete the stale section for good.
    """
    kept: list[str] = []
    divergences: list[str] = []
    by_label = {
        "test": commands.test,
        "typecheck": commands.typecheck,
        "lint": commands.lint,
    }
    # keepends: what survives is re-joined with "", so a file with CRLF
    # endings or no trailing newline round-trips byte for byte.
    for line in claude_md.splitlines(keepends=True):
        match = _CLAUDE_MD_COMMAND_RE.match(line)
        if match is None:
            kept.append(line)
            continue
        label = match.group(1).lower()
        stated = match.group(2).strip()
        resolved = by_label[label]
        if stated == resolved:
            kept.append(line)
            continue
        divergences.append(
            f"CLAUDE.md tells the agent to {label} with `{stated}`, but the "
            f"gate runs `{resolved}`. Dropping the stale line from the "
            f"engineer prompt; delete it from CLAUDE.md and set [verify] in "
            f"kstrl.toml instead."
        )
    return ScrubbedProjectContext(text="".join(kept), divergences=divergences)


def scrub_project_claude_md(
    root: Path,
    commands: ResolvedVerifyCommands,
) -> ScrubbedProjectContext | None:
    """``scrub_stale_verify_commands`` on ``root``'s CLAUDE.md, or None.

    None when the project has no readable CLAUDE.md. One place decides
    where the file lives and what an unreadable one means, because two
    callers need the same answer for different reasons: the engineer
    loop wants ``.text`` (the copy that goes into the prompt) and the
    factory preflight wants ``.divergences`` (what to tell the
    operator), and they had drifted into two different missing-file
    policies (#261).
    """
    try:
        claude_md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return scrub_stale_verify_commands(claude_md, commands)


def _failed_gate_result(
    name: str,
    message: str,
    parsed: ParsedOutput,
    cmd: str,
    cwd: Path,
    start: float,
) -> CheckResult:
    """Enrich a parse and package it as the gate's failing CheckResult.

    One home for all three gates because the enrichment has to happen at
    every one of them and forgetting a step is SILENT: without
    ``parsed.command`` the prompt label falls back to the parser name,
    which is exactly the #258 mislabel returning unannounced.

    #227: the row's ``measured`` is the parser's own answer, and this is
    the only place it is decided. ``ParsedOutput.recognised`` is True
    when a parser for this gate saw its tool reporting a failure - a
    diagnostic in the tool's format, or the tool's own failure footer -
    and False for everything else, uv's exit 2 for a command it could
    not spawn and the shell's 127 included.

    Decided here rather than passed in. Round 1 of #357 decided it at
    the three call sites from the EXIT CODE, ``returncode not in {126,
    127}``, and round 2 of review measured what that is worth on the
    commands this repository actually ships: the gate defaults are
    ``uv run pytest`` / ``uv run mypy .`` / ``uv run ruff check .``, and
    uv spawns the child itself and reports its OWN status, which is 2.
    So ``uv run <missing> check .`` returned ``measured=True`` and the
    comparison reported ``fixed={'linter:E501': 12, 'linter:F401': 3}``
    - uninstalling a linter read as fixing every one of its findings,
    which is exactly what the exit-code rule existed to prevent. A
    status is the LAUNCHER's, and only the tool's own report is
    evidence that the tool ran.
    """
    parsed.command = cmd
    for failure in parsed.failures:
        # eslint's default formatter prints ABSOLUTE paths, so without
        # this the engineer is handed a path rooted in kstrl's throwaway
        # worktree: correct on disk, useless as an instruction, and not
        # the path its own tools use. Deliberately the FILE only. The
        # message is prose the tool wrote and may quote a path too
        # (measured: vitest's load errors do); rewriting a tool's own
        # sentences by string substitution is a different and less safe
        # mechanism than resolving a path, and the file is the field the
        # engineer acts on and add_source_context resolves.
        if failure.file:
            failure.file = relative_to_root(Path(failure.file), cwd)
        add_source_context(failure, cwd)
        if not failure.fix_hint:
            failure.fix_hint = generate_fix_hint(failure)
    return CheckResult(
        name=name,
        passed=False,
        message=message,
        details=parsed.format_for_prompt(),
        duration_seconds=time.monotonic() - start,
        parsed=parsed,
        measured=parsed.recognised,
    )


def check_test_suite(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run the project's test suite independently.

    ``tool`` pins which parser reads the output; None runs every parser
    registered for the gate and unions what they find (#258).
    """
    start = time.monotonic()
    cmd = resolve_test_command(command)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_TEST,
            passed=False,
            message=f"Test suite timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except ChildOutputDecodeError as exc:
        return CheckResult(
            name=GATE_TEST,
            passed=False,
            message=f"Test suite output could not be decoded: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_TEST,
            f"Tests failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_TEST, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_TEST,
        passed=True,
        message="Tests passed",
        duration_seconds=time.monotonic() - start,
    )


def check_typecheck(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run typecheck independently. See ``check_test_suite`` for ``tool``."""
    start = time.monotonic()
    cmd = resolve_typecheck_command(command, cwd)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_TYPECHECK,
            passed=False,
            message=f"Typecheck timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except ChildOutputDecodeError as exc:
        return CheckResult(
            name=GATE_TYPECHECK,
            passed=False,
            message=f"Typecheck output could not be decoded: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_TYPECHECK,
            f"Typecheck failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_TYPECHECK, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_TYPECHECK,
        passed=True,
        message="Typecheck passed",
        duration_seconds=time.monotonic() - start,
    )


def check_linter(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run linter independently. See ``check_test_suite`` for ``tool``."""
    start = time.monotonic()
    cmd = resolve_lint_command(command)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_LINT,
            passed=False,
            message=f"Linter timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except ChildOutputDecodeError as exc:
        return CheckResult(
            name=GATE_LINT,
            passed=False,
            message=f"Linter output could not be decoded: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_LINT,
            f"Linter failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_LINT, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_LINT,
        passed=True,
        message="Linter passed",
        duration_seconds=time.monotonic() - start,
    )


#: What the two diff-driven checks report when the diff handed them nothing.
#:
#: One constant because the dampener turns a row's message into the REASON a
#: check is unmeasured, and round 2 of review on #357 found the two checks
#: disagreeing about the same empty diff - one measured, one did not. They sit
#: on the same `git diff`, so they answer this question together or the
#: mechanism is a coin toss over which check the operator configured.
NO_FILES_IN_THE_DIFF = "no files in the diff"

#: H3 (#303): fragments _diff_scope_details assembles; versioned as one
#: body (docs/adversarial-roadmap.md, H3a sweep row).
DIFF_SCOPE_DETAILS_PROMPT_VERSION = "1.0.0"

DIFF_SCOPE_BASE_BRANCH_PROMPT = (
    "Base branch: {base_branch} "
    "(scope is judged on `git diff {base_branch}...HEAD`; "
    "do NOT `git checkout {base_branch} -- <path>`, revert only "
    "your own out-of-scope commits/edits)"
)
DIFF_SCOPE_ALLOWED_PATHS_PROMPT = "Allowed paths (complete list): {allowed_paths}"
DIFF_SCOPE_HARNESS_PATHS_PROMPT = (
    "Plus harness artifacts (kstrl's own files, already in "
    "scope, no need to widen allowedPaths): {harness_paths}"
)
DIFF_SCOPE_VIOLATIONS_PROMPT = "Files outside allowed scope:\n{violations}"
DIFF_SCOPE_TRUNCATION_PROMPT = "  ... and {count} more"


def _diff_scope_details(
    base_branch: str,
    allowed_paths: list[str],
    harness_paths: list[str] | None,
    violations: list[str],
) -> list[str]:
    """Failure details for a diff that left its scope.

    R0.4: name the base branch and the FULL allowed-paths list. Without
    them the retry agent has to guess both; the recorded e2e run guessed
    `main` as base and reverted base-branch content with `git checkout
    main -- ...`, failing again. Base branch and allowed paths are single
    detail entries at the head of the list so
    ``VerificationResult.as_context()``'s ``details[:10]`` slice carries
    them into the retry prompt verbatim.

    #264: the harness carve-out is its own entry, never folded into the
    authored list. The operator has to be able to read what THEY
    authorised, and the retry agent has to know its own PRD and progress
    log are already in scope - telling it to stop writing those is the
    one instruction it cannot obey and still pass ``prd_stories``.
    """
    shown = violations[:15]
    violation_lines = [f"  - {v}" for v in shown]
    if len(violations) > len(shown):
        violation_lines.append(
            DIFF_SCOPE_TRUNCATION_PROMPT.format(count=len(violations) - len(shown))
        )
    harness_note = (
        [DIFF_SCOPE_HARNESS_PATHS_PROMPT.format(harness_paths=", ".join(harness_paths))]
        if harness_paths
        else []
    )
    return [
        DIFF_SCOPE_BASE_BRANCH_PROMPT.format(base_branch=base_branch),
        DIFF_SCOPE_ALLOWED_PATHS_PROMPT.format(allowed_paths=", ".join(allowed_paths)),
        *harness_note,
        # One multi-line entry so as_context()'s details[:10] slice
        # cannot drop violations or the truncation marker.
        DIFF_SCOPE_VIOLATIONS_PROMPT.format(violations="\n".join(violation_lines)),
    ]


#: The Phase 1 check name for mutation testing, and the ``check`` on
#: every :class:`NotMeasured` mutation testing produces.
#:
#: A constant rather than four literals for a reason that is measured,
#: not stylistic: ``tests/test_check_name_enrolment.py`` AST-walks
#: ``kstrl/`` for every name that can reach
#: ``evolution.category_for_check``, and it resolves literals and
#: module-level constants - not function-locals. Writing this name once
#: as ``name = "mutation_testing"`` inside
#: :func:`check_mutation_score` took it from the 19 names that walk
#: sees to 18, silently, because the walk fails on an unenrolled name
#: and cannot fail on one it cannot see. Same rule as the prompt walk
#: in #299: hoist it to a constant the guard can resolve.
MUTATION_TESTING_CHECK = "mutation_testing"


#: The Phase 1 check name for R8.5 Layer 1, patch coverage (#152). Diff-
#: dependent, so it is in :data:`DIFF_DEPENDENT_CHECKS`; enrolled in
#: :data:`kstrl.evolution._CATEGORY_BY_CHECK` like every other check name.
PATCH_COVERAGE_CHECK = "patch_coverage"


#: R8.5 Layer 2 (#152): mutation scoped to the changed AND covered lines.
DIFF_MUTATION_CHECK = "diff_mutation"


#: The Phase 1 check name for the vulture-or-``dead_code_command``
#: phase, and the ``check`` on every :class:`NotMeasured` it produces.
#:
#: It keeps the name the fused check had, and the new phase beside it
#: takes a new one, rather than the other way round. Three reasons, all
#: checkable. ``evolution.signatures_from_verification`` emits a
#: signature only for a FAILED check and the ruff phase has no failing
#: return, so every ``dead_code:*`` signature any journal could ever
#: carry came from this phase. This is the phase that reads
#: ``git diff``, so this is the one :data:`DIFF_DEPENDENT_CHECKS`
#: names. And ``[verify] dead_code_command`` replaces vulture outright,
#: so a name mentioning vulture would be wrong whenever the operator's
#: own detector runs.
DEAD_CODE_CHECK = "dead_code"

#: The Phase 1 check name for the ruff F401/F811/F841 phase (#335).
#:
#: New in the split, and NOT diff-dependent: ruff scans ``.``, so it has
#: an honest answer with no base to diff against. It is still suppressed
#: wherever ``[verify] dead_code_cleanup`` is turned off, which is what
#: ``narrow_to_undiffed`` does, because one toggle owns both phases.
DEAD_CODE_RUFF_CHECK = "dead_code_ruff"


#: The Phase 1 check name for "no trustworthy scope could be read".
#:
#: Deliberately NOT ``scope_source``, which is already taken in the same
#: substrate: ``events.ComponentScopeResolved.scope_source`` is a
#: payload FIELD naming which authority supplied a component's
#: allowlist (component_prd / run_flag / unconstrained / unresolved).
#: A check of that name reaches the same ``events.jsonl`` as a VALUE in
#: ``VerificationResultEvent.checks``, so one token would carry two
#: unrelated meanings for the dashboards that read that file.
SCOPE_UNREADABLE_CHECK = "scope_unreadable"


#: Opening words of the failure recorded when a component is refused for
#: an unreadable scope. Load bearing twice over, so it is a constant
#: rather than a literal: ``evolution._classify_check`` matches on it to
#: recover the check name from a manifest written by an earlier process,
#: and it is what an operator sees first in the inbox, the notification
#: and ``comp.error``.
SCOPE_UNREADABLE_ERROR_PREFIX = "Component scope could not be read; retrying cannot change it"


def scope_unreadable_error(cause: str) -> str:
    """The recorded error for an unreadable scope, carrying its cause.

    ``pipeline.fail`` writes this to ``comp.error``, the
    ``ComponentFailed`` event, ``notify.fire_first_failure`` and the
    HALTED_RUN inbox item's detail. A fixed string left all four saying
    only THAT the scope was unreadable, while the file to restore sat in
    the check's details, where none of them look.
    ``factory._preflight_component_scope`` names the file in its own
    refusal; every refusal for this cause should read alike.
    """
    return f"{SCOPE_UNREADABLE_ERROR_PREFIX}. {cause}"


#: Rendered in place of an empty ``allowed_paths_error``. A fail-closed
#: check must not pass on an ambiguous sentinel (round 2), and it must
#: not refuse while naming no cause either (round 1). It refuses, and
#: says the cause is missing.
NO_CAUSE_RECORDED = "(no cause recorded; the scope resolver supplied an empty error)"

#: H3 (#303): fragments check_scope_unreadable assembles; versioned as one
#: body (docs/adversarial-roadmap.md, H3a sweep row).
SCOPE_UNREADABLE_PROMPT_VERSION = "1.0.0"

SCOPE_UNREADABLE_EXPLANATION_PROMPT = (
    "The allowedPaths this component must be judged against "
    "could not be established before the run started, so no "
    "diff can be proven in-scope. This is NOT a diff violation, "
    "and NOT something an engineer can fix from inside the "
    "worktree: the scope is read from the pre-run checkout, "
    "outside this worktree, and is fixed for the life of the "
    "run, so neither narrowing nor widening the diff changes "
    "this verdict."
)
SCOPE_UNREADABLE_REMEDY_PROMPT = (
    "The Error line above names which of two faults this is. A "
    "pre-run PRD that would not read or parse: restore that "
    "file in the main checkout and start a new run. No "
    "plan-time scope resolved for this component at all: the "
    "PRD is not the problem, the manifest and the run's "
    "resolved scope disagree about which components exist, and "
    "that is a harness fault to report rather than a file to "
    "repair. A run-wide --allowed-paths fixes neither: scope "
    "resolution refuses before it reaches the flag, so a re-run "
    "with it set fails identically."
)


def check_scope_unreadable(allowed_paths_error: str) -> CheckResult:
    """Report that no trustworthy scope could be established (R1.5, #294).

    Fails CLOSED: no allowlist could be read, so no diff can be proven
    in-scope, and silently skipping the guard is the hole R1.5 exists to
    close. Distinct from ``allowed_paths=None`` reaching
    ``check_diff_scope``, which means no scope was CONFIGURED -- a
    legitimate pass.

    Its own check, and not a branch of ``diff_scope``, because the two
    name different faults and the name is what a reader acts on (#294).
    ``diff_scope`` means "the diff touched files outside the allowlist",
    so its retry context is read as "narrow the diff". Here there was no
    allowlist to be outside of: it is resolved once at plan time from
    the pre-run checkout (``scope.ComponentScope``), which is OUTSIDE
    every worktree and fixed for the life of the run, so nothing the
    engineer writes can move this verdict.

    TWO producers, with different remedies, which is why the text points
    at the ``Error:`` line rather than asserting a cause:

    - ``ComponentScope.resolve`` could not read or parse the component's
      pre-run PRD. Restore that file.
    - ``RunScope.for_component`` had no snapshot for the component at
      all and returned its fail-closed stand-in. The PRD is fine; the
      manifest and the resolved run scope disagree about which
      components exist, which is a harness fault.

    An earlier version asserted the first cause unconditionally, so on
    the second it sent an operator to inspect a file that reads
    perfectly. That is round-1 finding 1 again: a remediation naming an
    action that cannot fix the failure.

    Neither remedy is ``--allowed-paths``. ``resolve`` returns
    ``unresolved`` BEFORE it consults the run-wide flag, on the argument
    that a scope nobody could read is not a scope that does not exist,
    so a run restarted with the flag hits the identical refusal.

    Carries an infrastructure ``Finding`` because this is the harness
    failing to establish its own input, not a judgement about the
    change. Without it a run that dies here leaves an empty finding
    stream, and every consumer using ``len(findings) == 0`` as "ran
    cleanly" reads a hard stop as clean.

    Whether it runs at all is ``_scope_checks``'s decision, and it is
    ungated there.
    """
    start = time.monotonic()
    cause = allowed_paths_error or NO_CAUSE_RECORDED
    return CheckResult(
        name=SCOPE_UNREADABLE_CHECK,
        # #227: this row is a refusal about an INPUT nobody could read, so
        # it measured nothing about the diff. `passed` is untouched and the
        # gate still fails closed; what `measured=False` buys is that the
        # signature never enters a baseline, so repairing the harness is not
        # reported as a fix and the check leaving `measured_checks` is not
        # reported as a sensor that stopped.
        passed=False,
        measured=False,
        message="Scope could not be read at plan time; failing closed",
        details=[
            f"Error: {cause}",
            SCOPE_UNREADABLE_EXPLANATION_PROMPT,
            SCOPE_UNREADABLE_REMEDY_PROMPT,
        ],
        findings=[
            Finding.infrastructure_error(
                "verify",
                f"component scope could not be established at plan time: {cause}",
            )
        ],
        duration_seconds=time.monotonic() - start,
    )


def check_diff_scope(
    cwd: Path,
    base_branch: str,
    allowed_paths: list[str] | None = None,
    *,
    harness_paths: list[str] | None = None,
) -> CheckResult:
    """Check that git diff is within expected scope.

    One question only: did the diff touch a file outside the allowlist?
    The allowlist not being READABLE is a different fault with a
    different audience, and it is ``check_scope_unreadable`` (#294).

    It no longer carries PRD TAMPERING either. That refusal moved to
    ``check_prd_stories`` when the plan-time snapshot took the scope
    question away from the worktree PRD: the file can still be rewritten
    and the stories still have to be defended, but the scope this check
    enforces is not something the rewrite can reach any more, so saying
    "scope could not be established" about it was untrue.

    ``harness_paths`` (#264) is kstrl's OWN per-component carve-out from
    ``config.component_harness_paths``: exact files kstrl's other checks
    require the agent to write (its PRD, its progress log, the codebase
    map). They widen the effective scope but are reported SEPARATELY, so
    the failure message still shows the operator what they authorised.
    They never create a scope where none was configured: with
    ``allowed_paths`` unset the check still passes unconditionally.

    Keyword-only, because #294 deleted an ``allowed_paths_error``
    parameter that sat in the 4th positional slot and this argument
    would otherwise have inherited it. Measured on the intermediate
    version: an unported caller passing the error string positionally
    got ``passed=True`` / "No scope constraints" where it intended a
    hard refusal, and with a non-empty ``allowed_paths`` the string
    splatted character by character into the effective allowlist. A
    silent fail-open is the one failure mode this check exists to
    prevent.
    """
    start = time.monotonic()

    if not allowed_paths:
        # #227: a VACUOUS pass. It reads no diff and applies no rule, so it
        # proves nothing about scope. `ks sense` with no --allowed-path takes
        # this branch every time, and with measured=True it cleared: measured
        # on the head of #357, a baseline carrying
        # `diff_scope:files-outside-allowed-scope-diff-vs-base-branch` was
        # reported FIXED by a run that never looked.
        return CheckResult(
            name="diff_scope",
            passed=True,
            message="No scope constraints (allowed_paths not set)",
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    try:
        changed = git.get_diff_names(base_branch, cwd)
    except git.GitDiffError as exc:
        # The lenient reader raises for exactly one family: a diff git
        # produced and this process cannot decode (#416). Everything else it
        # still answers with [], which the vacuous-pass branch below handles.
        # Failing closed here rather than falling into that branch is the
        # point: an undecodable diff is not an empty one.
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                "diff scope could not read the diff; failing closed "
                "(infrastructure error, not a scope pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "verify",
                    f"diff scope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    if not changed:
        # The other vacuous pass, and the one round 1 of #357 missed: the rule
        # exists but there is nothing to apply it to. Round 2 of review
        # measured the two diff-driven checks side by side on one empty diff
        # and found them disagreeing - `diff_scope` measured, `bad_patterns`
        # did not - so an adopter who sets --allowed-path had every
        # `diff_scope` baseline signature CLEARED by a pull request whose diff
        # touched none of the allowed globs.
        return CheckResult(
            name="diff_scope",
            passed=True,
            message=NO_FILES_IN_THE_DIFF,
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    # #264: the authored scope plus kstrl's own per-component files. The
    # two lists stay separate all the way into the failure details: an
    # operator reading "outside allowed scope" must be able to tell what
    # they authorised from what the harness added on their behalf.
    #
    # Deliberately NOT guards.check_violations, which is the same
    # decision on the same inputs: it takes a set and returns sorted, and
    # the violation list is truncated to 15 for the retry prompt, so
    # sorting silently changes WHICH violations the retry agent is shown.
    # Git's order is the order the operator sees elsewhere; a cosmetic
    # de-duplication is not worth moving it.
    effective = [*allowed_paths, *(harness_paths or ())]
    violations = [f for f in changed if not path_is_allowed(f, effective)]

    if violations:
        # #435: name the ref the diff was actually judged against.
        # get_diff_names resolved it; saying "main" while measuring
        # origin/main sends the engineer to revert against the wrong tree.
        base_label = git.resolve_base_ref(base_branch, cwd)
        details = _diff_scope_details(
            base_label,
            allowed_paths,
            harness_paths,
            violations,
        )
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                f"{len(violations)} files outside allowed scope "
                f"(diff vs base branch '{base_label}')"
            ),
            details=details,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="diff_scope",
        passed=True,
        message=f"{len(changed)} files, all within scope",
        duration_seconds=time.monotonic() - start,
    )


#: The two whole-file rules `check_bad_patterns` applies, spelled once. One
#: function, `_content_finding`, decides what a file's content carries for
#: BOTH the worktree scan and the base probe; these are the kind tokens it
#: returns, compared with `==` (never truthiness) by its one caller, so the
#: rule cannot become two definitions of itself with the weaker one
#: deciding the gate (#425 simplify pass S7: crediting the mechanism to
#: these constants rather than to the function that reads them survives a
#: refactor that removes the real mechanism).
EMPTY_FILE = "empty file"
SYNTAX_ERROR = "syntax error"


def _content_finding(source: Path, cfile: str) -> tuple[str, str] | None:
    """``(kind, detail)`` for the whole-file rules, or None when clean.

    BYTES, not text. The file is handed to ``py_compile``, which does its own
    PEP 263 decoding, so decoding it here first would be a second decode that
    can only disagree: a file declaring ``# -*- coding: latin-1 -*-`` is legal
    Python and used to crash this check with a ``UnicodeDecodeError`` before
    the compile it would have passed. Emptiness needs no codec either.
    """
    if not source.read_bytes().strip():
        return (EMPTY_FILE, EMPTY_FILE)
    try:
        py_compile.compile(str(source), cfile=cfile, doraise=True)
    except py_compile.PyCompileError as exc:
        return (SYNTAX_ERROR, f"{SYNTAX_ERROR} - {exc}")
    return None


def _rename_sources(records: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Destination path -> the base path its content came from.

    ``git.get_diff_name_status`` flattens one rename or copy into TWO
    consecutive records carrying the SAME status token, source first, then
    destination, because ``git diff --name-status -z`` writes
    ``R<score>``, old, new. Everything else is a single record and
    contributes nothing.

    That helper is LENIENT: when git cannot produce a name-status it returns
    ``[]``, so this map comes back empty, a renamed file is probed at its
    DESTINATION path, ``git show`` finds nothing there and the finding is
    kept. That is the blocking direction, which is why ``strict=True`` is not
    used here.

    Not a pairwise ``zip``: measured (#425 simplify pass S9), two adjacent
    renames stepped in pairs by ``zip`` associate the SECOND rename's
    destination with the FIRST rename's source - a silently wrong entry,
    not a raised error. The index-stepping loop below is what keeps each
    pair matched to its own two records.
    """
    sources: dict[str, str] = {}
    index = 0
    while index < len(records):
        status, path = records[index]
        following = records[index + 1] if index + 1 < len(records) else None
        if status[:1] in ("R", "C") and following is not None and following[0] == status:
            sources[following[1]] = path
            index += 2
        else:
            index += 1
    return sources


def _merge_base_ref(base_label: str, cwd: Path) -> str:
    """The commit ``{base_label}...HEAD`` actually diffs against, or ``""``.

    ``git.get_diff_name_status`` and ``git.get_diff_content`` both spell
    their diff ``{base_ref}...HEAD`` (three dots: git's own shorthand for
    ``git merge-base base_ref HEAD``), so the set of changed paths this
    check scans is "what this branch changed since it forked". Reading the
    base CONTENT at ``base_label``'s current tip instead is a different
    revision the moment the base branch moves after the cut - which the
    factory makes routine (``pipeline.py`` fetches ``origin/<base>`` on
    every sibling component's PR merge, while a component's worktree was
    cut once at plan time) - and a blocking gate that reads the tip can
    then CLEAR a finding the branch wrote itself, because the tip
    independently carries a finding of the same KIND (#425 review, finding
    1).

    ``""`` whenever the merge base cannot be found: an absent ref,
    unrelated histories, a timeout, or a spawn that could not run at all.
    ``""`` is a REFUSAL to clear, never a clear - ``_base_finding``'s first
    line keeps the branch's finding on it - so every uncertainty here is
    the blocking direction, the same rule ``_base_finding`` itself follows.
    """
    try:
        found = subprocess.run(
            ["git", "merge-base", base_label, "HEAD"],
            cwd=cwd,
            capture_output=True,
            timeout=git.DEFAULT_TIMEOUT,
        )
        if found.returncode != 0:
            return ""
        return found.stdout.decode("utf-8").strip()
    except Exception:
        return ""


def _base_finding(
    base_commit: str,
    path: str,
    cwd: Path,
    blob: Path,
    cfile: str,
) -> str | None:
    """The finding KIND ``path`` already carried at ``base_commit``, or None.

    None whenever that cannot be established: ``base_commit`` itself could
    not be resolved (``""``, see :func:`_merge_base_ref`), the path was
    absent there, git could not be asked, the blob could not be written, or
    the base content carries no finding. This is a CLEARING mechanism, so
    every uncertainty it has keeps the branch's finding (CLAUDE.md
    guard-design rule 3: a guard that clears must be narrow, and one that
    cannot PROVE must flag).

    ``base_commit`` is the MERGE BASE of the base branch and ``HEAD``
    (:func:`_merge_base_ref`), never the base branch's current tip: every
    diff this check reads is taken at that same revision, and reading the
    base CONTENT anywhere else is a different commit the moment the base
    branch moves (#425 review, finding 1).

    ``Exception`` exactly, and EVERYTHING inside it, unlike
    ``config_toml.load_toml_document`` which deliberately keeps its I/O
    outside the guard. The reason the rules differ: there, a widened guard
    could swallow an ``OSError`` that has to be reported. Here every failure
    means "cannot clear", which is the blocking direction and is reported as
    the branch's finding, so there is nothing a widening can hide. Measured:
    ``py_compile.compile`` escapes with a bare ``OSError`` when it cannot
    write ``cfile``, and ``Path.write_bytes`` and ``Path.read_bytes`` raise
    ``OSError`` too; ``subprocess.run`` raises ``subprocess.TimeoutExpired``,
    a ``SubprocessError`` and NOT an ``OSError``, on the git spawn itself
    (#425 review finding C1: narrowing this clause to ``except OSError`` is
    green on every census pin, and red on nothing, until a test drives a
    non-OSError failure through this exact function -
    ``tests/test_bad_patterns_diff_scope.py::test_a_non_os_error_from_the_base_probe_keeps_the_branchs_finding``
    is that test). Any of these outside the guard is a traceback out of a
    blocking Phase 1 gate, which is the defect #416 closed everywhere else.

    NOT :func:`run_scrubbed`, which every other child this module spawns
    goes through. That wrapper decodes as utf-8 and raises
    ``ChildOutputDecodeError`` on bytes it cannot decode, which is the
    second decode this function exists to avoid: the blob is a source file
    handed straight to ``py_compile``, which does its own PEP 263 decoding.
    Bare ``subprocess.run`` with an explicit ``timeout`` is the spelling
    ``kstrl/git.py`` uses for all of its own git spawns.
    """
    if not base_commit:
        return None
    try:
        shown = subprocess.run(
            ["git", "show", f"{base_commit}:{path}"],
            cwd=cwd,
            capture_output=True,
            timeout=git.DEFAULT_TIMEOUT,
        )
        if shown.returncode != 0:
            return None
        blob.write_bytes(shown.stdout)
        finding = _content_finding(blob, cfile)
    except Exception:
        return None
    return None if finding is None else finding[0]


def _scan_changed_python(
    cwd: Path,
    py_files: Sequence[str],
    base_branch: str,
    rename_sources: Mapping[str, str],
    secret_hit_paths: frozenset[str],
) -> tuple[list[str], list[str], int]:
    """``(issues, preexisting, scanned)`` for the changed Python files.

    ``scanned`` counts the files this check actually OPENED, which is the
    number that says what it measured. ``len(py_files)`` is the number the
    diff NAMED: round 2 of review on #357 ran it on a deletion-only commit
    and got "Scanned 3 Python files, no issues" with measured=True, having
    opened none of them. A deleted file cannot be shown to be free of
    secrets.

    ``preexisting`` is the findings this branch did not write: counted in
    the row's ``message`` and listed in its ``details`` for
    ``ks sense --json``, though ``VerificationResult.report_lines`` and
    ``as_context`` skip a PASSING check's details, so the terminal report
    an operator reads shows only the count (#425 review, finding 4).

    ``base_branch`` is resolved to a base ref, then to the merge-base
    commit that ref and ``HEAD`` actually share, LAZILY on the first file a
    rule flags, and cached in ``base`` for the rest of the scan (#425
    review, findings 1 and 2): both cost a real process spawn, so the
    common case - nothing flagged - pays neither, which restores this
    check's spawn count on that path to what it was before #414.
    """
    issues: list[str] = []
    preexisting: list[str] = []
    scanned = 0
    base: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="kstrl-bytecode-") as bytecode_dir:
        # One reused destination for the bytecode: the content is never read
        # back, only the compile's success or failure is. `base_blob` holds
        # the base content for the same reason, and the scan still writes
        # nothing into the tree it is reading.
        scratch = Path(bytecode_dir)
        cfile = os.path.join(bytecode_dir, "scan.pyc")
        base_blob = scratch / "base_blob.py"
        for rel_path in py_files:
            full_path = cwd / rel_path
            if not full_path.exists():
                continue
            finding = _content_finding(full_path, cfile)
            scanned += 1
            if finding is not None:
                # A finding that was already true at the base is not the
                # branch's. Read the SAME rule at the base, through the path
                # the content came from, which for a rename is its source.
                # `== kind`, not a truthiness test: a file EMPTY at the base
                # that holds a syntax error now carries a different kind at
                # each end, and the branch wrote the syntax error.
                kind, detail = finding
                base_path = rename_sources.get(rel_path, rel_path)
                if not base:
                    base_label = git.resolve_base_ref(base_branch, cwd)
                    base.append((base_label, _merge_base_ref(base_label, cwd)))
                base_label, base_commit = base[0]
                if _base_finding(base_commit, base_path, cwd, base_blob, cfile) == kind:
                    preexisting.append(
                        f"{rel_path}: {kind} was already there at "
                        f"{base_label}:{base_path}; not this branch's change"
                    )
                else:
                    issues.append(f"{rel_path}: {detail}")
                continue
            # Secret patterns: did THIS file add one of the lines the scan
            # above matched? Keyed on path, not on re-reading the file's own
            # lines - which added lines match is a property of the diff.
            if rel_path in secret_hit_paths:
                issues.append(f"{rel_path}: possible secret/credential detected")
    return issues, preexisting, scanned


def check_bad_patterns(
    cwd: Path,
    base_branch: str,
    secret_patterns: Sequence[str] = DEFAULT_SECRET_PATTERNS,
) -> CheckResult:
    """Scan changed files for obvious problems.

    The scan reads the tree and writes nothing into it. The syntax
    check earns that: ``py_compile.compile`` defaults its output to
    ``<dir>/__pycache__/<name>.pyc`` NEXT TO the source, so scanning
    used to leave bytecode behind - noise in the factory's own diff,
    and a write ``ks sense`` (R10.1) promises never to make. Directing
    ``cfile`` at a throwaway directory keeps the ``PyCompileError``
    type and message byte-identical; only the destination moves.

    Which files add a secret is a property of the DIFF, not of a file
    (#399 simplify pass): it is computed once, by intersecting the lines
    this branch ADDED with ``secret_patterns`` via ``policy._scan_secrets``
    - the same function ``check_policy_envelope`` evaluates its own
    ``[policy] secret_patterns`` through - one rule, not two copies of it.
    The result is keyed by PATH, which is safe again now that
    ``policy.parse_added_lines`` (#399 addendum) unquotes a git-quoted path
    before comparing it to ``git diff --name-status``'s own, unquoted
    spelling of the same file. The caller passes the envelope's own
    ``PolicyConfig.secret_patterns`` when it has one; ``PolicyConfig.load``
    reads that field unconditionally, whether or not ``[policy] enabled``
    is true, so a stock install (no config at all) keeps this default,
    which is that same list.

    The empty-file and syntax-error rules judge the file as it sits in the
    worktree AND as it sat at the MERGE BASE of the base branch and ``HEAD``
    (#414), because a finding that was already true there is not this
    branch's. The merge base, not the base branch's current tip: every diff
    this check reads is taken at that same revision, and reading the base
    CONTENT at the tip is a different commit the moment the base branch
    moves, which the factory makes routine (#425 review, finding 1). For a
    rename the base is read through the SOURCE path from ``--name-status``,
    since that is where the content came from. A base read that cannot be
    done keeps the finding: this is a clearing mechanism, and one that
    cannot prove must flag. A dropped finding is counted in the row's
    ``message`` and listed in its ``details`` for ``ks sense --json``;
    ``VerificationResult.report_lines`` and ``as_context`` skip a passing
    check's details, so the terminal report an operator reads shows only
    the count (#425 review, finding 4).
    """
    start = time.monotonic()

    # #399: which changed files add a secret. Read and scan the diff only
    # when there is a Python file to check, so a diff with nothing to open
    # keeps the vacuous pass it has today instead of gaining a new way to
    # fail. One name-status call rather than two (#425 review, findings 1
    # and 2): `get_diff_names` IS `get_diff_name_status` projected and
    # deduped through `git._unique_paths`, private to `kstrl/git.py` (the
    # #423 lane owns that file this cycle) - the comprehension below is
    # that same dedupe written out, not a second copy of a public name.
    # `get_diff_name_status` is inside the try as of #414: it is lenient
    # about a diff git could not produce but raises on one it could not
    # DECODE, and outside the try that left this blocking gate as a
    # traceback (PR #419 handoff 1).
    secret_hit_paths: frozenset[str] = frozenset()
    rename_sources: dict[str, str] = {}
    try:
        records = git.get_diff_name_status(base_branch, cwd)
        changed = list(dict.fromkeys(path for _, path in records if path))
        py_files = [f for f in changed if f.endswith(".py")]
        if py_files:
            diff_text = git.get_diff_content(base_branch, cwd)
            secret_hit_paths = frozenset(
                _scan_secrets(parse_added_lines(diff_text), secret_patterns)
            )
            rename_sources = _rename_sources(records)
    except Exception as exc:
        # Exception exactly, broad clause last (#318). get_diff_name_status
        # is LENIENT, so the file list can arrive when the diff does not; at
        # least two unrelated families are measured reaching here: a
        # GitDiffError, and a UnicodeDecodeError (a ValueError) from a
        # diff this process could not decode. A misconfigured secret
        # pattern (PolicyConfigError, also a ValueError, raised inside
        # `_scan_secrets`) reaches the same clause for the same reason:
        # this check runs by default, so a bad regex must fail this row
        # closed rather than crash the whole verification run. The row
        # fails CLOSED, so a swallow costs a visible red gate, never a
        # silent pass.
        return CheckResult(
            name="bad_patterns",
            passed=False,
            message=(
                "bad patterns could not read the diff; failing closed "
                "(infrastructure error, not a scan pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "verify",
                    f"bad patterns could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    issues, preexisting, scanned = _scan_changed_python(
        cwd, py_files, base_branch, rename_sources, secret_hit_paths
    )

    if issues:
        return CheckResult(
            name="bad_patterns",
            passed=False,
            message=f"{len(issues)} issues found in changed files",
            details=[*issues, *preexisting],
            duration_seconds=time.monotonic() - start,
        )

    # The same sentence as check_diff_scope when the cause is the same, so an
    # operator reading two unmeasured rows in one report does not have to
    # work out whether two spellings mean one fact.
    message = NO_FILES_IN_THE_DIFF
    if changed:
        message = f"Scanned {scanned} of {len(py_files)} changed Python files, no issues"
    if preexisting:
        message = f"{message} ({len(preexisting)} already at the base, not this branch's)"
    return CheckResult(
        name="bad_patterns",
        passed=True,
        message=message,
        details=preexisting,
        duration_seconds=time.monotonic() - start,
        # #227: a scan that opened nothing is a vacuous pass. It cannot prove a
        # secret or a syntax error went away.
        measured=bool(scanned),
    )


#: H3 (#303): the fragment check_policy_envelope assembles into its
#: diff-unreadable refusal text.
POLICY_ENVELOPE_PROMPT_VERSION = "1.0.0"

POLICY_DIFF_UNREADABLE_PROMPT = (
    "The change cannot be proven within policy; do not treat this as permission to merge."
)


def check_policy_envelope(
    cwd: Path,
    base_branch: str,
    config: PolicyConfig,
) -> CheckResult:
    """R8.1: enforce the declarative ``[policy]`` envelope from artifacts.

    Reads the git diff and ``uv.lock`` only, never agent self-report.
    Fails CLOSED on any infrastructure error (diff unreadable, malformed
    policy) and on any envelope violation. Enforcement-machinery edits
    are a non-overridable halt. Violation details are packed as
    individual entries so ``VerificationResult.as_context()``'s
    ``details[:10]`` slice carries them into the retry prompt.
    """
    start = time.monotonic()
    # All three reads are strict: each is a SEPARATE git subprocess, so a
    # successful content read proves nothing about the two that follow.
    # A lenient read returns [] on timeout/nonzero exit, which the
    # evaluator cannot distinguish from "nothing changed" - the change
    # would then satisfy every path and size rule vacuously.
    try:
        diff_text = git.get_diff_content(base_branch, cwd)
        changed = git.get_diff_names(base_branch, cwd, strict=True)
        numstat = git.get_diff_numstat(base_branch, cwd, strict=True)
    except git.GitDiffError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not read the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[
                f"Error: {exc}",
                POLICY_DIFF_UNREADABLE_PROMPT,
            ],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    try:
        evaluation = evaluate_policy(changed, numstat, diff_text, config)
    except PolicyConfigError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message="policy envelope is misconfigured; failing closed",
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope is misconfigured: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )
    except Exception as exc:
        # Exception exactly, broad clause last (#318). evaluate_policy calls
        # policy.parse_added_lines on the diff text this function already
        # read, and a diff header path holding bytes that are not valid
        # utf-8 makes that raise UnicodeDecodeError - a ValueError, and
        # neither a GitDiffError (the diff itself DID read) nor a
        # PolicyConfigError (the config is fine). The row fails CLOSED
        # rather than let the exception escape the check (#399 blocker 1b).
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not evaluate the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not evaluate the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    # License gate (R8.1): resolve each newly-added uv.lock dependency's
    # license and classify it. Runs only when configured (license_allow
    # non-empty).
    violations = list(evaluation.violations) + _check_licenses(
        evaluation.new_dependencies,
        config,
    )
    blocking = [v for v in violations if v.blocking]
    advisories = [v for v in violations if not v.blocking]

    findings = [
        Finding.policy_violation(
            category=v.category,
            explanation=v.explanation,
            location=v.location,
            severity=v.severity,
            suggestion=v.suggestion,
        )
        for v in violations
    ]
    # Blocking violations first: as_context() slices details[:10] into the
    # retry prompt, and advisories must never crowd out a real failure.
    details = [v.explanation for v in blocking] + [v.explanation for v in advisories]

    if not blocking:
        message = evaluation.summary
        if advisories:
            message += f"; {len(advisories)} advisory(ies)"
        return CheckResult(
            name="policy_envelope",
            passed=True,
            message=message,
            details=details,
            findings=findings,
            duration_seconds=time.monotonic() - start,
        )
    message = f"{len(blocking)} policy violation(s)"
    if evaluation.machinery_hit:
        message += " including enforcement-machinery halt"
    return CheckResult(
        name="policy_envelope",
        passed=False,
        message=message,
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def _check_licenses(
    new_dependencies: list[tuple[str, str]],
    config: PolicyConfig,
) -> list[PolicyViolation]:
    """Resolve + classify the licenses of newly-added dependencies.

    Denied (copyleft) and resolved-but-not-allowlisted licenses are
    blocking. A license that no source could resolve is governed by
    ``license_unresolved``: "block" (default, fail-closed - an unprovable
    dependency is not demonstrably inside the envelope) or "advisory".
    No-op when the gate is unconfigured or nothing new was added.
    """
    if not config.license_allow or not new_dependencies:
        return []
    uv_cache = licensing.uv_cache_dir()
    violations: list[PolicyViolation] = []
    for name, version in new_dependencies:
        resolved = licensing.resolve_license(
            name,
            version,
            uv_cache=uv_cache,
            use_pypi=config.license_use_network,
        )
        if resolved is None:
            advisory = config.license_unresolved == "advisory"
            source = (
                "uv cache + PyPI both missed"
                if config.license_use_network
                else "uv cache missed; network resolution disabled"
            )
            violations.append(
                PolicyViolation(
                    category="license_unresolved",
                    location=f"{name} {version}",
                    severity="advisory" if advisory else "high",
                    explanation=(
                        f"license could not be resolved for {name} {version} "
                        f"({source})" + ("; recorded as advisory" if advisory else "")
                    ),
                    suggestion=(
                        "Warm the uv cache (`uv sync`) or allow network "
                        "resolution; set [policy] license_unresolved = "
                        '"advisory" to accept unprovable licenses.'
                    ),
                )
            )
            continue
        verdict = classify_license(
            resolved,
            config.license_allow,
            config.license_deny_partial,
        )
        if verdict == "denied":
            violations.append(
                PolicyViolation(
                    category="license_denied",
                    location=f"{name} {version}",
                    explanation=(f"denied license '{resolved}' for dependency {name} {version}"),
                    suggestion="Drop the dependency or find a permissive alternative.",
                )
            )
        elif verdict == "unknown":
            violations.append(
                PolicyViolation(
                    category="license_not_allowed",
                    location=f"{name} {version}",
                    explanation=(
                        f"license '{resolved}' for {name} {version} is not in license_allow"
                    ),
                    suggestion=(
                        f"Add '{resolved}' to [policy] license_allow if it is "
                        "acceptable for this repo."
                    ),
                )
            )
    return violations


def check_test_adequacy(
    cwd: Path,
    base_branch: str,
    config: AdequacyConfig,
    autonomy_level: int = 0,
) -> CheckResult:
    """R8.5 Layer 0: did this change weaken the suite, and do its new
    tests assert anything falsifiable?

    Reads the diff and the changed test files only - no test execution,
    no coverage, no mutation tooling, no historical data. Fails CLOSED on
    an unreadable diff, like every other artifact-reading check here.

    Advisory unless the level (or an explicit opt-in) says otherwise:
    findings are recorded either way, so switching the gate on later
    starts from evidence rather than a guess.

    File STATUS is read alongside the names: the whole-file oracle floor
    is a rule about NEW test files, and applying it to a file someone
    merely edited would fail a one-line change for oracles that predate
    it. Diff discipline applies to every changed file regardless.
    """
    start = time.monotonic()
    try:
        diff_text = git.get_diff_content(base_branch, cwd)
        records = git.get_diff_name_status(base_branch, cwd, strict=True)
        changed = [path for _, path in records]
    except git.GitDiffError as exc:
        return CheckResult(
            name="test_adequacy",
            passed=False,
            message=(
                "test adequacy could not read the diff; failing closed "
                "(infrastructure error, not an adequacy pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "adequacy",
                    f"adequacy could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
            measured=False,
        )

    sources: dict[str, str] = {}
    for rel in changed:
        if not is_test_path(rel) or not rel.endswith(".py"):
            continue
        full = cwd / rel
        if not full.exists():
            continue  # deleted; the diff analysis covers it
        try:
            sources[rel] = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # Only status "A" is new content. A rename/copy destination ("R"/"C")
    # carries tests that already existed, so it is not held to the
    # new-file oracle floor.
    new_paths = {path for status, path in records if status.startswith("A") and path in sources}
    adequacy_findings = evaluate_layer0(
        diff_text,
        sources,
        config,
        new_paths=new_paths,
    )
    blocking = layer0_blocks(config, autonomy_level)
    severity = "high" if blocking else "advisory"
    findings = [
        Finding.adequacy_finding(
            category=str(f.kind),
            explanation=f.render(),
            location=f.path,
            severity=severity,
        )
        for f in adequacy_findings
    ]

    if not adequacy_findings:
        return CheckResult(
            name="test_adequacy",
            passed=True,
            message=(f"test adequacy: {len(sources)} changed test file(s), no weakening signals"),
            duration_seconds=time.monotonic() - start,
        )
    details = [f.render() for f in adequacy_findings]
    mode = "blocking" if blocking else "advisory"
    return CheckResult(
        name="test_adequacy",
        passed=not blocking,
        message=(f"{len(adequacy_findings)} test-adequacy finding(s) [{mode}]"),
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def _changed_non_test_python(
    base_branch: str,
    cwd: Path,
    check: str,
) -> list[str] | NotMeasured:
    """The non-test Python files in ``git diff <base>...HEAD``.

    One home for the rule, because two checks act on it and both report
    ``no_target`` when it comes back empty: the mutation gate mutates
    exactly these files and the dead-code scan scans exactly these
    files. Widening what counts as a test file in one place and not the
    other would make one of the two gaps say something the other does
    not mean.

    STRICT, and that is the whole reason ``check`` is a parameter. The
    lenient read this helper started with maps a bad base ref, a missing
    ``origin/<base>``, a non-repository and a git timeout all onto
    ``[]`` - ``get_diff_names``' own docstring calls that fail-OPEN -
    and both callers turn ``[]`` into ``no_target``, the one reason
    token that means "nothing to scan, not a fault". A git read that
    FAILED is a fault, the tokens are machine-read off
    ``NotMeasured.as_token``, and ``command_failed`` already exists for
    it (#335 round 2). Returning the gap rather than raising keeps both
    call sites' shape: they already return ``NotMeasured`` on the line
    after this one.

    "Non-test" is :func:`kstrl.adequacy.is_test_path` (#152 simplify
    pass), not a bare ``not f.startswith("test")``: the latter misses
    ``src/tests/x.py`` and ``pkg/foo_test.py``, and this helper's own
    two callers are exactly the reason a second, looser definition here
    would matter - :func:`check_patch_coverage` already uses
    ``is_test_path`` through :func:`kstrl.adequacy.coverage_targets`, so
    a diff touching only ``pkg/foo_test.py`` now reads ``no_target`` the
    same way for mutation, dead-code and patch coverage alike.
    """
    try:
        changed = git.get_diff_names(base_branch, cwd, strict=True)
    except git.GitDiffError as exc:
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"git could not read the diff against {base_branch}: {exc}",
        )
    return [f for f in changed if f.endswith(".py") and not is_test_path(f)]


#: The ``read_only`` detail both mutmut-backed checks (#152 simplify
#: pass, B4) return, byte-identical: mutmut REWRITES the file it
#: mutates, so neither can run under ``ks sense``. One string rather
#: than two copies 300 lines apart drifting on the next edit.
_MUTMUT_READ_ONLY_DETAIL = "mutmut rewrites the files it mutates and cannot run read-only"


def _mutmut_missing(check: str, config_key: str) -> NotMeasured:
    """The ``tool_missing`` sidecar both mutmut-backed checks (#152
    simplify pass, B4) return when ``shutil.which("mutmut")`` is None:
    ``[verify] mutation_testing`` and ``[adequacy] diff_mutation`` carried
    a byte-identical refusal block, and this is the one copy.

    ``config_key`` is the bracketed section-and-key an operator would
    read in ``kstrl.toml`` (``"[verify] mutation_testing"`` or
    ``"[adequacy] diff_mutation"``), spelled out in full rather than
    reassembled from ``check`` so the two checks can keep their own exact
    wording without this helper guessing at it.
    """
    return NotMeasured(
        check,
        NOT_MEASURED_TOOL_MISSING,
        f"mutmut is not on PATH, so {config_key} measured nothing",
    )


def _mutmut_tool_preflight(
    check: str, config_key: str, test_command: str | None
) -> list[str] | NotMeasured:
    """The refusals both mutmut-backed checks make before they look at
    the tree: the operator's test command must be a single pytest
    invocation mutmut's ``--runner`` can wrap, and mutmut must be on
    PATH. One copy, because two copies 300 lines apart disagreed on five
    learned facts about the same tool (#391)."""
    tokens = _pytest_tokens_or_gap(check, test_command, "mutmut's runner can wrap")
    if isinstance(tokens, NotMeasured):
        return tokens
    if not shutil.which("mutmut"):
        return _mutmut_missing(check, config_key)
    return tokens


def _mutmut_tree_preflight(cwd: Path, check: str, paths: Sequence[str]) -> NotMeasured | None:
    """The refusals that read the tree and still cost no spawn: a
    project-level ``mutmut_config.py`` (its ``pre_mutation`` hook is the
    only route to mutmut's ``skipped`` status, which renders identically
    to a killed mutant in the junitxml report), and a ``<path>.bak``
    already beside a target, which kstrl cannot tell from one mutmut is
    about to write."""
    if (cwd / "mutmut_config.py").exists():
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            "mutmut_config.py is present in this project; its pre_mutation "
            "hook is the only route to mutmut's `skipped` status, which "
            "renders identically to a killed mutant in the junitxml report "
            "this check reads, so the result cannot be trusted",
        )
    existing = _preexisting_backups(cwd, paths)
    if existing:
        bak_names = ", ".join(f"{path}.bak" for path in existing)
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"a backup file is already beside a target ({bak_names}); "
            "mutmut writes <file>.bak before it mutates, so kstrl cannot tell "
            "its backup from this one and refused rather than risk "
            "overwriting the project's file",
        )
    return None


def check_mutation_score(
    cwd: Path,
    base_branch: str,
    test_command: str | None,
    threshold: float = 50.0,
    timeout: float = 600.0,
) -> CheckResult | NotMeasured:
    """R8.5 Layer 1 (#152, #391): mutate every non-test Python file this
    diff changed, and score the report against ``threshold``.

    ``test_command`` has no default (#391 simplify pass on PR #392, C5):
    it is load-bearing for this check's own ``tool_missing`` refusal
    (D6), the same reason :func:`check_diff_mutation`'s identical
    parameter has never had one, so a caller states its choice rather
    than inheriting a smart default it never asked for. ``None`` is
    still a legal value - :func:`resolve_test_command` reads it as the
    harness default - it is only the silent ``= None`` that is gone.

    Returns a :class:`CheckResult` - PASS or FAIL against ``threshold`` -
    only when a score was actually measured. Every other path returns
    :class:`NotMeasured`, which is a SIDECAR record and never a row in
    ``checks`` (#306).

    Six reasons return NotMeasured, and the ``reason`` token separates
    them because they are not the same event:

    - ``tool_missing``: mutmut is not on PATH, OR ``[verify]
      test_command`` is not a single pytest invocation mutmut's
      ``--runner`` can wrap (D6, #391) - a behaviour change from before
      #391, when this check ignored ``test_command`` entirely.
    - ``no_target``: the diff changed no non-test Python file. Nothing
      to mutate; not a fault.
    - ``timed_out``: the ``[verify] mutation_timeout`` cap fired.
      :data:`_MUTMUT_CANNOT_REPORT_TRUNCATED` (D4, #391), so the report
      spawn never follows a fired cap.
    - ``command_failed``: a pre-existing ``<path>.bak`` beside a target,
      a project ``mutmut_config.py``, a fatal mutmut exit (bit 1 of its
      return code), or a report mutmut wrote but kstrl could not parse.
    - ``no_mutants``: mutmut reported no mutant on any target line.

    The sixth, ``read_only``, never reaches this function:
    :func:`_mutation_checks` owns it, because mutmut works by rewriting
    the source files it mutates.

    #391 replaced this function's own spawn, its ``mutmut results`` text
    parse and its bespoke cache/``.bak``/mode bookkeeping with the same
    driver R8.5 Layer 2 (:func:`check_diff_mutation`) already used:
    :func:`_mutmut_measure`, parameterised on the target selector -
    ``None`` here for whole-file scope, derived from the report's own
    rows restricted to the files kstrl asked for (D2), rather than a
    synthetic ``--use-patch-file`` patch Layer 2 builds. ``mutmut
    results`` prints a survivor bucket with mutant id ranges and no
    killed count under any flag; the counts existed only in the
    progress line ``--no-progress`` suppresses.

    Every one of the situations above used to return
    ``CheckResult(passed=True)`` - so a green ``mutation_testing`` row
    meant "we did not look" as often as it meant "we looked and it was
    fine" - and :func:`kstrl.review.build_review_prompt` copied that row
    into the LLM reviewer's prompt as ``mutation_testing: PASS``.

    NotMeasured rather than a not-measured STATUS on the row, because
    ``passed`` is the only field every consumer reads: a third state
    there still reads as a pass through ``all(c.passed ...)``,
    ``report_lines``, ``ks sense --json`` and that reviewer prompt, for
    every reader not yet taught the new field. Absence from ``checks``
    is also the convention this repo already wrote down - see
    :func:`kstrl.feature_verify` on its own suppressed checks, "the
    suppressed checks are ABSENT from it rather than recorded as passing
    skips: a machine reader doing ``all(c.passed)`` must never see a
    check that measured nothing counted as a pass".

    And not a FAIL either, on the paths where something is genuinely
    wrong. A failing mechanical check is retry context for the engineer,
    and installing a binary is not a thing an engineer iteration can do,
    so a FAIL there spends ``repair_max_runs`` iterations on a diff that
    cannot change the outcome. Halting for a human on mutation infra
    failure is the L3+ behaviour in ``docs/dark-factory-roadmap.md``
    (Layer 2, "mutation infra failure halts for a human rather than
    skipping"), halt is not fail, and that layer is unbuilt. The sidecar
    is the third option: seen by the operator, gating nothing.
    """
    start = time.monotonic()
    tokens = _mutmut_tool_preflight(
        MUTATION_TESTING_CHECK, "[verify] mutation_testing", test_command
    )
    if isinstance(tokens, NotMeasured):
        return tokens
    py_files = _changed_non_test_python(base_branch, cwd, MUTATION_TESTING_CHECK)
    if isinstance(py_files, NotMeasured):
        return py_files
    if not py_files:
        return NotMeasured(
            MUTATION_TESTING_CHECK,
            NOT_MEASURED_NO_TARGET,
            "the diff changed no non-test Python file, so there was nothing to mutate",
        )
    gap = _mutmut_tree_preflight(cwd, MUTATION_TESTING_CHECK, py_files)
    if gap is not None:
        return gap
    score = _mutmut_measure(cwd, MUTATION_TESTING_CHECK, tokens, py_files, None, timeout)
    if isinstance(score, NotMeasured):
        return score
    return _mutation_score_result(score, threshold, start)


def _no_measured_lines(check: str, score: MutationScore) -> NotMeasured:
    """The gap a zero-measured score is, for either mutation check:
    ``no_mutants`` when mutmut reported no mutant on any target line,
    ``command_failed`` when it reported some and none carries a definite
    status. Zero killed out of zero measured is not a score."""
    if score.mutable_lines == 0:
        return NotMeasured(
            check,
            NOT_MEASURED_NO_MUTANTS,
            "mutmut ran cleanly and generated no mutant on any target line, so there is no score",
        )
    return NotMeasured(
        check,
        NOT_MEASURED_COMMAND_FAILED,
        f"mutmut reported {score.mutable_lines} mutant(s) on the target "
        "lines and none carries a killed or survived status",
    )


def _mutation_score_result(
    score: MutationScore, threshold: float, start: float
) -> CheckResult | NotMeasured:
    """Turn a scored mutmut run into Layer 1's row or a sidecar (#391)."""
    if score.measured_lines == 0:
        return _no_measured_lines(MUTATION_TESTING_CHECK, score)
    percent = 100.0 * score.killed_lines / score.measured_lines
    details = [
        f"Killed: {score.killed_lines}, Survived: {len(score.survivors)}, "
        f"Measured: {score.measured_lines}",
        f"Score: {percent:.1f}% (threshold: {threshold}%)",
    ]
    details.extend(f"survived: {path}:{line}" for path, line in score.survivors)
    if percent < threshold:
        return CheckResult(
            name=MUTATION_TESTING_CHECK,
            passed=False,
            message=f"Mutation score {percent:.1f}% below threshold {threshold}%",
            details=details,
            duration_seconds=time.monotonic() - start,
        )
    return CheckResult(
        name=MUTATION_TESTING_CHECK,
        passed=True,
        message=f"Mutation score {percent:.1f}% (threshold: {threshold}%)",
        details=details,
        duration_seconds=time.monotonic() - start,
    )


def _last_output_line(result: subprocess.CompletedProcess[str]) -> str:
    """The last line a failed tool printed, capped, for a gap's detail.

    stderr first because that is where a tool that could not start says
    so, and the last line because that is where a Python traceback and
    a git failure put the sentence a reader needs.

    NOT because "a usage message puts it at the bottom", which is what
    this said and is measurably false for the tool this module runs
    most. ruff is a clap CLI, and clap prints the diagnosis FIRST and
    `For more information, try '--help'.` last: measured on ruff 0.1.15,
    stderr line 1 is ``error: invalid value 'concise' for
    '--output-format <OUTPUT_FORMAT>'`` and the last line carries no
    cause at all (#335 round 3). :func:`_tool_failure_line` is the
    caller for that case.

    Capped for the reason git.py caps its stderr at 500: this reaches
    ``ks sense --json`` and the terminal, and one unbroken line of tool
    output has no bound.

    Each side is stripped BEFORE the choice, not after: ``stderr or
    stdout`` on the raw strings picks stderr whenever it is truthy,
    including when it holds nothing but a newline, and then returns "no
    output" while the sentence the reader needs sits in stdout (#335
    round 2).
    """
    tail = (result.stderr.strip() or result.stdout.strip()).splitlines()
    return tail[-1][:500] if tail else "no output"


#: clap's own prefix for the diagnosis, at the start of a line. ruff, uv
#: and cargo all print it; a Python traceback never does.
_CLAP_ERROR = re.compile(r"^error:.*", re.MULTILINE)


def _tool_failure_line(result: subprocess.CompletedProcess[str]) -> str:
    """Why a tool refused to run at all, capped, for a gap's detail.

    The FIRST stderr line beginning ``error:`` when there is one, and
    :func:`_last_output_line` otherwise. A tool that exits before doing
    any work states the cause at the top and the remedy at the bottom,
    so the last line is the wrong end of it.

    Measured on the ruff versions below this phase's floor, which is the
    reachable case for it: 0.0.272 prints ``error: unexpected argument
    '--output-format' found`` then a 40-line usage block, and 0.1.0 and
    0.1.15 print ``error: invalid value 'concise' for '--output-format
    <OUTPUT_FORMAT>'`` then the possible values. Both end
    ``For more information, try '--help'.``, which is what the gap
    carried before and says nothing about the cause (#335 round 3).
    """
    found = _CLAP_ERROR.search(result.stderr)
    return found.group(0).strip()[:500] if found else _last_output_line(result)


# ---------------------------------------------------------------------------
# R8.5 Layer 1 (#152): patch coverage. Advisory, no floor - see
# check_patch_coverage's docstring for the full register.
# ---------------------------------------------------------------------------
_SHELL_OPERATORS: tuple[str, ...] = ("&", "|", ";", "\n", "<", ">", "`", "$")


def _validated_pytest_tokens(test_command: str) -> list[str] | None:
    """The tokenised ``test_command``, validated as a single pytest
    invocation - or ``None``.

    Named for what it does rather than for its first caller (#152
    simplify pass, B5): despite the old name it does nothing
    coverage-specific, and R8.5 Layer 2 (:func:`_diff_mutation_preflight`)
    calls it too, to build the ``--runner=`` value it hands mutmut - not
    to "extend under coverage" at all.

    ``None`` when the resolved command contains any :data:`_SHELL_OPERATORS`
    character, when ``shlex.split`` cannot tokenize it, or when no token
    is exactly ``"pytest"``. The tokens are never handed to a shell:
    :func:`_coverage_report` runs both of its spawns as LISTS, and
    :func:`_mutation_run_command` shell-joins them into a single
    ``--runner=`` value it never hands a shell either, so a file name
    that came out of an agent-authored diff can never be interpreted by
    one (the hazard #335 round 2 found on the mutation command).
    """
    if any(op in test_command for op in _SHELL_OPERATORS):
        return None
    try:
        tokens = shlex.split(test_command)
    except ValueError:
        return None
    if "pytest" not in tokens:
        return None
    return tokens


def _pytest_tokens_or_gap(
    check: str, test_command: str | None, clause: str
) -> list[str] | NotMeasured:
    """``test_command``, resolved and tokenised as a single pytest
    invocation the caller can extend - or the ``tool_missing`` sidecar
    naming why not, in the caller's own words (``clause``).

    One helper (#391 simplify pass, B6) for what used to be three copies
    - this PR already took it from three to two - differing only in the
    trailing clause: R8.5 Layer 1's coverage check can EXTEND the
    command (``"this can extend"``), while the two mutmut-backed checks
    hand it to mutmut's own ``--runner`` (``"mutmut's runner can
    wrap"``, D6).
    """
    tokens = _validated_pytest_tokens(resolve_test_command(test_command))
    if tokens is None:
        return NotMeasured(
            check,
            NOT_MEASURED_TOOL_MISSING,
            f"[verify] test_command is not a single pytest invocation {clause}: {test_command!r}",
        )
    return tokens


def _coverage_data_command(tokens: list[str]) -> list[str]:
    """``tokens`` plus the two flags that make the run write coverage
    DATA and no report.

    ``--cov-report=`` (an empty value, not ``json:<path>``): measured on
    ``tests/test_atomicio.py`` alone, a JSON report costs 8.2 to 9.0s /
    448MB - pytest-cov serialises every file ``--cov=.`` measured, inside
    the test process - against 2.4s / 157MB for the data file alone.
    :func:`_coverage_json_command` turns the data file into the JSON this
    check reads, at a further 0.16s / 50MB, and
    :func:`kstrl.adequacy.measure_patch_coverage` gives the identical
    result from both reports. Appends nothing else: no ``-q``, no
    ``-p no:cacheprovider``, which would break a command relying on
    ``--lf``. The coverage run is the operator's own command plus
    coverage.
    """
    return [*tokens, "--cov=.", "--cov-report="]


def _coverage_json_command(
    tokens: list[str],
    data_file: Path,
    targets: Iterable[str],
    json_path: Path,
) -> list[str]:
    """``coverage json``, narrowed to ``targets`` (D2 still holds:
    ``--cov=.`` measured the whole tree in the first spawn; ``--include``
    here only narrows what THIS command reports, so the project's own
    coverage config - ``source``, ``omit``, ``branch``, plugins - still
    applied at measurement time).

    Prefixed with every token of ``tokens`` before the literal
    ``"pytest"`` one - the runner, e.g. ``uv run`` - so ``coverage``
    resolves the way the project's own test command does: a project run
    through ``uv run pytest`` has its coverage plugin importable only
    through ``uv run coverage``, not a bare ``coverage`` off ``PATH``.
    """
    prefix = tokens[: tokens.index("pytest")]
    return [
        *prefix,
        "coverage",
        "json",
        f"--data-file={data_file}",
        f"--include={','.join(sorted(targets))}",
        "-o",
        str(json_path),
    ]


def _run_coverage_step(
    command: list[str],
    cwd: Path,
    timeout: float,
    coverage_file: Path,
) -> subprocess.CompletedProcess[str] | NotMeasured:
    """Run one of :func:`_coverage_report`'s two spawns; classify the two
    failure modes that do not differ between them (D9: bounded by
    ``timeout``, the same ceiling :func:`check_test_suite` uses, through
    :func:`run_scrubbed`'s own process-group kill).

    A non-zero exit or a missing output file mean something DIFFERENT for
    each spawn - a missing ``.coverage`` after the first is pytest-cov
    itself being absent; a missing JSON after the second is ``coverage``
    itself - so those checks stay with :func:`_coverage_report`.
    """
    try:
        return run_scrubbed(
            command,
            cwd=cwd,
            timeout=timeout,
            extra_env={"COVERAGE_FILE": str(coverage_file)},
        )
    except subprocess.TimeoutExpired:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_TIMED_OUT,
            f"the coverage run exceeded {timeout}s",
        )
    except OSError as exc:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"the coverage command could not be started: {exc}",
        )
    except ChildOutputDecodeError as exc:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"the coverage run's output could not be decoded: {exc}",
        )


def _coverage_report(
    cwd: Path,
    tokens: list[str],
    timeout: float,
    targets: Iterable[str],
    json_path: Path,
    spawn_start: float,
) -> dict[str, object] | NotMeasured:
    """Run the two coverage spawns (#152 simplify pass) and hand back the
    parsed report or the gap.

    ``COVERAGE_FILE`` derives from ``json_path``'s parent (D3, passed to
    :func:`run_scrubbed`'s ``extra_env`` - see that function for the
    one-line contract): both spawns write into the temporary directory
    the caller already created, so the project's rootdir is left
    untouched and a subsequent ``git add -A`` (``[verify]
    dead_code_cleanup``) cannot pick up a ``.coverage`` a target
    project's own ``.gitignore`` may not list. The rejected alternative
    is ``--cov-config`` pointed at a temp ``.coveragerc`` with
    ``data_file`` set: that would need no change to ``run_scrubbed`` at
    all, but it REPLACES the project's own coverage configuration
    wholesale (``source``, ``omit``, ``branch``, plugins), so the number
    it produces would stop meaning what the project's own config says it
    means. ``COVERAGE_FILE`` overrides only the data file.

    ``timeout`` and ``spawn_start`` share ONE budget across both spawns
    (#152 blocker 1): the data spawn is handed ``timeout`` in full, but
    the JSON spawn is handed whatever remains of ``timeout`` measured
    from ``spawn_start``, not another full ``timeout``. Without this,
    ``check_patch_coverage`` could spend up to ``2 * timeout`` - twice
    the ``[verify] subprocess_timeout`` ceiling every other check in
    this module is bounded by - which is exactly what the docstring on
    :func:`check_patch_coverage` claims cannot happen.

    1. The DATA spawn (:func:`_coverage_data_command`), through
       :func:`_run_coverage_step`. No ``.coverage`` on disk afterwards is
       ``tool_missing`` - FIRST among the "ran but produced nothing
       useful" checks, because a missing pytest-cov exits 4 and writes no
       data file (critic-measured: ``error: unrecognized arguments:
       --cov=.``), and ``tool_missing`` is the token an operator can act
       on. A non-zero exit otherwise is ``command_failed``: a partial
       run's coverage is not a measurement of the suite.
    2. The remaining budget is checked BEFORE the JSON spawn: if the
       data spawn alone used up ``timeout`` (or came close enough that
       nothing useful remains), this returns ``timed_out`` without
       spawning ``coverage json`` at all, rather than handing it a
       second full ``timeout``.
    3. The JSON spawn (:func:`_coverage_json_command`), through the same
       step function, bounded by the REMAINING budget. A non-zero exit
       or no JSON on disk is ``command_failed``.
    4. The JSON fails to parse, or parses to something that is not an
       object, is ONE ``command_failed`` site: the object check raises
       ``ValueError`` inside the same ``try`` the parse is in, so
       ``except (OSError, ValueError)`` catches both. ``ValueError`` and
       not ``json.JSONDecodeError`` alone: ``UnicodeDecodeError`` is a
       ``ValueError`` and would escape a narrower clause.
       ``encoding="utf-8"`` is named explicitly for the same reason.
    """
    data_file = json_path.parent / ".coverage"
    data_result = _run_coverage_step(_coverage_data_command(tokens), cwd, timeout, data_file)
    if isinstance(data_result, NotMeasured):
        return data_result
    if not data_file.exists():
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_TOOL_MISSING,
            "pytest-cov is not installed for this project's test command, "
            f"so [adequacy] patch_coverage measured nothing: {_last_output_line(data_result)}",
        )
    if data_result.returncode != 0:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"the test command exited {data_result.returncode} under coverage; "
            "a partial run's coverage is not a measurement of the suite",
        )
    remaining = timeout - (time.monotonic() - spawn_start)
    if remaining <= 0:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_TIMED_OUT,
            "the coverage data spawn used the full [verify] subprocess_timeout "
            "budget, leaving nothing for the coverage json spawn",
        )
    json_result = _run_coverage_step(
        _coverage_json_command(tokens, data_file, targets, json_path), cwd, remaining, data_file
    )
    if isinstance(json_result, NotMeasured):
        return json_result
    if json_result.returncode != 0 or not json_path.exists():
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"coverage json exited {json_result.returncode}: {_last_output_line(json_result)}",
        )
    try:
        parsed: object = read_json(json_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("the coverage report was not a JSON object")
    except (OSError, ValueError) as exc:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"the coverage report could not be read: {exc}",
        )
    return parsed


def check_patch_coverage(
    cwd: Path,
    base_branch: str,
    test_command: str | None,
    timeout: float,
) -> PatchCoverage | NotMeasured:
    """R8.5 Layer 1 (#152): what fraction of the lines this diff ADDED to
    non-test Python files did the project's own test suite execute.

    Returns the measured :class:`kstrl.adequacy.PatchCoverage` on
    success, or the :class:`NotMeasured` sidecar naming why not (#152
    simplify pass, B2: this used to return ``(outcome, coverage)``, a
    tuple whose second element is derivable from the first's type -
    ``coverage`` non-``None`` iff ``outcome`` is a passing
    :class:`CheckResult` - forcing five ``return X, None`` sites for no
    information the caller could not already have. The row a success
    builds is now :func:`_patch_coverage_row`, called from
    :func:`_patch_coverage_checks`, which is also where ``coverage``
    reaches R8.5 Layer 2 (:func:`_diff_mutation_checks`) rather than
    paying for a second coverage run: mutmut cannot compute "changed AND
    covered" itself (its ``--use-coverage`` and ``--use-patch-file`` are
    mutually exclusive), and this check already has the intersection.

    Runs ``test_command`` a SECOND time (D1) rather than folding coverage
    flags into the existing :func:`check_test_suite` run. Folding costs
    less wall clock (see the PR body for the measured numbers), but the
    load-bearing reason it is rejected is ``[verify] pin_verify_commands``:
    the engineer sees the PINNED test command through
    ``VERIFY_COMMANDS_PROMPT``, and folding would change what that
    command means without telling it. It would also let an advisory
    measurement turn a green suite RED whenever pytest-cov is not
    installed for the project (measured: exit 4, no data file), which is
    the position kstrl's own first external target (deckgen) is in
    today. A probe-plus-fold follow-up - detect pytest-cov first, fold
    only when present - is recorded as not taken, measured at 457s per
    component on this repo; not taken because it still moves what the
    engineer sees.

    See :func:`_coverage_report` for the two-spawn shape (D2: ``--cov=.``
    measures the whole tree in the first spawn; the second narrows the
    JSON to ``targets`` with ``--include``). Every other coverage-check
    docstring in this module points back to this one rather than
    repeating it.

    Four reason tokens, and why each is not the others:

    - ``tool_missing``: ``test_command`` is not a single pytest
      invocation this can extend (:func:`_validated_pytest_tokens`
      returned ``None``), or the first spawn produced no coverage data
      (pytest-cov
      is not installed for THIS project, even though the harness's own
      venv has it).
    - ``no_target``: the diff added no line to a non-test Python file.
      Checked BEFORE either spawn (:func:`kstrl.adequacy.coverage_targets`
      empty) so the no-op case costs nothing, and AGAIN after both run
      (D5: the added lines carried no statement coverage can measure, so
      ``coverage.total`` is 0) - a git read that FAILED is a fault and is
      ``command_failed``, never this token.
    - ``timed_out``: the two spawns share ONE ``timeout`` budget, the
      same ``[verify] subprocess_timeout`` :func:`check_test_suite`
      uses, not one ``timeout`` each. The data spawn is bounded by
      ``timeout`` directly; the JSON spawn is bounded by whatever
      remains of ``timeout`` once the data spawn returns, and gets
      ``timed_out`` with no second spawn at all when nothing remains.
      This check cannot double Phase 1's ceiling.
    - ``command_failed``: a spawn could not be started, git could not
      read the diff, either spawn exited non-zero, or the JSON could not
      be parsed.

    D7/D9 stated outright: this is ADVISORY ALWAYS. There is no floor
    key, no level reads here, and the finding is emitted at every
    percentage including 100%, because the distribution a floor would
    later be set from is the point of shipping this now. Both spawns
    together are bounded by ONE ``timeout``, not one each: a hung
    project suite cannot outlive the check, because each spawn goes
    through :func:`run_scrubbed`, which already spawns with
    ``start_new_session=True`` and signals the whole process group
    (SIGTERM, grace, SIGKILL) before raising
    :class:`subprocess.TimeoutExpired`.
    """
    tokens = _pytest_tokens_or_gap(PATCH_COVERAGE_CHECK, test_command, "this can extend")
    if isinstance(tokens, NotMeasured):
        return tokens
    try:
        diff_text = git.get_diff_content(base_branch, cwd)
    except git.GitDiffError as exc:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"git could not read the diff against {base_branch}: {exc}",
        )
    targets = coverage_targets(diff_text)
    if not targets:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_NO_TARGET,
            "the diff added no line to a non-test Python file",
        )
    # The two pre-flight refusals above cost nothing: no temp dir is
    # created for a command this check cannot extend, an unreadable
    # diff, or a diff with no target at all.
    spawn_start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="kstrl-coverage-") as tmp_name:
        json_path = Path(tmp_name) / "coverage.json"
        report = _coverage_report(cwd, tokens, timeout, targets, json_path, spawn_start)
    if isinstance(report, NotMeasured):
        return report
    coverage = measure_patch_coverage(targets, report)
    if coverage.total == 0:
        return NotMeasured(
            PATCH_COVERAGE_CHECK,
            NOT_MEASURED_NO_TARGET,
            "the changed lines contain no statement coverage can measure",
        )
    return coverage


def _patch_coverage_row(coverage: PatchCoverage, start: float) -> CheckResult:
    """The passing ``patch_coverage`` row for a measured ``coverage``
    (#152 simplify pass, B2), split out of :func:`check_patch_coverage` so
    that function can return the measurement alone. ``start`` is the
    caller's own :func:`time.monotonic` reading, taken immediately before
    it called :func:`check_patch_coverage` - the row's ``duration_seconds``
    is therefore the same wall clock the inlined version measured, not a
    second, later clock.
    """
    headline = (
        f"patch coverage {100.0 * coverage.covered / coverage.total:.1f}% "
        f"({coverage.covered}/{coverage.total} changed executable lines)"
    )
    per_file = [
        f"{path}: {c}/{t} changed executable line(s) covered" for path, c, t in coverage.files
    ]
    return CheckResult(
        name=PATCH_COVERAGE_CHECK,
        passed=True,
        message=f"{headline} [advisory]",
        details=per_file
        + (
            [f"not in the coverage report: {', '.join(coverage.unmeasured)}"]
            if coverage.unmeasured
            else []
        ),
        findings=[
            Finding.adequacy_finding(
                category="patch_coverage",
                explanation=f"{headline}: " + "; ".join(per_file),
                severity="advisory",
                suggestion=(
                    "Advisory only: R8.5 Layer 1 records the number, no floor is "
                    "configured and none blocks. The floor is set later from the "
                    "recorded distribution."
                ),
            )
        ],
        duration_seconds=time.monotonic() - start,
    )


def _patch_coverage_checks(
    cwd: Path,
    base_branch: str,
    config: VerifyConfig,
    adequacy_config: AdequacyConfig | None,
) -> tuple[list[CheckResult], NotMeasured | None, PatchCoverage | None]:
    """``(rows, gap, coverage)`` for patch coverage: at most one row and
    one gap (#306), plus the measured :class:`PatchCoverage` when there
    is one.

    ``gap`` is the SINGLE outcome (#152 simplify pass, B1's sibling on
    the Layer 1 side), not a list: this function by construction produces
    at most one, so a caller that wants a list still builds it, once, at
    the point that needs one.

    Exact shape of :func:`_mutation_checks`, for the exact reason its
    docstring gives: a check nobody asked for records nothing at all.
    BOTH switches are read: ``adequacy_config.enabled`` is Layer 0's
    master switch for the ``[adequacy]`` section, and an operator reading
    ``enabled = false`` must not get a second full test run out of this
    one sitting under it. ``adequacy_config.patch_coverage`` is Layer 1's
    own opt-in on top of that.

    Deliberately NO ``read_only`` parameter here, unlike
    :func:`_mutation_checks`. mutmut skips under ``read_only`` because it
    REWRITES the files it mutates; the coverage run has no such property
    (D3's ``COVERAGE_FILE`` redirect writes nothing into the tree), so
    ``ks sense`` measures this check too - the cheap way to collect the
    distribution a future floor will be set from, across repositories,
    without factory spend.

    ``coverage`` (#152) is handed to :func:`_diff_mutation_checks` so
    Layer 2 never runs a second coverage pass of its own. The disabled
    path returns ``None`` for it, same as every :class:`NotMeasured` path
    inside :func:`check_patch_coverage`.
    """
    if adequacy_config is None or not (adequacy_config.enabled and adequacy_config.patch_coverage):
        return [], None, None
    start = time.monotonic()
    outcome = check_patch_coverage(cwd, base_branch, config.test_command, config.subprocess_timeout)
    if isinstance(outcome, NotMeasured):
        return [], outcome, None
    return [_patch_coverage_row(outcome, start)], None, outcome


# ---------------------------------------------------------------------------
# R8.5 Layer 2 (#152): diff-scoped mutation. Advisory, no floor - see
# check_diff_mutation's docstring for the full register. Consumes Layer
# 1's PatchCoverage rather than running a second coverage pass.
# ---------------------------------------------------------------------------
#: Bound for the ``mutmut junitxml`` report spawn - reading a local
#: sqlite cache is not the expensive part, the bound exists so a wedged
#: process cannot hang the phase. Same value :func:`check_mutation_score`
#: already uses for `mutmut results`, and for the same reason.
_MUTATION_REPORT_TIMEOUT = 30.0


def _mutation_run_command(
    tokens: list[str],
    paths: Sequence[str],
    patch_path: Path | None,
    tests_dir: Path,
) -> list[str]:
    """The ``mutmut run`` argv both mutation checks build, as a LIST
    (#391: one driver for R8.5 Layers 1 and 2).

    ``--tests-dir`` points at an EMPTY temp directory, not the project's
    real test directory. Measured (measurements 2i): ``tests_dirs`` feeds
    only ``hash_of_tests`` (cache invalidation - this check deletes the
    cache before every run) and ``python_source_files`` (not consulted
    for explicit ``--paths-to-mutate`` file paths). Its default
    ``tests/:test/`` raises ``FileNotFoundError`` on any project whose
    tests live elsewhere (measurements 1c: why the pre-existing
    ``mutation_testing`` check cannot run on such a repo), and
    ``--tests-dir=.`` would walk ``.venv`` for the hash.

    ``-x`` is appended to the operator's own test command (D10).
    mutmut's own default runner is ``python -m pytest -x --assert=plain``;
    the command is already validated as a single pytest invocation by
    :func:`_coverage_command`, and without ``-x`` every KILLED mutant
    runs the whole suite instead of stopping at the first failure.
    ``--assert=plain`` is NOT added: it changes which failures pytest
    reports.

    The runner value is built with :func:`shlex.join`, never
    ``" ".join`` (D10): mutmut SHELLS that value itself
    (``popen_streaming_output``), and ``tokens`` is shlex-SPLIT, so a
    token can legitimately contain a space - the interpreter path on a
    machine whose home directory has one in it, for instance.
    ``" ".join`` there hands mutmut two words where kstrl meant one, and
    every mutant then reports as SURVIVING (a command-not-found), which
    is a wrong NUMBER, not a failure, so nothing would go red (test 20,
    plant 15).

    ``patch_path`` is ``None`` for whole-file scope (R8.5 Layer 1, #391):
    ``--use-patch-file`` needs the ``mutmut[patch]`` extra that plain
    mutmut does not ship (measured: ``ImportError: The --use-patch
    feature requires the whatthepatch library``, exit 1), and whole-file
    scope has nothing to say that ``--paths-to-mutate`` does not already
    say.

    Returns a LIST so kstrl hands mutmut nothing to a shell of its own;
    the runner value inside it is operator config from ``[verify]
    test_command``, never agent-authored text.
    """
    command = [
        "mutmut",
        "run",
        "--paths-to-mutate=" + ",".join(sorted(paths)),
        f"--tests-dir={tests_dir}",
    ]
    if patch_path is not None:
        command.append(f"--use-patch-file={patch_path}")
    command.extend(["--no-progress", "--simple-output", "--runner=" + shlex.join([*tokens, "-x"])])
    return command


def _preexisting_backups(cwd: Path, paths: Iterable[str]) -> list[str]:
    """The target paths that already have a ``<path>.bak`` beside them,
    sorted.

    Pure, no I/O beyond :meth:`Path.exists`. Called once, from
    :func:`_mutmut_tree_preflight` (shared by both mutation checks since
    #391; #391 simplify pass on PR #392, C5 - it used to name only
    :func:`check_diff_mutation`, which called it directly before the
    shared pre-flight existed), BEFORE any spawn (D7 step 1). A
    non-empty result is a ``command_failed`` sidecar, not a run: mutmut
    writes ``<file>.bak`` before it mutates a file, so kstrl cannot tell
    a backup already on disk from one mutmut is about to write, and
    refuses rather than risk overwriting the project's own file.
    """
    return sorted(path for path in paths if (cwd / f"{path}.bak").exists())


def _target_modes(cwd: Path, paths: Iterable[str]) -> dict[str, int]:
    """The permission bits of every mutation target, read BEFORE the run.

    mutmut 2.5.1 does not preserve them: it writes its backup with
    ``open(path + '.bak', 'w')`` (the umask default) and restores it with
    ``shutil.move``, which renames, so the backup's mode lands on the
    source file. A 0755 module therefore comes back 0644 with its content
    correct - a change git can see, and one ``[verify]
    dead_code_cleanup``'s ``git add -A`` can commit - and a 0600 one comes
    back loosened with git seeing nothing at all.
    :func:`_restore_mutated_sources` puts these back.

    A target that cannot be stat'ed is dropped rather than raised on: it
    is a file that vanished between Layer 1 measuring it and this check
    starting, so mutmut cannot mutate it either, and the run's own
    ``command_failed`` sidecar is where that is reported. Dropping it here
    also drops its ``.bak`` restore, which is correct for the same reason:
    a file that is not there has no backup beside it.

    ``except OSError`` exactly. :meth:`Path.stat` raises nothing else here,
    and nothing in this function parses a document, so CLAUDE.md's
    ``tomllib`` rule (catch ``Exception``) does not apply.
    """
    modes: dict[str, int] = {}
    for path in paths:
        try:
            modes[path] = stat.S_IMODE((cwd / path).stat().st_mode)
        except OSError:
            continue
    return modes


def _restore_mutated_sources(cwd: Path, modes: Mapping[str, int]) -> None:
    """Restore every ``<path>.bak`` mutmut wrote over its target, and the
    target's original permission bits.

    Safe ONLY because :func:`_preexisting_backups` already refused when a
    ``.bak`` was there before the run (D7): every ``.bak`` this function
    sees is therefore one mutmut wrote, and it is byte-identical to the
    file as mutmut found it (``mutate_file`` writes the backup before the
    mutation - measurements 2h), but NOT mode-identical: mutmut's own
    ``open(path + '.bak', 'w')`` and ``shutil.move`` restore lose the
    mode, and so does kstrl's own :func:`os.replace` on the timed-out
    path. :func:`os.replace`, never ``git checkout --``: the ``.bak`` is
    right whether or not the engineer's own work in this file is
    committed.

    Measured (2h): SIGTERM to the process group - what
    :func:`run_scrubbed` sends first - leaves the source file MUTATED
    with its ``.bak`` beside it, because mutmut writes the backup before
    it writes the mutation. Without this restore, the cap firing hands
    ``[verify] dead_code_cleanup``'s ``git add -A`` a mutant to commit.

    The chmod is UNCONDITIONAL, outside the ``.bak`` branch, because the
    two paths lose the mode in different places: a run that finished
    leaves no ``.bak`` at all and mutmut's own ``shutil.move`` already
    dropped the mode, while a run the cap killed leaves one and the
    ``os.replace`` above drops it. One restore covers both
    (``test_the_mode_of_a_mutated_file_survives_the_run`` pins both ids;
    moving the chmod into the branch leaves ``[cap-fired]`` green and only
    ``[mutmut-restored-it]`` red, which is plant P4).

    No cache delete here, no glob, no ``Path.rglob("*.bak")`` - only the
    exact targets this check chose.

    SYMLINK IDENTITY (#152 simplify pass, C1) is DECLINED here rather
    than defended, and this paragraph is that decline, not silence.
    ``os.replace`` swaps the directory entry, so if ``target`` were a
    symlink, replacing it over ``bak`` would turn a shared link into a
    plain file - ``kstrl/atomicio.py``'s module docstring documents the
    identical property for the atomic-write helper, and
    ``init_cmd._rewrite_blockers`` refuses to rewrite a symlinked
    ``prompt.md`` for exactly this reason. This function does not, for
    two reasons neither of which is "it cannot happen": mutmut itself
    reads ``context.filename`` with a plain ``open()`` and would mutate
    THROUGH a symlinked target the same way, so a symlink identity loss
    here is downstream of one mutmut already risks, not one this
    function introduces; and :func:`_preexisting_backups` already
    refuses the one shape that would make restoring a symlink actively
    destructive (a ``.bak`` already on disk). Refusing on
    ``target.is_symlink()`` in :func:`_diff_mutation_preflight` instead
    is the shape that would close it, mirroring ``_rewrite_blockers``,
    and is recorded as not built in this round: no fixture in this repo
    constructs a symlinked mutation target, so a refusal here would be
    unmeasured code guarding an unmeasured scenario.
    """
    for path, mode in modes.items():
        target = cwd / path
        bak = cwd / f"{path}.bak"
        if bak.exists():
            os.replace(bak, target)
        try:
            current = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            continue
        if current != mode:
            target.chmod(mode)


def _mutmut_run_spawn(
    cwd: Path, check: str, command: list[str], cap: float, paths: Sequence[str]
) -> NotMeasured | None:
    """Run mutmut and restore the tree. ``None`` means the run left a
    cache worth reading; anything else is a sidecar (#391).

    ``result`` is ``None`` on the timeout path, so the fatal-exit check
    below reads ``result is not None and result.returncode & 1`` rather
    than ``result.returncode`` directly - the latter is an
    ``UnboundLocalError`` there, which would surface as a Phase-1
    traceback instead of a sidecar.

    ``result.returncode & 1``, never ``result.returncode != 0`` (D11,
    measured via ``uvx mutmut@2.5.1 run --help``): mutmut's exit code is
    a bit-OR of independent outcomes - 2 for survivors, 4 for timeouts, 8
    for a slow suite - and only bit 1 is a FATAL error. Testing ``!= 0``
    would read a run with survivors (exit 2, a real and useful
    measurement) as a failure.

    The ``.bak`` restore runs in a ``finally`` around the mutation spawn
    ALONE (measurements 2h: the timeout path is the one that leaves a
    mutant on disk; the happy path cleans up after itself and the
    restore there is then a no-op).

    The targets' permission bits are captured here, before any spawn,
    because mutmut has already changed them by the time this function
    could read them again - the happy path leaves no ``.bak`` at all and
    mutmut's own ``shutil.move`` has already dropped the mode by then.

    D4 (#391): a truncated run is ``timed_out``, never a score -
    :data:`_MUTMUT_CANNOT_REPORT_TRUNCATED` (measurements 2d). The caller
    must not follow a ``timed_out`` (or any other) gap with a report
    spawn.
    """
    modes = _target_modes(cwd, paths)
    result: subprocess.CompletedProcess[str] | None = None
    timed_out = False
    try:
        result = run_scrubbed(command, cwd=cwd, timeout=cap)
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exc:
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"the mutation command could not be started: {exc}",
        )
    except ChildOutputDecodeError as exc:
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"the mutation run's output could not be decoded: {exc}",
        )
    finally:
        _restore_mutated_sources(cwd, modes)
    if timed_out:
        return NotMeasured(
            check,
            NOT_MEASURED_TIMED_OUT,
            f"the {cap:.0f}s [verify] mutation_timeout cap fired, and "
            + _MUTMUT_CANNOT_REPORT_TRUNCATED,
        )
    if result is not None and result.returncode & 1:
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"mutmut run exited {result.returncode}: {_last_output_line(result)}",
        )
    return None


#: The entire fail-closed guarantee behind D4 (#391 simplify pass, A3):
#: with this flag, an un-run mutant makes ``mutmut junitxml`` raise
#: (measured: ``ValueError: Obtained null mutant``) instead of silently
#: rendering as a bare ``<testcase>`` - indistinguishable from a killed
#: one. Under any OTHER untested policy that render is exactly what
#: happens, which is the 100%-on-8-of-11 defect this PR measured and
#: fixed. D4's own control flow (:func:`_mutmut_run_spawn`'s ``timed_out``
#: branch) only ever catches the truncation KSTRL causes, keyed on its
#: own cap; a truncated cache reaching this spawn by any OTHER route -
#: an operator's own ``Ctrl-C``, an OOM kill - is caught by this flag
#: alone. Deleting it leaves the whole suite green (measured; see plant
#: P1), because no earlier version of this file asserted the report
#: spawn's own argv.
_UNTESTED_POLICY_ERROR = "--untested-policy=error"

#: The operator-facing half of D4's rule (#391 simplify pass, C4): why a
#: fired cap can never be scored. Written once; the docstrings below
#: that explain the DECISION point at this sentence instead of each
#: repeating it.
_MUTMUT_CANNOT_REPORT_TRUNCATED = (
    "mutmut 2.5.1 cannot report a truncated run: its junitxml raises "
    "ValueError: Obtained null mutant under --untested-policy=error, and "
    "under any other policy an un-run mutant renders exactly like a "
    "killed one"
)


def _mutmut_report_spawn(cwd: Path, check: str) -> str | NotMeasured:
    """``mutmut junitxml``'s stdout, or the sidecar naming why not.

    ``_UNTESTED_POLICY_ERROR`` is the whole fail-closed guarantee - see
    its own comment.
    """
    report: subprocess.CompletedProcess[str] | None = None
    report_error = ""
    try:
        report = run_scrubbed(
            ["mutmut", "junitxml", _UNTESTED_POLICY_ERROR, "--suspicious-policy=error"],
            cwd=cwd,
            timeout=_MUTATION_REPORT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError, ChildOutputDecodeError) as exc:
        report_error = str(exc)
    if report is None:
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"the mutation report could not be read: {report_error}",
        )
    if report.returncode != 0:
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"mutmut junitxml exited {report.returncode}: {_last_output_line(report)}",
        )
    return report.stdout


def _reported_lines(mutants: Iterable[Mutant], paths: Sequence[str]) -> dict[str, set[int]]:
    """Whole-file scope's target set (#391, D2): every line mutmut
    reported a mutant on, in the files kstrl named and no others.
    mutmut's own scoping is still not trusted for WHICH FILES (D2's
    rule); it is trusted for which lines inside a file it can mutate,
    which is what whole-file scope means."""
    allowed = set(paths)
    targets: dict[str, set[int]] = {}
    for mutant in mutants:
        if mutant.path in allowed:
            targets.setdefault(mutant.path, set()).add(mutant.line)
    return targets


def _mutmut_measure(
    cwd: Path,
    check: str,
    tokens: list[str],
    paths: Sequence[str],
    patch_lines: Mapping[str, Collection[int]] | None,
    cap: float,
) -> MutationScore | NotMeasured:
    """One mutmut run, scored (#391). The ONE driver: both mutation
    checks reach mutmut through here, so the cache lifecycle, the
    ``.bak`` restore, the mode restore, the exit-code reading and the
    report parse cannot disagree between them. ``patch_lines`` is the
    target selector and the only axis they differ on: a line set becomes
    a synthetic ``--use-patch-file``, and ``None`` means whole-file
    scope, whose target set is derived from the report's own rows
    restricted to ``paths``.

    ``.mutmut-cache`` has no relocation knob (``cache.init_db``
    hard-codes ``os.path.join(os.getcwd(), '.mutmut-cache')``), so a
    stale cache from an earlier run would be read as this run's
    inventory unless it is gone before mutmut starts. The delete stays
    in exactly two places and in this order: BEFORE the run, and in a
    ``finally`` AFTER the report spawn - never fused, and never removed
    between them, because the report spawn reads that cache. ``gap is
    not None`` is what stops the report spawn from following a fired
    cap (D4): the run's own gap, not a fresh call, becomes ``report``.
    """
    with tempfile.TemporaryDirectory(prefix="kstrl-mutation-") as tmp_name:
        tmp = Path(tmp_name)
        tests_dir = tmp / "empty-tests"
        tests_dir.mkdir()
        patch_path: Path | None = None
        if patch_lines is not None:
            patch_path = tmp / "targets.diff"
            atomic_write_text(patch_path, mutation_patch(patch_lines))
        command = _mutation_run_command(tokens, paths, patch_path, tests_dir)
        (cwd / ".mutmut-cache").unlink(missing_ok=True)
        try:
            gap = _mutmut_run_spawn(cwd, check, command, cap, paths)
            report = gap if gap is not None else _mutmut_report_spawn(cwd, check)
        finally:
            (cwd / ".mutmut-cache").unlink(missing_ok=True)
    if isinstance(report, NotMeasured):
        return report
    try:
        mutants = parse_mutant_report(report)
    except ValueError as exc:
        return NotMeasured(
            check,
            NOT_MEASURED_COMMAND_FAILED,
            f"the mutation report could not be read: {exc}",
        )
    targets: Mapping[str, Collection[int]] = (
        patch_lines if patch_lines is not None else _reported_lines(mutants, paths)
    )
    return score_mutants(targets, mutants)


def _diff_mutation_preflight(
    cwd: Path, coverage: PatchCoverage, test_command: str | None
) -> tuple[list[str], dict[str, set[int]]] | NotMeasured:
    """Every refusal :func:`check_diff_mutation` can make before any
    spawn: ``(tokens, targets)`` on success, or the sidecar naming why
    not.

    In cost order: ``test_command`` must be a single pytest invocation
    and mutmut must be on PATH (:func:`_mutmut_tool_preflight`, shared
    with Layer 1 since #391 - config validation already refuses
    ``diff_mutation = true`` with ``patch_coverage = false``, so both
    checks tokenise the SAME ``[verify] test_command``);
    ``coverage.covered_lines`` - the changed-and-covered set Layer 1
    measured - must be non-empty; then :func:`_mutmut_tree_preflight`
    (also shared): no project-level ``mutmut_config.py`` (the only route
    to mutmut's ``skipped`` status, which renders identically to a
    killed mutant in the junitxml report - measurements 2g), and D7 step
    1's pre-existing-``.bak`` refusal, which is the LAST pre-flight and
    still costs no spawn and no temp dir - kstrl cannot tell a backup
    already on disk from one mutmut is about to write, and refuses
    rather than risk overwriting the project's file.
    """
    tokens = _mutmut_tool_preflight(DIFF_MUTATION_CHECK, "[adequacy] diff_mutation", test_command)
    if isinstance(tokens, NotMeasured):
        return tokens
    targets: dict[str, set[int]] = {
        path: set(lines) for path, lines in coverage.covered_lines if lines
    }
    if not targets:
        return NotMeasured(
            DIFF_MUTATION_CHECK,
            NOT_MEASURED_NO_TARGET,
            "the diff added no line that is both changed and covered",
        )
    gap = _mutmut_tree_preflight(cwd, DIFF_MUTATION_CHECK, sorted(targets))
    if gap is not None:
        return gap
    return tokens, targets


def _diff_mutation_score_result(score: MutationScore, start: float) -> CheckResult | NotMeasured:
    """Turn a scored mutmut run into a row or a sidecar.

    ``score.measured_lines == 0`` is never a score - it is
    :func:`_no_measured_lines` (#391, shared with Layer 1):
    ``no_mutants`` when mutmut reported no mutant on any target line at
    all, and ``command_failed`` for the remaining case (mutants reported
    and none with a definite status). D4 (#391) deleted the ``timed_out``
    branch that used to live here: a fired cap never reaches this
    function at all now - :data:`_MUTMUT_CANNOT_REPORT_TRUNCATED`
    (measurements 2d) - the report spawn does not follow a fired cap, so
    ``_mutmut_measure`` returns the ``timed_out`` gap directly.

    Otherwise builds the PASSING row: ``sampled`` now has ONE cause
    (D4) - fewer lines were measured than mutmut reported a mutant for -
    where it used to have two (the cap firing was the other). D4's
    selection rule already decided ``score`` before this is called; this
    only renders it.

    ``sampled`` also reaches the finding as a TAG (#152 simplify pass,
    A3), not only as the ``[sampled: ...]`` substring on the headline: a
    reader that wants to set a floor from the recorded distribution must
    be able to separate a sampled score from a complete one without
    parsing prose, and ``Finding.tags`` is where every other structured
    fact about a finding already lives. The prose stays; this adds a
    second, machine-readable place the same fact is true.
    """
    if score.measured_lines == 0:
        return _no_measured_lines(DIFF_MUTATION_CHECK, score)

    percent = 100.0 * score.killed_lines / score.measured_lines
    sampled = score.measured_lines < score.mutable_lines
    headline = (
        f"diff-scoped mutation {percent:.1f}% "
        f"({score.killed_lines}/{score.measured_lines} changed+covered lines "
        f"whose first mutant was killed)"
    )
    if sampled:
        headline += (
            f" [sampled: {score.measured_lines} of {score.target_lines} "
            "changed+covered lines measured]"
        )
    details = [
        f"{score.target_lines} changed+covered line(s) targeted; "
        f"{score.mutable_lines} produced a mutant; {score.measured_lines} measured"
    ]
    details.extend(f"survived: {path}:{line}" for path, line in score.survivors)
    suggestion = "Advisory only: nothing blocks."
    if score.survivors:
        suggestion += " Surviving lines are concrete test targets: " + "; ".join(
            f"{path}:{line}" for path, line in score.survivors
        )
    return CheckResult(
        name=DIFF_MUTATION_CHECK,
        passed=True,
        message=f"{headline} [advisory]",
        details=details,
        findings=[
            Finding.adequacy_finding(
                category="diff_mutation",
                explanation=headline + ": " + "; ".join(details),
                severity="advisory",
                suggestion=suggestion,
                extra_tags=("sampled",) if sampled else (),
            )
        ],
        duration_seconds=time.monotonic() - start,
    )


def check_diff_mutation(
    cwd: Path,
    coverage: PatchCoverage,
    test_command: str | None,
    cap: float,
) -> CheckResult | NotMeasured:
    """R8.5 Layer 2 (#152): mutate the changed AND covered lines this
    diff added, and report what fraction the suite detects a change to.

    No ``base_branch`` parameter: the target set comes from ``coverage``
    - Layer 1's own measurement (:func:`_patch_coverage_checks`) - not
    from a second git read.

    Split into two named steps, each responsible for one register:
    :func:`_diff_mutation_preflight` owns every refusal that costs no
    spawn (``tool_missing``, ``no_target``, and D7's ``command_failed``
    pre-flights); :func:`_mutmut_measure` (#391, shared with Layer 1) owns
    the run, the report spawn and the parse - ``command_failed`` for a
    spawn that failed to start, exited fatally, or could not be parsed,
    and ``timed_out`` for a fired cap; :func:`_diff_mutation_score_result`
    owns ``no_mutants`` and the remaining ``command_failed`` case (mutants
    reported, none with a definite status). ``read_only`` is owned by
    :func:`_diff_mutation_checks` and never reaches here at all - mutmut
    rewrites the files it mutates.

    ADVISORY ALWAYS: there is no floor key, and no autonomy level reads
    this check. The wall clock is bounded by ``cap`` for the mutation run
    plus at most :data:`_MUTATION_REPORT_TIMEOUT` for the report spawn -
    except on a fired cap, where D4 (#391) means no report spawn follows.

    D4's selection rule, in one sentence: each target line's verdict is
    its LOWEST-id mutant among those with a killed-or-survived status.
    """
    start = time.monotonic()
    preflight = _diff_mutation_preflight(cwd, coverage, test_command)
    if isinstance(preflight, NotMeasured):
        return preflight
    tokens, targets = preflight
    score = _mutmut_measure(cwd, DIFF_MUTATION_CHECK, tokens, sorted(targets), targets, cap)
    if isinstance(score, NotMeasured):
        return score
    return _diff_mutation_score_result(score, start)


def _diff_mutation_checks(
    cwd: Path,
    config: VerifyConfig,
    adequacy_config: AdequacyConfig | None,
    coverage: PatchCoverage | None,
    coverage_gap: NotMeasured | None,
    coverage_duration: float,
    *,
    test_suite_passed: bool,
    read_only: bool,
) -> tuple[list[CheckResult], list[NotMeasured]]:
    """``(rows, gaps)`` for R8.5 Layer 2, diff-scoped mutation (#152).

    BOTH switches are read, for the reason :func:`_patch_coverage_checks`
    gives: a check nobody asked for records nothing at all.

    ``read_only=True`` (``ks sense``) is a gap here, the OPPOSITE of
    Layer 1's D8: mutmut REWRITES the file it mutates, so this cannot run
    read-only at all, while Layer 1's coverage run writes nothing into
    the tree and does run under ``ks sense``.

    ``coverage is None`` means Layer 1 either was never asked to run or
    ran and produced no measurement; config validation
    (``AdequacyConfig.__post_init__``) already refuses
    ``diff_mutation = true`` with ``patch_coverage = false``, so this
    branch is only ever Layer 1 having run and gapped, and ``coverage_gap``
    is therefore never ``None`` here either - Layer 1 having run and
    PRODUCED a measurement is exactly the ``coverage is not None`` branch
    below. The reason is INHERITED from Layer 1's own gap (#152 simplify
    pass, B1: ``coverage_gap`` is now the single outcome
    :func:`_patch_coverage_checks` returns, not a list this function
    scanned for it, and there is no guessed fallback for the case that
    invariant already rules out) rather than reported as a fresh
    ``no_target``: Layer 1 gapping for ``tool_missing`` or
    ``command_failed`` is not "no target", and reporting it as one would
    tell the operator the diff was empty when pytest-cov was actually
    missing. Every value ``reason`` can take here is one of the six
    existing ``NOT_MEASURED_*`` tokens; this invents no vocabulary.

    Two more refusals sit here, both BEFORE any mutmut spawn and both
    added in the #152 simplify pass over a real run on kstrl itself
    (Group A):

    - ``test_suite_passed=False`` (A2): the ``[verify] test_suite`` row
      already in ``checks`` failed. mutmut's own baseline is "run the
      suite once before mutating anything", so spawning it over a
      failing suite runs the suite a THIRD time (once for the test
      check, once under Layer 1's coverage) only to raise "Tests don't
      run cleanly without mutations" and abort - a wasted 457s run on
      this repo, measured. :func:`_mutation_checks` carries the
      byte-for-byte identical guard since the #391 simplify pass on PR
      #392: reaching mutmut through one shared driver made the missing
      guard on that side the only remaining asymmetry.
    - ``coverage_duration >= config.mutation_timeout`` (A1): Layer 1's
      OWN measured coverage-run duration already meets or exceeds the
      cap this check's mutation run would be bounded by. mutmut always
      pays its baseline test-suite run in full before mutating a single
      line (``_remove_mutation_cache`` - now inlined into
      :func:`_mutmut_measure` - deletes ``.mutmut-cache`` before every
      run, so the cache-hit early return ``time_test_suite`` offers is
      unreachable), and that baseline is the SAME suite Layer 1 just
      ran under coverage - so a baseline that takes at least as long as
      the cap has already been measured to certainly exhaust it before
      a single mutant runs. No invented ratio: this compares the two
      measured durations directly, never a tightened factor guessed
      without a second repository's numbers beside kstrl's own.
      ``config.mutation_timeout`` is unchanged here: this check runs
      FIRST in the phase (see :func:`run_mechanical_verification`) and
      gets the full budget; :func:`_mutation_checks`, running second,
      gets what THIS check's own run actually left of it (#391 simplify
      pass on PR #392, A2 - see that function's docstring).
    """
    if adequacy_config is None or not (adequacy_config.enabled and adequacy_config.diff_mutation):
        return [], []
    if read_only:
        return [], [
            NotMeasured(
                DIFF_MUTATION_CHECK,
                NOT_MEASURED_READ_ONLY,
                _MUTMUT_READ_ONLY_DETAIL,
            )
        ]
    if coverage is None:
        # Never a guessed default here (#152 simplify pass, B1): the
        # config-validated invariant this docstring states above makes
        # `coverage_gap is None` in this branch unreachable, so an
        # `assert` states that rather than a fallback silently guessing
        # `no_target` for a state that never occurs.
        assert coverage_gap is not None, (
            "Layer 1 measured no coverage but recorded no gap; "
            "AdequacyConfig.__post_init__ should have refused "
            "diff_mutation=true with patch_coverage=false before this ran"
        )
        reason = coverage_gap.reason
        return [], [
            NotMeasured(
                DIFF_MUTATION_CHECK,
                reason,
                "R8.5 Layer 1 produced no coverage measurement "
                f"({reason}), so Layer 2 has nothing to mutate",
            )
        ]
    if not test_suite_passed:
        return [], [
            NotMeasured(
                DIFF_MUTATION_CHECK,
                NOT_MEASURED_COMMAND_FAILED,
                "[verify] test_suite already failed; mutmut's own baseline run "
                "would only run the suite a third time to abort with 'Tests "
                "don't run cleanly without mutations', so Layer 2 refuses "
                "before spending anything",
            )
        ]
    if coverage_duration >= config.mutation_timeout:
        return [], [
            NotMeasured(
                DIFF_MUTATION_CHECK,
                NOT_MEASURED_TIMED_OUT,
                f"R8.5 Layer 1's own coverage run already took "
                f"{coverage_duration:.0f}s, at or beyond the "
                f"{config.mutation_timeout:.0f}s [verify] mutation_timeout cap "
                "this check's mutation run would share; mutmut always pays "
                "that same suite's baseline in full before mutating a single "
                "line, so it would certainly exhaust the cap before "
                "measuring anything, and Layer 2 refuses before spending it",
            )
        ]
    outcome = check_diff_mutation(cwd, coverage, config.test_command, config.mutation_timeout)
    if isinstance(outcome, NotMeasured):
        return [], [outcome]
    return [outcome], []


def _ruff_dead_code_command(read_only: bool) -> str:
    """The ruff invocation for the phase, in each of its two modes.

    ``--no-fix`` is explicit rather than implied by omitting ``--fix``: a
    project can set ``fix = true`` under ``[tool.ruff]``, which turns a
    bare ``ruff check`` into a fixing run. ``--no-cache`` so not even
    ``.ruff_cache`` appears in a tree kstrl was asked only to measure.

    ``--output-format=concise`` for the same reason as ``--no-fix``, and
    it is the load-bearing one: :func:`_ruff_count` reads a summary line
    that only the text formats print, and ``[tool.ruff] output-format =
    "json"`` in the measured project's own ``pyproject.toml`` removes
    it. Measured on ruff 0.16.1, the flag beats both that key and
    ``RUFF_OUTPUT_FORMAT`` in the environment, and concise keeps the
    ``Found N errors``, ``(M fixed, K remaining)`` and ``[*] N fixable``
    lines this phase parses (#335 round 2).
    """
    if read_only:
        return "ruff check --no-fix --no-cache --output-format=concise --select F401,F811,F841 ."
    return "ruff check --fix --output-format=concise --select F401,F811,F841 ."


#: ``Found 3 errors (2 fixed, 1 remaining).`` - a fixing run's summary.
_RUFF_FIXED = re.compile(r"Found \d+ errors? \((\d+) fixed, (\d+) remaining\)")
#: ``[*] 2 fixable with the `--fix` option.`` - what a --no-fix run WOULD
#: remove. Older ruff says "potentially fixable".
_RUFF_FIXABLE = re.compile(r"\[\*\] (\d+) (?:potentially )?fixable")
#: ``Found 3 errors.`` with no ``(M fixed, K remaining)`` beside it and no
#: ``[*]`` line beneath it: findings exist and ruff removed none of them,
#: because every fix it has for them is unsafe. Printed in BOTH modes.
_RUFF_FOUND = re.compile(r"Found (\d+) errors?")
#: What ruff 0.3.3 and later print when they have nothing to report.
#: 0.2.0 through 0.3.2 print nothing at all instead; see
#: :func:`_ruff_said_nothing`.
_RUFF_CLEAN = "All checks passed!"
#: ruff's advice lines, which are not findings: ``warning: No Python
#: files found under the given path(s)`` is the whole output of a run
#: over a tree with no Python file in it, on every version measured.
_RUFF_WARNING = "warning:"


def _ruff_said_nothing(output: str) -> bool:
    """Ruff printed no diagnostic, no summary and no error, only advice.

    A shape, not a fallback. Measured with this phase's exact flags:
    ruff 0.2.0, 0.3.0 and 0.3.2 print NOTHING on a clean tree, in either
    mode, exiting 0; 0.3.3 onwards print ``All checks passed!`` there.
    On a tree with no Python file in it, 0.2.0 and 0.3.0 print the
    ``warning:`` line alone and 0.4.0 and 0.16.1 print it beside ``All
    checks passed!``. So the empty output is real ruff on a real tree
    across the whole supported range's lower half, and reading it as
    unrecognisable turned a healthy clean run into a ``command_failed``
    gap (#335 round 3).

    The caller pairs this with ``returncode == 0``, which is what makes
    it a measurement rather than a guess: exit 0 is ruff saying it
    finished with nothing left to report, and every measured run that
    fixed something printed ``Found N errors (M fixed, K remaining).``
    even while exiting 0.
    """
    return not [
        line
        for line in output.splitlines()
        if line.strip() and not line.strip().startswith(_RUFF_WARNING)
    ]


def _ruff_count(output: str, *, read_only: bool, returncode: int) -> int | None:
    """How many findings ruff removed, or would remove, or ``None``.

    Two different numbers off two different lines, because the two modes
    print different things: a fixing run reports ``Found 3 errors (2
    fixed, 1 remaining).`` and a ``--no-fix`` run reports ``Found 3
    errors.`` followed by ``[*] 2 fixable with the `--fix` option.``
    Last match wins in the fixing case, which is what the fused function
    did.

    ``None`` means the output matched NO shape this function knows, and
    it is the reason this returns an optional at all. The version before
    it returned 0 there, and 0 is also what "ruff removed nothing" says,
    so an output kstrl could not read was reported as a clean auto-fix
    phase - the defect class this whole change exists to close, in the
    gate that closes it. Measured with ``output-format = "json"`` set in
    a project's own config: ruff exits 1 with two unused imports found,
    the summary line is absent, and the row read ``dead_code_ruff pass
    ruff auto-fixed 0`` over a worktree ruff had just edited and nothing
    had committed. The caller turns ``None`` into a ``command_failed``
    gap. Recognition is POSITIVE - a known shape or nothing - so a
    future ruff that renames its summary produces a gap rather than a
    fabricated zero.

    The read-only number is the ``[*]`` count, not the ``Found`` count.
    They differ by the unsafe fixes: on one measured tree ``Found 3
    errors.`` sat above ``[*] 2 fixable``, and the fixing run on the
    same tree removed 2. Reading ``Found`` there made ``ks sense`` and
    the factory report different numbers for the same tree while the
    message called them "auto-removable" (#335 round 2).

    The set of shapes is MEASURED, not remembered, and
    ``tests/test_verify.py::RUFF_SHAPES`` is that measurement: every
    entry is a captured stdout-plus-stderr from a real ruff run with
    this phase's exact flags, tagged with the version that printed it,
    across ruff 0.2.0, 0.3.0 and 0.16.1 and five trees. Round 2 of #335
    recognised the fixing mode's ``Found N errors.`` nowhere, so a tree
    whose only findings have unsafe fixes - one ``x = do_thing()`` an
    agent left behind is enough - reported a healthy ruff run as a tool
    failure, and a clean tree on ruff 0.2.0 did the same (#335 round 3).
    """
    if returncode == 0 and _ruff_said_nothing(output):
        return 0
    if read_only:
        fixable = _RUFF_FIXABLE.search(output)
        if fixable:
            return int(fixable.group(1))
        if _RUFF_FOUND.search(output):
            return 0
        return 0 if _RUFF_CLEAN in output else None
    fixed = _RUFF_FIXED.findall(output)
    if fixed:
        return int(fixed[-1][0])
    if _RUFF_FOUND.search(output):
        return 0
    return 0 if _RUFF_CLEAN in output else None


def _ruff_remaining(output: str) -> int:
    """Findings a fixing run located and did not remove.

    Decoration, never a gate: it reads the same two lines
    :func:`_ruff_count` reads, and 0 when neither is present, because by
    then :func:`_ruff_count` has already refused an output it could not
    read. It is in the message because ``ruff auto-fixed 0`` cannot
    otherwise tell a clean tree from a tree with three unsafe-fix
    ``F841``s - which is exactly the tree that reaches the ``Found N
    errors.`` branch here, so without it the whole shape B1 of round 3
    restored would report the same string as a clean run.
    """
    remaining = _RUFF_FIXED.findall(output)
    if remaining:
        return int(remaining[-1][1])
    found = _RUFF_FOUND.search(output)
    return int(found.group(1)) if found else 0


def _commit_ruff_fixes(cwd: Path) -> str | None:
    """Stage and commit what ruff removed, so later checks see a clean tree.

    Everything EXCEPT the state directory (#274 review). Under
    ``use_worktrees=False`` this runs with ``cwd`` at the project root,
    so a bare ``git add -A`` commits kstrl's own live ``.kstrl/``
    journals onto the component branch the moment ruff fixes one
    finding - and ``check_diff_scope`` is deliberately un-carved, so the
    next pass fails on them and they ride into the PR. In a worktree the
    same exclusion is wanted for the opposite reason: a ``.kstrl/``
    there is the agent's, and this must not commit it on the agent's
    behalf.

    A list, not a string: ``run_scrubbed`` only shells out for a string,
    and the ``:(exclude)`` pathspec must reach git unmangled.

    Returns ``None`` when the commit landed and a short reason when it
    did not, and the caller says so in the row. Neither status used to
    be read: ``run_scrubbed`` returns a ``CompletedProcess`` and never
    raises on a non-zero exit, so a commit refused by a repo hook, or
    aborted for want of a ``user.email``, left ruff's deletions on disk
    and uncommitted while the row said ``ruff auto-fixed 2``. That is
    the shape of the defect this change exists to remove, one step
    further in: a step that did not happen, reported as done. The
    earlier version's excuse, that the next check would notice, does not
    hold - ``check_diff_scope``, ``check_bad_patterns`` and
    ``check_test_adequacy`` all run BEFORE this point in
    :func:`run_mechanical_verification` (#335 round 2).

    A timeout is a reason like the other two rather than a silent pass,
    for the extra reason that a timeout on ``git add`` skips the commit
    entirely.
    """
    try:
        staged = run_scrubbed(
            ["git", "add", "-A", "--", ".", f":(exclude){STATE_DIR_NAME}"],
            cwd=cwd,
            timeout=30,
        )
        if staged.returncode != 0:
            return f"git add exited {staged.returncode}: {_last_output_line(staged)}"
        committed = run_scrubbed(
            'git commit -m "chore: auto-remove dead code (ruff F401/F811/F841)"',
            cwd=cwd,
            timeout=30,
        )
        if committed.returncode != 0:
            return f"git commit exited {committed.returncode}: {_last_output_line(committed)}"
    except subprocess.TimeoutExpired:
        return "git add or git commit exceeded 30s"
    except ChildOutputDecodeError as exc:
        return f"git add or git commit output could not be decoded: {exc}"
    return None


def _ruff_fix_message(cwd: Path, output: str, count: int) -> str:
    """The fixing row's message, and the tidying it is allowed to claim.

    Its own function so :func:`check_dead_code_ruff` does not pay three
    branches for the wording of one string, and so the commit and the
    sentence describing the commit cannot drift apart.
    """
    message = f"ruff auto-fixed {count}"
    remaining = _ruff_remaining(output)
    if remaining:
        message += f", {remaining} remaining"
    if count > 0:
        problem = _commit_ruff_fixes(cwd)
        if problem is not None:
            message += f", not committed: {problem}"
    return message


def check_dead_code_ruff(
    cwd: Path,
    timeout: float = 300.0,
    *,
    read_only: bool = False,
) -> CheckResult | NotMeasured:
    """Auto-remove unused imports and locals with ruff F401/F811/F841.

    The first half of what ``check_dead_code`` used to do in one
    function, split out by #335 because one row covering two phases
    reported a pass for whichever of the two had not run.
    :func:`_dead_code_checks` records the full account.

    Always a row when ruff ran AND said what it did, because zero fixes
    is a measurement. Four paths return :class:`NotMeasured` instead,
    and the ``reason`` token separates them:

    - ``tool_missing``: ruff is not on PATH.
    - ``timed_out``: the run exceeded ``timeout``.
    - ``command_failed``: ruff exited outside 0 (clean or fixed) and 1
      (findings). Measured on ruff 0.16.1, 2 is a configuration error;
      anything else is a tool that did not complete. This is the path
      that most needed splitting out - a bad ``ruff.toml`` printed no
      count, parsed to zero fixes, and read as a clean auto-fix phase.
    - ``command_failed`` again, for an exit inside 0 and 1 whose output
      holds no shape :func:`_ruff_count` knows. Round 1 of this change
      left that one open and it was the same defect one field over: an
      unreadable output parsed to zero, which is also what "removed
      nothing" says. The same token because the event is the same, a
      ruff run that produced no measurement, and because a new reason
      token would be new status vocabulary.

    ``--output-format=concise`` puts a FLOOR under this phase at ruff
    **0.2.0** (January 2024). Measured: 0.0.272 rejects
    ``--output-format`` outright, 0.1.0 and 0.1.15 take the flag and
    reject the value, and all three exit 2, so an older ruff on PATH
    reaches the ``command_failed`` gap above rather than a wrong number.
    The gap carries ruff's own diagnosis because
    :func:`_tool_failure_line` reads the first ``error:`` line and not
    the last one, which is ``For more information, try '--help'.``
    A project's own pinned ruff is far past 0.2.0; the reachable case is
    ``ks sense`` against a live checkout with a system-wide old ruff
    first on PATH (#335 round 3).

    ``read_only=True`` (``ks sense``, R10.1) runs the SAME rule set with
    ``--no-fix`` and reports what the factory WOULD have removed instead
    of removing it. Nothing is edited, staged or committed. The factory
    owns the worktree it verifies, so editing and committing there is
    free; ``ks sense`` runs against the operator's live checkout, where
    a ``git add -A`` sweeps in every unrelated untracked file and the
    commit moves their HEAD.

    One divergence worth naming: the factory deletes the ruff-fixable
    subset before the detector in :func:`check_dead_code` looks, so that
    scan sees a cleaner tree than a read-only run does. A tree whose
    only dead code is ruff-fixable can therefore fail there and pass
    inside the factory. That is the tree being reported honestly, not a
    bug - but it is a difference.
    """
    start = time.monotonic()

    if not shutil.which("ruff"):
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_TOOL_MISSING,
            "ruff is not on PATH, so no unused import or local was looked for",
        )

    try:
        result = run_scrubbed(_ruff_dead_code_command(read_only), cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_TIMED_OUT,
            f"ruff exceeded [verify] subprocess_timeout of {timeout}s",
        )
    except ChildOutputDecodeError as exc:
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"ruff's output could not be decoded: {exc}",
        )

    if result.returncode not in (0, 1):
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"ruff check exited {result.returncode}: {_tool_failure_line(result)}",
        )

    output = result.stdout + result.stderr
    count = _ruff_count(output, read_only=read_only, returncode=result.returncode)
    if count is None:
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"ruff check exited {result.returncode} and printed no count line: "
            f"{_last_output_line(result)}",
        )
    if read_only:
        message = f"ruff reports {count} auto-removable, not removed"
    else:
        message = _ruff_fix_message(cwd, output, count)
    return CheckResult(
        name=DEAD_CODE_RUFF_CHECK,
        passed=True,
        message=message,
        duration_seconds=time.monotonic() - start,
    )


def _dead_code_command(
    cwd: Path,
    base_branch: str,
    command: str | None,
) -> str | list[str] | NotMeasured:
    """What :func:`check_dead_code` should run, or why it cannot run.

    Its own function so the check does not pay a branch for a choice
    made before anything executes, and so the two reasons there is
    nothing to run are separated at the point where they are known
    rather than reconstructed later.

    A user-supplied ``command`` wins outright and is run as given, and
    as a STRING: it is the operator's own program, in the same category
    as ``test_command``, so it is their shell line and their quoting.

    vulture's is an argv LIST, which ``run_scrubbed`` runs without a
    shell. The names in it come from ``git diff --name-only`` over a
    diff an agent wrote, which is the least trusted input in the
    factory, and they used to be interpolated into a shell string: a
    changed file called ``my file.py`` split into two arguments vulture
    could not find and FAILED the component for the wrong reason, and
    one called ``$(id).py`` executed (#335 round 2).

    The paths go LAST and behind ``--``, which is the other half of that
    same threat model: an argv list stops ``/bin/sh`` reading the names,
    and ``--`` stops vulture's own parser reading them. Measured on
    vulture 2.16 over a directory holding ``a.py`` and ``-v.py``:
    ``vulture -v.py a.py --min-confidence 80`` exits 2 with
    ``vulture: error: unrecognized arguments: -.py``, which
    :func:`check_dead_code` used to report as a dead-code FAIL naming
    the wrong cause, and ``vulture --min-confidence 80 -- -v.py a.py``
    exits 0 (#335 round 3).
    """
    if command:
        return command
    if not shutil.which("vulture"):
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_TOOL_MISSING,
            "vulture is not on PATH and no [verify] dead_code_command is set, "
            "so nothing scanned for dead code",
        )
    py_files = _changed_non_test_python(base_branch, cwd, DEAD_CODE_CHECK)
    if isinstance(py_files, NotMeasured):
        return py_files
    if not py_files:
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_NO_TARGET,
            "the diff changed no non-test Python file, so there was nothing to scan",
        )
    return ["vulture", "--min-confidence", "80", "--", *py_files]


def check_dead_code(
    cwd: Path,
    base_branch: str,
    command: str | None = None,
    timeout: float = 300.0,
) -> CheckResult | NotMeasured:
    """Detect dead code with vulture, or with the operator's own command.

    The second half of the old fused ``check_dead_code`` (#335). It
    scans for what ruff cannot see - unreachable functions, unused
    classes, unused attributes - over the non-test Python files in
    ``git diff <base>...HEAD``, and findings are reported as a FAIL for
    the engineer to fix on retry.

    Returns a row only when a scan happened. Five paths return
    :class:`NotMeasured`, and each ``reason`` token is a different
    event:

    - ``tool_missing``: no ``[verify] dead_code_command`` and no vulture
      on PATH.
    - ``no_target``: the diff changed no non-test Python file. Nothing
      to scan; not a fault.
    - ``command_failed``: git could not read the diff at all, which is a
      fault and is why :func:`_changed_non_test_python` reads strictly.
    - ``timed_out``: the scan exceeded ``timeout``.
    - ``command_failed``: the detector exited non-zero and this check
      could read no finding out of what it printed. Measured on vulture
      2.16: exit 3 is findings, 1 is invalid input and 2 is a bad
      command line, so a non-zero exit with nothing to report is the
      tool failing rather than a clean tree. The decision is on the EXIT
      CODE, not on the output being empty: round 2 of #335 keyed the gap
      on empty output alone, so a detector that exited non-zero and
      printed only lines the ``__all__`` filter drops fell through to
      ``no remaining dead code`` (#335 round 3).

    Every one of them used to be ``CheckResult(passed=True)``. A missing binary
    is not something the engineer's next diff can fix, so none of them
    is a FAIL either: a gap is seen by the operator and gates nothing.

    No ``read_only`` flag, unlike the ruff phase: vulture and an
    operator's own detector read the tree without changing it, so there
    is nothing narrower for this to do.
    """
    start = time.monotonic()

    detect_cmd = _dead_code_command(cwd, base_branch, command)
    if isinstance(detect_cmd, NotMeasured):
        return detect_cmd

    try:
        result = run_scrubbed(detect_cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_TIMED_OUT,
            f"the dead code scan exceeded [verify] subprocess_timeout of {timeout}s",
        )
    except ChildOutputDecodeError as exc:
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"the dead code scan's output could not be decoded: {exc}",
        )

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        # Filter out common false positives (e.g., __all__, __init__).
        real_issues = [
            line
            for line in output.splitlines()
            if line.strip() and not line.strip().startswith("#") and "__all__" not in line
        ]
        if real_issues:
            return CheckResult(
                name=DEAD_CODE_CHECK,
                passed=False,
                message=f"{len(real_issues)} dead code issues remaining",
                details=real_issues[:20],
                duration_seconds=time.monotonic() - start,
            )
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"the dead code scan exited {result.returncode} and reported no finding "
            f"this check could read: {_last_output_line(result)}",
        )

    return CheckResult(
        name=DEAD_CODE_CHECK,
        passed=True,
        message="no remaining dead code",
        duration_seconds=time.monotonic() - start,
    )


def _dead_code_checks(
    cwd: Path,
    base_branch: str,
    config: VerifyConfig,
    *,
    read_only: bool,
) -> tuple[list[CheckResult], list[NotMeasured]]:
    """``(rows, gaps)`` for the dead-code phases: at most one of each, twice.

    The same shape as :func:`_mutation_checks`, for the same reason
    (#306, #335): a check the operator TURNED OFF records nothing at
    all, and a check they turned on that measured nothing records why.
    One toggle, ``[verify] dead_code_cleanup``, still owns both phases -
    splitting the ROW is not splitting the switch.

    Order is load-bearing and is why this is a function rather than two
    calls inline: ruff runs FIRST so the detector scans a tree with the
    ruff-fixable subset already deleted. Reversing it changes what
    vulture reports.
    """
    if not config.dead_code_cleanup:
        return [], []
    ruff_outcome = check_dead_code_ruff(
        cwd,
        config.subprocess_timeout,
        read_only=read_only,
    )
    detect_outcome = check_dead_code(
        cwd,
        base_branch,
        config.dead_code_command,
        config.subprocess_timeout,
    )
    outcomes = (ruff_outcome, detect_outcome)
    return (
        [o for o in outcomes if isinstance(o, CheckResult)],
        [o for o in outcomes if isinstance(o, NotMeasured)],
    )


def _scope_checks(
    cwd: Path,
    base_branch: str,
    *,
    allowed_paths: list[str] | None,
    allowed_paths_error: str | None,
    harness_paths: list[str] | None,
    compare: bool,
) -> list[CheckResult]:
    """The scope checks Phase 1 appends, at most one of two.

    An unreadable scope source and an out-of-scope diff are alternatives
    rather than a check with a mode (#294), so the choice is made once,
    here, instead of inside a check that would then be named for the
    wrong one of them:

    - ``allowed_paths_error`` non-empty: ``scope_unreadable`` alone,
      UNGATED. The comparison is not merely turned off, it is
      unavailable - there is no trustworthy allowlist to compare
      against - so running ``check_diff_scope`` too would report a PASS
      ("no scope constraints") beside the refusal, which is the
      fail-open reading of the same state. The error wins even when a
      caller also supplies a list: a half-loaded state must not be
      judged on paths that may be stale.

      ``is not None``, not truthiness. Both review rounds hit this from
      opposite sides and both were right about the defect: truthiness
      lets an empty-string sentinel PASS a ``diff_scope`` that had no
      allowlist to compare, which is a fail-open in the one check whose
      job is to fail closed; ``is not None`` alone refused while naming
      no cause, rendering the bare "Error: ". Neither problem requires
      the other. This refuses on any non-None value and
      ``check_scope_unreadable`` substitutes
      :data:`NO_CAUSE_RECORDED` for the empty one, so an ambiguous
      sentinel is never read as permission and the refusal always says
      something. ``ComponentScope.resolve`` never produces "", but
      ``run_mechanical_verification`` is a public entry point.
    - otherwise ``diff_scope``, gated on ``compare``, which is
      ``[verify] check_diff_scope`` and nothing else. The one flag
      rather than the whole ``VerifyConfig``: this is the only field
      the decision reads, and the two ``list[str] | None`` arguments
      beside it are keyword-only so a transposition of the authored
      allowlist and the harness carve-out cannot type-check clean.

    Returns a list rather than taking the branch in
    ``run_mechanical_verification``: that function is already over the
    cyclomatic ratchet and is judged against its own previous value, so
    an ``if``/``elif`` there is a refusal at commit time.
    """
    if allowed_paths_error is not None:
        return [check_scope_unreadable(allowed_paths_error)]
    if compare:
        return [
            check_diff_scope(
                cwd,
                base_branch,
                allowed_paths,
                harness_paths=harness_paths,
            )
        ]
    return []


def _mutation_checks(
    cwd: Path,
    base_branch: str,
    config: VerifyConfig,
    coverage_duration: float,
    *,
    test_suite_passed: bool,
    cap: float,
    read_only: bool,
) -> tuple[list[CheckResult], list[NotMeasured]]:
    """``(rows, gaps)`` for mutation testing: at most one of each (#306).

    Every reason mutation testing might not produce a score lives here
    or in :func:`check_mutation_score`, and all of them keep ``rows``
    empty. What differs is ``gaps``, and the difference is the point of
    round 2: a check the operator TURNED OFF records nothing, and a
    check they turned on that measured nothing records why.

    So an absent ``mutation_testing`` row plus an absent gap means
    disabled, and an absent row plus a gap means asked-for and not
    delivered. Seven states, one bit and one token to separate them,
    where before #306 six of the seven produced the same green row.

    ``read_only`` is the one reason that cannot live inside the check.
    mutmut works by rewriting the source files it mutates, so ``ks
    sense`` and :func:`run_undiffed_verification` must not call it at
    all rather than call it and have it decline: a flag threaded into a
    check just to be refused is a flag that can return a row, which is
    how the read-only skip came to report ``passed=True`` in the first
    place.

    A pair rather than mutating two lists the caller owns, and a
    function rather than two branches inline. Measured: inlining the
    toggle and the read-only test at the call site takes
    :func:`run_mechanical_verification` to cyclomatic 10 against a gate
    of 10 and cognitive 15 against a gate of 15.

    The two conditions decidable before anything runs - the toggle and
    ``read_only`` - are exactly the subject of #305 (one object owning
    every argument that decides whether a check can honestly run,
    alongside :data:`DIFF_DEPENDENT_CHECKS` and
    :func:`run_undiffed_verification`). The other four cannot join it:
    mutmut absent, timed out, failed and no mutants are only knowable
    after the check has run.

    Two more refusals, both BEFORE any mutmut spawn, byte-for-byte the
    same shape :func:`_diff_mutation_checks` already had (#391 simplify
    pass on PR #392, group A): this check reaches mutmut through the
    identical shared driver, and #391 is the change that made the two
    ONE driver, so the pre-spend guards were the only asymmetry left
    between them.

    - ``test_suite_passed=False``: ``[verify] test_suite`` already
      failed, and mutmut's own baseline is "run the suite once before
      mutating anything" - spawning it here would run the suite a third
      time only to abort with "Tests don't run cleanly without
      mutations".
    - ``coverage_duration >= cap``: Layer 1's own measured coverage-run
      duration already meets or exceeds ``cap`` - see that parameter's
      own note for what it is here. mutmut always pays its baseline
      test-suite run in full before mutating a single line, and that
      baseline is the SAME suite the coverage run just measured, so a
      cap already at or below that duration would certainly be
      exhausted before a single mutant runs.

    ``cap`` is this check's share of ONE phase-level mutation budget
    (#391 simplify pass on PR #392, A2), not a second copy of ``[verify]
    mutation_timeout``: :func:`run_mechanical_verification` runs R8.5
    Layer 2 first and passes it the FULL ``config.mutation_timeout``,
    then decrements that number by however long Layer 2's own call
    actually took (its wall clock, not only a scored row's
    ``duration_seconds`` - a `timed_out` sidecar still spent the wall
    time) and hands this check what remains. Two independent full-sized
    caps back to back would let the phase's mutation portion cost, per
    side, ``mutation_timeout`` plus the mutation spawn's own
    ``_SCRUB_TERM_GRACE_SECONDS`` wait and post-SIGKILL drain (5s each)
    plus ``_MUTATION_REPORT_TIMEOUT`` (30s) plus that report spawn's own
    matching wait and drain (5s each) - at the 600s default, 650s per
    side, both sides summing to 1300s for a run that scores nothing on
    either side. See the PR body for the exact arithmetic this repo's
    own 533s baseline suite length produces against that number.
    """
    if not config.mutation_testing:
        return [], []
    if read_only:
        return [], [
            NotMeasured(
                MUTATION_TESTING_CHECK,
                NOT_MEASURED_READ_ONLY,
                _MUTMUT_READ_ONLY_DETAIL,
            )
        ]
    if not test_suite_passed:
        return [], [
            NotMeasured(
                MUTATION_TESTING_CHECK,
                NOT_MEASURED_COMMAND_FAILED,
                "[verify] test_suite already failed; mutmut's own baseline run "
                "would only run the suite a third time to abort with 'Tests "
                "don't run cleanly without mutations', so this check refuses "
                "before spending anything (#391 simplify pass on PR #392: the "
                "guard R8.5 Layer 2 already had)",
            )
        ]
    if coverage_duration >= cap:
        return [], [
            NotMeasured(
                MUTATION_TESTING_CHECK,
                NOT_MEASURED_TIMED_OUT,
                f"R8.5 Layer 1's own coverage run already took "
                f"{coverage_duration:.0f}s, at or beyond the {cap:.0f}s this "
                "check has left of the phase's shared [verify] "
                "mutation_timeout budget (#391 simplify pass on PR #392, A2); "
                "mutmut always pays that same suite's baseline in full before "
                "mutating a single line, so it would certainly exhaust what "
                "remains before measuring anything, and this check refuses "
                "before spending it",
            )
        ]
    outcome = check_mutation_score(
        cwd,
        base_branch,
        test_command=config.test_command,
        threshold=config.mutation_threshold,
        timeout=cap,
    )
    if isinstance(outcome, NotMeasured):
        return [], [outcome]
    return [outcome], []


#: Every check :func:`run_mechanical_verification` appends that answers
#: its question by reading ``git diff <base>...HEAD``.
#:
#: Beside the function that appends them, because a caller that has no
#: measurable base has to know which checks that rules out, and deriving
#: the list by reading this module's source is how two callers end up
#: disagreeing about it. ``mutation_testing`` and ``diff_mutation`` (#152)
#: belong here even though ``read_only=True`` already skips both: each
#: mutates the files the diff names, so with no diff there is nothing for
#: either to mutate either.
DIFF_DEPENDENT_CHECKS: tuple[str, ...] = (
    "diff_scope",
    "bad_patterns",
    "policy_envelope",
    "test_adequacy",
    DEAD_CODE_CHECK,
    MUTATION_TESTING_CHECK,
    PATCH_COVERAGE_CHECK,
    DIFF_MUTATION_CHECK,
)


def self_critique_progress_path(
    config: VerifyConfig,
    worktree_path: Path,
    prd_path: Path | None,
) -> Path | None:
    """The log ``check_self_critique`` would read, or None if it will not run.

    Read the log the engineer was actually pointed at: a factory
    component writes NEXT TO its PRD (the only location inside its
    allowedPaths), so resolving a repo-root default here would check a
    file that was never written and fail the component for the harness's
    own path confusion. An explicit config wins. ``prd_path`` is
    worktree-absolute at the factory call site, so the derived sibling is
    too; the join is a no-op for an absolute path and still anchors a
    relative one. With neither a PRD nor an explicit path there is no log
    to read, so the check is skipped rather than run against a path that
    cannot exist.

    Extracted (#288 review) because a caller has to be able to ask
    whether this check will run BEFORE the run, to say so: `ks feature`
    announces its report up front, and the announcement was silently
    wrong for an operator who had set ``require_self_critique``. Two
    copies of the rule is how the announcement and the run disagree, so
    there is one, and :func:`run_mechanical_verification` calls it too.
    """
    if not config.require_self_critique:
        return None
    if config.progress_file_path is not None:
        return worktree_path / Path(config.progress_file_path)
    if prd_path is not None:
        return worktree_path / component_progress_path(prd_path, None)
    return None


def run_undiffed_verification(
    worktree_path: Path,
    config: VerifyConfig,
) -> VerificationResult:
    """Mechanical verification over a tree with no base to diff against.

    The ONLY safe entry point for that case, and it is a function rather
    than a documented convention because :func:`narrow_to_undiffed`
    cannot deliver the guarantee its name promises (#288 review round
    2). Its ``replace`` reaches four of the eight
    :data:`DIFF_DEPENDENT_CHECKS`; the other four - ``policy_envelope``,
    ``test_adequacy``, ``patch_coverage`` and ``diff_mutation`` (#152) -
    are gated by ``policy_config`` and ``adequacy_config``, which are
    separate ARGUMENTS to :func:`run_mechanical_verification`, and
    ``allowed_paths_error``
    outranks the ``check_diff_scope`` toggle entirely because
    :func:`_scope_checks` reads it first and appends the ungated
    ``scope_unreadable`` on any non-None value. So a second caller
    writing ``config=narrow_to_undiffed(cfg), policy_config=pc`` gets
    ``policy_envelope`` reporting a PASS over an empty diff: the exact
    defect the narrowing is named for, reintroduced by an argument the
    narrowing cannot see.

    This owns all of them. There is no parameter here for anything that
    consumes a diff, so the four suppressed by config and the four
    suppressed by argument are suppressed the same way: by not being
    reachable. ``read_only=True`` for the same reason ``ks sense`` uses
    it (R10.1) - the two checks that would rewrite the tree they measure
    are forbidden.

    ``base_branch=""`` is the honest value for "there is no base here"
    and is never read, because nothing left running consumes one.
    ``prd_path=None`` skips the PRD-derived checks: ``prd_stories``
    re-reads a flag the agent itself set, which is a self-report rather
    than an independent measurement.

    The structural version of this - one object owning every argument
    that decides whether a check can honestly run - is tracked on #305.
    """
    return run_mechanical_verification(
        worktree_path=worktree_path,
        prd_path=None,
        base_branch="",
        allowed_paths=None,
        allowed_paths_error=None,
        config=narrow_to_undiffed(config),
        read_only=True,
    )


def narrow_to_undiffed(config: VerifyConfig) -> VerifyConfig:
    """``config`` with every :data:`DIFF_DEPENDENT_CHECKS` toggle off.

    Prefer :func:`run_undiffed_verification`, which owns the arguments
    this cannot reach. Exported on its own only because the announcement
    side of a report needs the narrowed config to say what will run.

    For a caller whose tree has no base it can honestly diff against -
    `ks feature` (#288), where nothing commits for the agent and the
    branch the loop checks out may BE the base branch, so
    ``base...HEAD`` is routinely empty and a diff-based check would
    report ``0 files, all within scope`` over work it never saw.

    An empty diff is indistinguishable from nothing changed: the lenient
    git helpers return an empty file list either way, and even
    ``get_diff_names(..., strict=True)`` returns ``[]`` without raising.
    So the only honest answer is not to run those checks, which is what
    this does.

    Note what it does NOT cover, because the toggles cannot. ``policy``
    and ``adequacy`` are separate config objects and are suppressed by
    not being passed at all. And ``allowed_paths_error`` outranks
    ``check_diff_scope`` entirely: :func:`_scope_checks` reads it first
    and, on ANY non-None value, appends :func:`check_scope_unreadable`
    instead, which is ungated by this config and fails closed by design
    (#294). So a caller relying on this narrowing must still leave that
    argument None, but for the opposite reason to the one that held
    before #294: the risk is no longer a ``diff_scope`` PASS over a diff
    it never saw, it is a hard scope_unreadable FAIL over a scope the
    caller never had.
    """
    return replace(
        config,
        check_diff_scope=False,
        check_bad_patterns=False,
        dead_code_cleanup=False,
        mutation_testing=False,
    )


class MechanicalVerification(Protocol):
    """The call shape of :func:`run_mechanical_verification` (#316).

    ``PipelineHooks.run_mechanical_verification`` was typed
    ``Callable[..., VerificationResult]``, and ``...`` means mypy checks
    NOTHING about the arguments - which matters because that hook is how
    the only call site carrying a real component's scope reaches the
    function. Measured on this branch: with the hook typed ``...``,
    swapping ``harness_paths=scope.harness_paths`` for
    ``harness_paths=scope.error`` - a ``str | None`` into a
    ``list[str] | None`` slot, an authored carve-out replaced by the
    snapshot's failure to read one - left ``mypy --strict`` reporting
    SUCCESS. With this Protocol the same swap is
    ``error: Argument "harness_paths" to "__call__" of
    "MechanicalVerification" has incompatible type "str | None";
    expected "list[str] | None"``.

    Making the arguments keyword-only stops a SLOT from being inherited
    silently; it cannot stop a wrong value being handed to the right
    name. Only a type can, and only if there is one.

    The defaults below are spelled as real values rather than the
    conventional ``= ...`` so that ``inspect.Signature`` equality can
    compare this to the function in one assertion; a Protocol that has
    drifted is worse than none, because it would type-check calls the
    function rejects. See
    ``test_the_protocol_says_exactly_what_the_function_says``.
    """

    def __call__(
        self,
        worktree_path: Path,
        prd_path: Path | None,
        base_branch: str,
        allowed_paths: list[str] | None,
        config: VerifyConfig,
        *,
        allowed_paths_error: str | None = None,
        harness_paths: list[str] | None = None,
        pre_run_prd_path: Path | None = None,
        fixtures_config: FixturesConfig | None = None,
        policy_config: PolicyConfig | None = None,
        adequacy_config: AdequacyConfig | None = None,
        autonomy_level: int = 0,
        component_id: str | None = None,
        read_only: bool = False,
    ) -> VerificationResult: ...


def run_mechanical_verification(
    worktree_path: Path,
    prd_path: Path | None,
    base_branch: str,
    allowed_paths: list[str] | None,
    config: VerifyConfig,
    *,
    allowed_paths_error: str | None = None,
    harness_paths: list[str] | None = None,
    pre_run_prd_path: Path | None = None,
    fixtures_config: FixturesConfig | None = None,
    policy_config: PolicyConfig | None = None,
    adequacy_config: AdequacyConfig | None = None,
    autonomy_level: int = 0,
    component_id: str | None = None,
    read_only: bool = False,
) -> VerificationResult:
    """Run all mechanical checks. All checks run even if earlier ones fail.

    Everything after ``config`` is keyword-only (#316), so an inserted
    parameter cannot shift a later argument into a slot that means
    something else - and three of the arguments here mean opposite
    things in near-identical types (see :class:`MechanicalVerification`,
    which covers the half that keyword-only does not). Cost: none. No
    caller passed any of them positionally.

    ``prd_path=None`` (R10.1, ``ks sense``) skips the PRD-dependent
    checks: ``prd_stories``, the approved-fixtures oracle (fixtures are
    declared in the PRD), and ``self_critique`` unless
    ``config.progress_file_path`` names the log explicitly (with no PRD
    there is no sibling to derive it from). Every other check runs
    exactly as it does with a real path.

    ``harness_paths`` (#264) is the per-component carve-out for kstrl's
    OWN files, forwarded to ``check_diff_scope``. It reaches the factory
    from the run's plan-time scope snapshot (``scope.RunScope``), which
    is also where ``allowed_paths`` comes from; ``ks sense`` leaves both
    None because it judges an operator's diff, not a factory
    component's.

    ``allowed_paths_error`` (#269) is that snapshot reporting that it
    could not read the component's scope at all. It replaces the
    ``diff_scope`` comparison with ``scope_unreadable``, an ungated
    fail-closed refusal named for its own cause (#294) - see
    ``_scope_checks``. Any non-None value refuses, empty included. ``ks sense`` never sets it: it
    has no plan-time snapshot, so its scope is whatever
    ``--allowed-paths`` gave it.

    ``pre_run_prd_path`` (#269) is the copy of ``prd_path`` the run
    started with, forwarded to ``check_prd_stories``, which fails closed
    on a PRD the component rewrote. Also None for ``ks sense``: there is
    no pre-run copy to compare an operator's working tree against.

    ``fixtures_config`` (R7.2): when provided AND ``.enabled`` is true,
    the approved-fixtures oracle runs against the PRD's ``fixtures``
    entries - sandboxed subprocess execution lives in
    ``kstrl.fixtures``. ``component_id`` keys the fixture snapshot
    used for regression detection; None disables snapshotting only.

    ``read_only=True`` (``ks sense``, R10.1) forbids the two checks that
    change the tree they measure: ``dead_code_ruff`` drops its auto-fix
    and the ``git add -A`` / ``git commit`` that followed it, and
    ``mutation_testing`` and ``diff_mutation`` (#152) are not run at all -
    mutmut rewrites the source it mutates either way. What remains still
    shells out to the project's OWN configured test / typecheck / lint
    (and fixture) commands, which are the operator's programs and write
    their own caches; kstrl suppresses only kstrl's writes.

    ``mutation_testing``, ``dead_code_ruff``, ``dead_code``,
    ``patch_coverage`` and ``diff_mutation`` append NO ROW rather than a
    passing one whenever nothing was measured (#306, #335, #152). See
    :func:`_mutation_checks`, :func:`_dead_code_checks`,
    :func:`_patch_coverage_checks` and :func:`_diff_mutation_checks`.
    ``patch_coverage`` also runs the project's own test command a SECOND
    time when ``[adequacy] enabled`` and ``[adequacy] patch_coverage`` are
    both on; it is off by default for that reason. ``diff_mutation``
    additionally needs ``[adequacy] patch_coverage`` on (config-refused
    otherwise) and consumes ITS measurement rather than paying for a
    third. A consumer reading ``checks`` must already tolerate those
    rows' absence, because ``[verify] mutation_testing`` and ``[verify]
    dead_code_cleanup`` both default to false; what changed is that
    absence is now the ONLY thing a non-measurement can look like.

    ``[verify] dead_code_cleanup`` produces TWO rows, not one: the ruff
    F401/F811/F841 phase and the vulture-or-``dead_code_command`` phase
    answer for themselves, because one row for both reported a pass for
    a scan that never ran (#335).

    :attr:`VerificationResult.not_measured` is where the reason goes,
    and it is the half that makes the absence readable rather than
    merely honest. See :class:`NotMeasured`.
    """
    checks: list[CheckResult] = []
    not_measured: list[NotMeasured] = []
    # #399 addendum: the envelope's own secret_patterns, read whether or not
    # [policy] enabled is true (PolicyConfig.load populates the field
    # unconditionally), so check_bad_patterns and check_policy_envelope
    # enforce one rule.
    bad_patterns_secret_patterns = (
        policy_config.secret_patterns if policy_config is not None else DEFAULT_SECRET_PATTERNS
    )

    if prd_path is not None:
        checks.append(check_prd_stories(prd_path, pre_run_prd_path))

    checks.append(
        check_test_suite(
            worktree_path,
            config.test_command,
            config.subprocess_timeout,
            config.test_tool,
        )
    )

    checks.append(
        check_typecheck(
            worktree_path,
            config.typecheck_command,
            config.subprocess_timeout,
            config.typecheck_tool,
        )
    )

    checks.append(
        check_linter(
            worktree_path,
            config.lint_command,
            config.subprocess_timeout,
            config.lint_tool,
        )
    )

    checks.extend(
        _scope_checks(
            worktree_path,
            base_branch,
            allowed_paths=allowed_paths,
            allowed_paths_error=allowed_paths_error,
            harness_paths=harness_paths,
            compare=config.check_diff_scope,
        )
    )

    if config.check_bad_patterns:
        checks.append(check_bad_patterns(worktree_path, base_branch, bad_patterns_secret_patterns))

    # R8.1 policy envelope: opt-in ([policy] enabled). When disabled the
    # check is not appended, so existing runs are unchanged.
    if policy_config is not None and policy_config.enabled:
        checks.append(
            check_policy_envelope(
                worktree_path,
                base_branch,
                policy_config,
            )
        )

    # R8.5 Layer 0: opt-in ([adequacy] enabled), advisory unless the
    # level or config says block. Runs before the expensive layers so a
    # suite-weakening diff is reported even when mutation is off.
    if adequacy_config is not None and adequacy_config.enabled:
        checks.append(
            check_test_adequacy(
                worktree_path,
                base_branch,
                adequacy_config,
                autonomy_level,
            )
        )

    coverage_rows, coverage_gap, coverage = _patch_coverage_checks(
        worktree_path, base_branch, config, adequacy_config
    )
    checks.extend(coverage_rows)
    if coverage_gap is not None:
        not_measured.append(coverage_gap)

    # One [verify] test_suite reading feeds BOTH mutation checks' A2 guard
    # below (#391 simplify pass on PR #392): a single generator read,
    # never re-evaluated between the two calls.
    test_suite_passed = next(c.passed for c in checks if c.name == GATE_TEST)
    coverage_duration = coverage_rows[0].duration_seconds if coverage_rows else 0.0

    # ONE phase-level mutation budget (#391 simplify pass on PR #392,
    # A2), not two independent copies of [verify] mutation_timeout: R8.5
    # Layer 2 runs first and is bounded by the FULL config value; the
    # wall clock its own call actually spent - whether it scored, gapped
    # or refused before spending anything - is subtracted before what
    # remains reaches R8.5 Layer 1 below. `time.monotonic()` around the
    # call, not `mutation_diff_rows[0].duration_seconds`, because a
    # `timed_out` gap still spent the cap's own wall time and produces no
    # row to read a duration from.
    mutation_diff_start = time.monotonic()
    mutation_diff_rows, mutation_diff_gaps = _diff_mutation_checks(
        worktree_path,
        config,
        adequacy_config,
        coverage,
        coverage_gap,
        coverage_duration,
        test_suite_passed=test_suite_passed,
        read_only=read_only,
    )
    mutation_diff_elapsed = time.monotonic() - mutation_diff_start
    checks.extend(mutation_diff_rows)
    not_measured.extend(mutation_diff_gaps)

    dead_code_rows, dead_code_gaps = _dead_code_checks(
        worktree_path,
        base_branch,
        config,
        read_only=read_only,
    )
    checks.extend(dead_code_rows)
    not_measured.extend(dead_code_gaps)

    mutation_rows, mutation_gaps = _mutation_checks(
        worktree_path,
        base_branch,
        config,
        coverage_duration,
        test_suite_passed=test_suite_passed,
        cap=max(0.0, config.mutation_timeout - mutation_diff_elapsed),
        read_only=read_only,
    )
    checks.extend(mutation_rows)
    not_measured.extend(mutation_gaps)

    progress_path = self_critique_progress_path(config, worktree_path, prd_path)
    if progress_path is not None:
        checks.append(
            check_self_critique(
                progress_path,
                config.self_critique_min_bullets,
            )
        )

    if prd_path is not None and fixtures_config is not None and fixtures_config.enabled:
        # Imported lazily: fixtures.py imports CheckResult/run_scrubbed
        # from this module, so a module-level import would be a cycle.
        from kstrl.fixtures import check_fixtures_from_prd

        checks.append(
            check_fixtures_from_prd(
                prd_path,
                worktree_path,
                fixtures_config,
                component_id=component_id,
            )
        )

    # ``checks`` only: a check that measured nothing neither passes nor
    # fails the run (#306).
    passed = all(c.passed for c in checks)
    return VerificationResult(passed=passed, checks=checks, not_measured=not_measured)
