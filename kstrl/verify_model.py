"""What mechanical verification is configured with and what it returns."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.config_numbers import check_numbers
from kstrl.findings import Finding
from kstrl.rung import Rung
from kstrl.stack import Stack, stack_in_force


@dataclass
class CheckResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
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
    # Read by :mod:`kstrl.baseline` and nothing else today. It changes no
    # existing behaviour and no published surface: `passed` still decides the
    # verdict, the report table and the `ks check --json` check objects are
    # untouched. What it buys is that a signature's disappearance can be told
    # apart from the check's, which a fallback signature cannot say for
    # itself: `signature_slug` strips digits, so "timed out after 300.0s" and
    # "timed out after 1800.0s" are the same string.
    measured: bool = True
    # #462: the gate's own output, stdout then stderr, when a test,
    # typecheck or lint gate ran and FAILED; None for every other row.
    # #527: a gate that timed out, or printed bytes that are not utf-8,
    # FAILED too, and this holds what it printed before it was stopped.
    # Bounded by :func:`bounded_gate_output`. The pipeline writes it to
    # disk as the operator's evidence for the failure. It is never put in
    # the retry prompt, the report table or ``ks check --json``: those
    # read ``details``, which is the excerpt of this text.
    output: str | None = None


#: Why a check that was ASKED FOR produced no measurement. A stable
#: token: it reaches `ks check --json` and `events.jsonl`, so a reader
#: keys on it and not on the prose beside it.
#:
#: ``retired``: kstrl no longer has a mechanism for the check, and the
#: thing that asked for it (the autonomy ladder, for Layer 0) still does
#: (#696 slice 8).
NOT_MEASURED_RETIRED = "retired"


@dataclass(frozen=True)
class NotMeasured:
    """A check that was asked for and produced no measurement (#306).

    The SIDECAR. Deliberately not a :class:`CheckResult`: it never
    reaches ``checks``, so ``all(c.passed ...)``, ``report_lines``'
    verdict column, ``ks check --json``'s ``checks`` array and
    :func:`kstrl.review.build_review_prompt` cannot read it as a pass -
    which is the whole of #306. Equally it never reaches
    :meth:`VerificationResult.as_context`, so it is not retry context:
    no engineer iteration is spent on a gap it cannot close.

    A check nobody asked for records nothing at all, on purpose: a
    question nobody asked needs no answer. ``reason`` is one of the
    ``NOT_MEASURED_*`` constants above. ``detail`` is prose for a human
    and is never parsed.
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
        """The ``ks check --json`` rendering."""
        return {"check": self.check, "reason": self.reason, "detail": self.detail}


#: The gap Phase 1 records where Layer 0 ran (#696 decision 7): from
#: autonomy level 1, where the R8.5 roadmap made Layer 0 blocking. The
#: mechanical layer read one language's test files and is gone; the code
#: reviewer's test-weakening criterion (``review.REVIEWER_PROMPT``) is the
#: only check left, and it has no measured detection rate yet. The
#: pipeline puts this gap on the pull request.
LAYER0_NOT_MEASURED = NotMeasured(
    "test_adequacy",
    NOT_MEASURED_RETIRED,
    "Layer 0 not measured: kstrl reads no test file mechanically (#696 decision 7). "
    "The code reviewer's test-weakening criterion is the only check that this change "
    "did not weaken the tests, and its detection rate is not yet measured.",
)


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

    @property
    def failure_count(self) -> int:
        """How many checks failed (#233).

        One per failing check: kstrl parses no check's output, so a check
        that reported forty failures counts the same as one that reported
        one (#696 decision 5). This is the number ``[factory]
        convergence_attempts`` watches across attempts; the retry context
        cannot supply it, because Phase 1 files one entry per attempt
        however many checks failed.
        """
        return sum(1 for check in self.checks if not check.passed)

    def report_lines(
        self,
        *,
        durations: bool = True,
        max_detail_lines: int | None = None,
    ) -> list[str]:
        """One line per check, then the indented details of each failure.

        The TERMINAL rendering of this object, in one place: ``ks check``
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
        ``Log`` event on the run bus, and a failing check's details are
        the excerpt :func:`kstrl.failure_excerpt.failure_excerpt` keeps,
        up to 80 lines. A run of failing checks is hundreds of events
        per report, up to ``2 + repair_max_runs`` times a run, which is
        the event-stream flood ``commandrun._StreamFilterSink`` exists to
        prevent. ``as_context`` already truncates at 10 for the same
        reason. None (the default, and ``ks check``) prints everything:
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
        # `ks check` and `ks feature` must not be able to disagree about
        # whether they mention what was not measured. No verdict column
        # and no duration - there is no verdict, and nothing was timed.
        lines.extend(gap.as_line() for gap in self.not_measured)
        return lines


def _optional_str(value: object) -> str | None:
    """A toml scalar as a string, with the empty string meaning unset."""
    return str(value) or None


def gate_names(config: VerifyConfig) -> tuple[str, ...]:
    """The names Phase 1's command gates go by: the ``[stack]``'s check
    names (#696), or none when there is no stack."""
    return config.project_stack.check_names if config.project_stack is not None else ()


def _gate_name_list(value: object, source: str) -> list[str]:
    """``value`` as a list of strings, or ValueError naming ``source``."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{source} must be a list of gate names, got {value!r}")
    return list(value)


def validate_fast_iteration_checks(value: object, source: str, names: Sequence[str]) -> list[str]:
    """``value`` as a list of gate names, or ValueError naming ``source``.

    A list of strings, each one of ``names``: :func:`gate_names`, the
    ``[stack]``'s check names. Empty is valid and turns the
    between-iteration checks off.
    """
    gates = _gate_name_list(value, source)
    unknown = [item for item in gates if item not in names]
    if unknown:
        raise ValueError(f"{source} names unknown gate(s) {unknown}; expected any of {list(names)}")
    return gates


def _fast_iteration_checks_from_toml(value: object) -> list[str]:
    """The shape only: the names are checked at the end of
    :meth:`VerifyConfig.load`, once it knows whether a ``[stack]`` names them."""
    return _gate_name_list(value, "[verify] fast_iteration_checks")


def _fast_iteration_checks_from_env(raw: str) -> list[str]:
    """Comma-separated. Blank parts are dropped, so "" is the empty list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


#: Every live ``[verify]`` toml key and how its value is coerced onto the
#: dataclass. A table rather than a per-key ``if``: the chain it replaced
#: was fourteen near-identical branches, and its cyclomatic complexity
#: was already twice the repo's ratchet limit before #258 added three
#: more keys to it. Order is the dataclass's, so a reader can diff the
#: two lists by eye. Anything absent from this table is not a toml key.
_VERIFY_TOML_FIELDS: tuple[tuple[str, Callable[[Any], object]], ...] = (
    ("check_diff_scope", bool),
    ("check_bad_patterns", bool),
    ("subprocess_timeout", float),
    ("require_self_critique", bool),
    ("self_critique_min_bullets", int),
    ("progress_file_path", _optional_str),
    ("fast_iteration_checks", _fast_iteration_checks_from_toml),
)


@dataclass
class VerifyConfig:
    """Configuration for mechanical verification."""

    check_diff_scope: bool = True
    check_bad_patterns: bool = True
    subprocess_timeout: float = 0.0
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
    # #233: the gates run between engineer iterations, whose failures are
    # handed to the next iteration's prompt. Empty (the default) is off.
    # A list, never a tuple: scripts/gen_docs.py probes list defaults.
    fast_iteration_checks: list[str] = field(default_factory=list)
    # #696: the project's [stack], read by ``load`` from its own table. Its
    # checks are Phase 1's command gates; None is no stack, and Phase 1 then
    # fails closed (:data:`NO_STACK_CHECK`). Provenance: no [verify] key.
    project_stack: Stack | None = field(default=None, metadata={"provenance": True})
    # #700 slice 2: the TEST-zone rung a ``ks factory`` run under a [stack]
    # proved before its base gates; every [stack] check runs inside it.
    # None runs on the host. Set only through ``FactoryConfig``, never from
    # kstrl.toml. Provenance: no [verify] key.
    rung: Rung | None = field(default=None, metadata={"provenance": True})

    @classmethod
    def from_env(cls) -> VerifyConfig:
        """Load verify config from environment variables."""
        return cls(
            subprocess_timeout=float(os.environ.get("KSTRL_TIMEOUT_VERIFY", "0")),
            require_self_critique=os.environ.get("KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE", "") == "1",
            self_critique_min_bullets=int(
                os.environ.get("KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS", "3"),
            ),
            progress_file_path=os.environ.get("KSTRL_VERIFY_PROGRESS_FILE"),
            fast_iteration_checks=_fast_iteration_checks_from_env(
                os.environ.get("KSTRL_VERIFY_FAST_ITERATION_CHECKS", ""),
            ),
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
        # the dataclass default (e.g. KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS=3
        # could not override a toml self_critique_min_bullets), breaking the
        # env-beats-toml precedence contract (R2.1).
        env = cls.from_env()
        env_var_to_field = {
            "KSTRL_TIMEOUT_VERIFY": "subprocess_timeout",
            "KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE": "require_self_critique",
            "KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS": "self_critique_min_bullets",
            "KSTRL_VERIFY_PROGRESS_FILE": "progress_file_path",
            "KSTRL_VERIFY_FAST_ITERATION_CHECKS": "fast_iteration_checks",
        }
        for env_var, field_name in env_var_to_field.items():
            if env_var in os.environ:
                setattr(config, field_name, getattr(env, field_name))
        # Last, so every [verify] key above has been read when a bad
        # [stack] raises (the entry check's unread-name report).
        config.project_stack = stack_in_force(root_dir)
        if "fast_iteration_checks" in section:
            # The toml value is refused on its own names even when the
            # environment overrides it, as before #696.
            validate_fast_iteration_checks(
                section["fast_iteration_checks"],
                "[verify] fast_iteration_checks",
                gate_names(config),
            )
        source = (
            "KSTRL_VERIFY_FAST_ITERATION_CHECKS"
            if "KSTRL_VERIFY_FAST_ITERATION_CHECKS" in os.environ
            else "[verify] fast_iteration_checks"
        )
        config.fast_iteration_checks = validate_fast_iteration_checks(
            config.fast_iteration_checks, source, gate_names(config)
        )
        return check_numbers(config)
