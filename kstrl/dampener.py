"""The dampener: a sense measurement in version control, and what a branch added to it.

R10.6 (#227). The mechanism is three parts and no LLM: record the current
structured failure signatures of a tree in a file the repository tracks, run the
same sensors on a branch, and report what the branch ADDED. It is advisory by
default - it prints the report and exits 0 whether or not it found a regression -
because a dampener that fails a teammate's pull request on its first day is a
dampener somebody turns off.

The vocabulary is deliberately the evolution journal's. A signature here is
``"<check>:<code>"`` produced by
:func:`kstrl.evolution.signature_counts_from_verification`, so the dampener and
the journal cannot disagree about what a failure is called, and the spelling
lives in exactly one module.

FIVE BUCKETS, AND WHY ``fixed`` IS THE NARROW ONE
-------------------------------------------------
``new``, ``increased`` and ``stopped_measuring`` FLAG: they may over-match, and
the cost of a false one is a comment somebody reads. ``fixed`` CLEARS: it says a
failure went away, and an over-matching clear deletes the mechanism silently. So
``fixed`` has to be PROVED, not inferred from absence. A baseline signature that
is absent now lands in ``fixed`` only when the check that produced it MEASURED
SOMETHING in the current run; when it did not, the signature lands in
``unmeasured`` and the report says so.

``stopped_measuring`` is the flagging half of the same fact, keyed on the CHECK
rather than on a signature, and it is not redundant with ``unmeasured``: that
bucket holds baseline signatures, and a baseline can be green. This repository's
own committed baseline records ``"signatures": {}``, so on a branch whose test
suite stops finishing there is no signature anywhere to bucket, and without this
the report read ``no regression`` and exited 0 under ``--fail-on-regression``.

That is why :attr:`Baseline.unmeasured_checks` exists on both sides. A sensor
that timed out, whose tool is missing, or that recorded a
:class:`kstrl.verify.NotMeasured` gap contributes NO signatures to a baseline and
is named in ``unmeasured_checks`` instead. Without that rule this repository's
own baseline was measurably wrong: at the default 300s verify timeout the test
suite times out, which is a FAILING row carrying the signature
``test_suite:test-suite-timed-out-after-s`` (the digits are stripped by
``signature_slug``, so 300s and 1800s produce the same string), and the same tree
on a faster machine reports that signature as fixed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from kstrl.atomicio import atomic_write_json
from kstrl.evolution import signature_counts_from_verification, split_signature
from kstrl.verify import CheckResult, ResolvedVerifyCommands, VerificationResult

#: Version of the BASELINE document, which is not the version of the
#: ``ks sense --json`` document. They move independently: the baseline records
#: the sensor's schema version in ``sense_schema_version`` so a reader can tell
#: that the sensor changed under a baseline nobody refreshed.
BASELINE_SCHEMA_VERSION = 1

#: Relative to ``--root``. ``scripts/kstrl/`` is the versioned per-project kstrl
#: config home, beside ``prompt.md`` and ``prd.json``.
DEFAULT_BASELINE_PATH = Path("scripts/kstrl/sense-baseline.json")

FORMAT_HUMAN = "human"
FORMAT_MARKDOWN = "markdown"

#: What :attr:`Comparison.stopped_measuring` records when the current run has
#: no reason for a check at all: the check produced neither a row nor a gap,
#: so it was not asked for. That is still a sensor that stopped.
NO_REASON_RECORDED = "the check produced no row at all in this run"

#: ``--write-baseline`` and ``--compare-baseline`` take an OPTIONAL path. Click
#: spells that with ``is_flag=False, flag_value=<sentinel>``, so the bare flag
#: yields this string. A NUL byte cannot appear in an argv element, so no
#: operator can type it: the sentinel is closed by construction rather than by a
#: reserved word somebody might pass. Measured: it does not appear in ``--help``.
OPTIONAL_VALUE_SENTINEL = "\x00default"


class BaselineError(ValueError):
    """The baseline could not be read as a baseline.

    A ``ValueError`` because that is what the CLI's fail-closed handlers already
    catch, and because ``UnicodeDecodeError`` - a real way this fails - is one.
    Never raised for a baseline that is merely RED: a brownfield repository's
    baseline is expected to record failures, and that is the case this exists
    for.
    """


class DampenerUsage(ValueError):
    """A flag combination in which one of the flags would silently do nothing."""


def _fail(message: str) -> NoReturn:
    """One raise site, so `-> NoReturn` tells mypy the checks below are
    exhaustive without an `assert isinstance` after every one of them."""
    raise BaselineError(message)


def _object_field(document: Mapping[str, Any], key: str) -> Any:
    if key not in document:
        _fail(f"baseline is missing {key!r}")
    return document[key]


def _bool_field(document: Mapping[str, Any], key: str) -> bool:
    value = _object_field(document, key)
    if not isinstance(value, bool):
        _fail(f"baseline {key!r} must be a boolean, got {type(value).__name__}")
    return value


def _int_field(document: Mapping[str, Any], key: str) -> int:
    value = _object_field(document, key)
    # bool is an int in Python; a `true` here is a malformed document, not a 1.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"baseline {key!r} must be an integer, got {type(value).__name__}")
    return value


def _optional_str_field(document: Mapping[str, Any], key: str) -> str | None:
    value = _object_field(document, key)
    if value is not None and not isinstance(value, str):
        _fail(f"baseline {key!r} must be a string or null, got {type(value).__name__}")
    return value if isinstance(value, str) else None


def _str_tuple_field(document: Mapping[str, Any], key: str) -> tuple[str, ...]:
    # Required, not defaulted: a missing `measured_checks` read as empty is
    # a lenient read of a key the comparison depends on.
    value = _object_field(document, key)
    if not isinstance(value, list):
        _fail(f"baseline {key!r} must be an array, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            _fail(f"baseline {key!r}[{index}] must be a non-empty string, got {item!r}")
    return tuple(value)


def _str_field(document: Mapping[str, Any], key: str) -> str:
    value = _object_field(document, key)
    if not isinstance(value, str):
        _fail(f"baseline {key!r} must be a string, got {type(value).__name__}")
    return value


def _str_map_field(document: Mapping[str, Any], key: str) -> dict[str, str]:
    value = _object_field(document, key)
    if not isinstance(value, dict):
        _fail(f"baseline {key!r} must be an object, got {type(value).__name__}")
    reasons: dict[str, str] = {}
    for name, reason in value.items():
        if not isinstance(name, str) or not name:
            _fail(f"baseline {key!r} has a key that is not a non-empty string: {name!r}")
        if not isinstance(reason, str) or not reason:
            _fail(f"baseline {key!r}[{name!r}] must be a non-empty string, got {reason!r}")
        reasons[name] = reason
    return reasons


def _signatures_field(document: Mapping[str, Any], key: str) -> dict[str, int]:
    value = _object_field(document, key)
    if not isinstance(value, dict):
        _fail(f"baseline {key!r} must be an object, got {type(value).__name__}")
    counts: dict[str, int] = {}
    for name, count in value.items():
        if not isinstance(name, str) or not name:
            _fail(f"baseline {key!r} has a key that is not a non-empty string: {name!r}")
        # `< 1`, not `< 0`: `to_document` writes a Counter of occurrences, so a
        # count of zero is a shape it cannot produce. Accepting one put "was 0"
        # in the `fixed` table of a report - a number that means nothing
        # happened, presented as something that did.
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            _fail(f"baseline {key!r}[{name!r}] must be a positive integer, got {count!r}")
        counts[name] = count
    return counts


def verify_digest(commands: ResolvedVerifyCommands, timeout: float) -> str:
    """A digest of HOW a tree was measured: the three gate commands and the timeout.

    ``docs/dampener.md`` states that a baseline and a comparison measured at
    different timeouts are not a comparison, and before this the only mechanism
    behind that sentence was a literal ``1800`` typed into the workflow YAML.
    An operator who ran the comparison at the default 300s got a report in
    which this repository's own test suite had "stopped" failing.

    Recorded in the baseline and checked by :func:`refuse_foreign_baseline`,
    which is the same rule ``decisions.bind_register`` applies to the decision
    register: an artifact one phase writes and another READS carries the
    identity of the thing it belongs to, and the reader checks it.

    Sixteen hex characters of SHA-256. The digest is an equality check on a
    short JSON payload, not a security boundary; the full 64 would make the
    baseline diff noisier for nothing.
    """
    payload = json.dumps(
        {
            "test": commands.test,
            "typecheck": commands.typecheck,
            "lint": commands.lint,
            "timeout": timeout,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Baseline:
    """One sense measurement, reduced to what a later run can be compared to.

    Both sides of a comparison are one of these: the committed document and the
    run that just happened. Same type on purpose, so nothing can compare a
    baseline against a shape that carries less information than it does.
    """

    generated_at: str
    base_ref: str | None
    #: Which project this measurement is OF: ``owner/repo`` from ``origin``,
    #: falling back to the measured directory's name when there is no remote.
    #: Provenance the report NOTES rather than refuses: a baseline copied
    #: between two checkouts of the same project is legitimate, a baseline
    #: copied between two different projects is the ``bind_register`` mistake,
    #: and only a person can tell those apart.
    project: str
    passed: bool
    sense_schema_version: int
    #: :func:`verify_digest` of the commands and timeout this was measured
    #: with. A mismatch IS a refusal: see :func:`refuse_foreign_baseline`.
    verify_digest: str
    #: Checks that produced a row AND measured something. The only checks whose
    #: absent signature may be reported as fixed.
    measured_checks: tuple[str, ...]
    #: Checks that were asked for and measured nothing: a gap, a missing tool, a
    #: timeout. They contribute no signatures at all.
    unmeasured_checks: tuple[str, ...]
    #: Why each name in ``unmeasured_checks`` measured nothing, as the check
    #: itself said it. Same key set as ``unmeasured_checks``, checked on read:
    #: a hole in a baseline is only useful if the reader can see what made it.
    unmeasured_reasons: Mapping[str, str]
    signatures: Mapping[str, int]

    @property
    def total_findings(self) -> int:
        """Occurrences, not distinct signatures: 12 E501s are 12, not 1."""
        return sum(self.signatures.values())

    def to_document(self) -> dict[str, Any]:
        """The on-disk JSON, with every collection sorted.

        ``sorted`` on ``str`` compares code points and never consults the
        locale, so the file is byte-stable across machines and a git diff shows
        only what actually moved. Top-level key order is this literal order and
        is pinned by a test, because reordering it would produce a whole-file
        diff on a run that changed nothing.
        """
        return {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "base_ref": self.base_ref,
            "project": self.project,
            "passed": self.passed,
            "sense_schema_version": self.sense_schema_version,
            "verify_digest": self.verify_digest,
            "measured_checks": sorted(self.measured_checks),
            "unmeasured_checks": sorted(self.unmeasured_checks),
            "unmeasured_reasons": dict(sorted(self.unmeasured_reasons.items())),
            "signatures": dict(sorted(self.signatures.items())),
        }

    @classmethod
    def from_document(cls, raw: object) -> Baseline:
        """Validate the RAW payload entry by entry, then build.

        Nothing here is read leniently. A document this cannot understand is a
        refusal, never an empty baseline: read as ``{}`` every current signature
        would be "new" (or every baseline one would vanish), and either way the
        mechanism is gone with nothing failing.
        """
        if not isinstance(raw, dict):
            raise BaselineError(f"baseline must be a JSON object, got {type(raw).__name__}")
        version = _int_field(raw, "schema_version")
        if version != BASELINE_SCHEMA_VERSION:
            raise BaselineError(
                f"baseline schema_version is {version}, expected {BASELINE_SCHEMA_VERSION}; "
                "run ks sense --write-baseline --force to regenerate it"
            )
        unmeasured = _str_tuple_field(raw, "unmeasured_checks")
        reasons = _str_map_field(raw, "unmeasured_reasons")
        if set(reasons) != set(unmeasured):
            raise BaselineError(
                "baseline 'unmeasured_reasons' names "
                f"{sorted(reasons)} but 'unmeasured_checks' names {sorted(unmeasured)}; "
                "every hole in a baseline carries the reason it is there. "
                "Regenerate it with ks sense --write-baseline --force"
            )
        return cls(
            # Provenance, like ``base_ref``: nothing gates on it, so null is
            # accepted, but a wrong TYPE is still a refusal. Read through the
            # same helper as every other field rather than a lenient
            # ``raw.get``, so the paragraph above stays true of all of them.
            generated_at=_optional_str_field(raw, "generated_at") or "",
            base_ref=_optional_str_field(raw, "base_ref"),
            project=_str_field(raw, "project"),
            passed=_bool_field(raw, "passed"),
            sense_schema_version=_int_field(raw, "sense_schema_version"),
            verify_digest=_str_field(raw, "verify_digest"),
            measured_checks=_str_tuple_field(raw, "measured_checks"),
            unmeasured_checks=unmeasured,
            unmeasured_reasons=reasons,
            signatures=_signatures_field(raw, "signatures"),
        )


def _measured_and_unmeasured(
    result: VerificationResult,
) -> tuple[list[CheckResult], dict[str, str]]:
    """Split a verification into the rows that measured and the names that did not.

    A check named in ``not_measured`` produced no row at all; a check whose row
    carries ``measured=False`` produced one and measured nothing anyway (a
    timeout, a missing detector). Both are equally unable to prove that a
    signature was fixed, so both land on the same side. A name in both is
    unmeasured: the clearing side has to be the narrow one.

    The unmeasured half comes back as name -> REASON rather than as bare names,
    because a hole in a baseline that does not say what made it sends the
    operator back to the run to find out. A row's message wins over a gap's
    reason for the same name: the row is the more specific statement, and the
    name is unmeasured either way.
    """
    reasons = {gap.check: f"{gap.reason}: {gap.detail}" for gap in result.not_measured}
    reasons.update(
        {
            check.name: check.message or NO_REASON_RECORDED
            for check in result.checks
            if not check.measured
        }
    )
    measured = [check for check in result.checks if check.measured and check.name not in reasons]
    return measured, reasons


def baseline_from_result(
    result: VerificationResult,
    *,
    base_ref: str | None,
    project: str,
    generated_at: str,
    sense_schema_version: int,
    digest: str,
) -> Baseline:
    """Reduce a sense run to a :class:`Baseline`.

    ``generated_at``, ``base_ref``, ``project`` and ``digest`` are injected
    rather than read here so the document is a pure function of the run for
    tests. ``sense_schema_version`` is passed in from
    :data:`kstrl.cli.SENSE_SCHEMA_VERSION` rather than imported, because the
    CLI imports this module.

    ``limit=None``: the journal caps a check at five distinct signatures so one
    catastrophic run cannot flood a journal entry, but a baseline that dropped
    the sixth would report it as new on the very next run.
    """
    measured, reasons = _measured_and_unmeasured(result)
    counts = signature_counts_from_verification(measured, limit=None)
    return Baseline(
        generated_at=generated_at,
        base_ref=base_ref,
        project=project,
        passed=result.passed,
        sense_schema_version=sense_schema_version,
        verify_digest=digest,
        measured_checks=tuple(sorted({check.name for check in measured})),
        unmeasured_checks=tuple(sorted(reasons)),
        unmeasured_reasons=dict(sorted(reasons.items())),
        signatures=dict(sorted(counts.items())),
    )


def read_baseline(path: Path) -> Baseline:
    """The committed baseline, or :class:`BaselineError` saying why not.

    THE PARSER'S ERROR TAXONOMY BELONGS TO THE PARSER, which is the #318 rule
    stated for ``tomllib`` and true for the same reason here: the reasoning is
    about the DOCUMENT, not about which module reads it. Round 1 of review on
    #357 measured this function catching ``ValueError`` around ``json.loads``
    and a baseline of 200000 nested arrays escaping as a bare
    ``RecursionError`` - a ``RuntimeError``, not a ``ValueError`` - so a
    document this promises to refuse with exit 2 killed the command with a
    traceback and exit 1 instead. Four rules, all checkable:

    1. ``Exception`` exactly. Narrower is the defect itself: everything about
       the document derives from ``Exception``, while ``KeyboardInterrupt``
       and ``SystemExit`` are about the process.
    2. The broad clause LAST, or the specific message above it is unreachable.
    3. ALL the I/O outside the guarded block. ``read_bytes`` here, ``decode``
       and ``loads`` there, so no widening of the parse guard can reach an
       ``OSError`` and report a disk failure as malformed JSON.
    4. Report individually only the causes that can be named. Two are:
       a file that is not there, and bytes that are not UTF-8.

    ``Baseline.from_document`` is called OUTSIDE the guard too. It raises
    ``BaselineError``, which is a ``ValueError``, and a broad clause around it
    would rewrite its own precise message into "is not JSON".
    """
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        raise BaselineError(f"no baseline at {path}; run ks sense --write-baseline first") from None
    except OSError as exc:
        raise BaselineError(f"cannot read the baseline at {path}: {exc}") from exc
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BaselineError(f"cannot read the baseline at {path}: {exc}") from exc
    except Exception as exc:
        raise BaselineError(f"{path} is not JSON: {type(exc).__name__}: {exc}") from exc
    return Baseline.from_document(document)


def refuse_foreign_baseline(baseline: Baseline, digest: str) -> None:
    """Raise unless this baseline was measured the way this run will be.

    ``decisions.bind_register`` refuses a register whose project and spec do
    not match the manifest about to be scheduled, and the reason is the same
    one: an artifact one phase writes and another READS has to carry the
    identity of the thing it belongs to, and the reader has to check it.
    ``docs/dampener.md`` already said a baseline and a comparison measured at
    different timeouts are not a comparison; this is the mechanism behind that
    sentence, in place of a literal timeout typed into a workflow file.

    Both digests are named because neither one alone tells the operator
    anything they can act on.
    """
    if baseline.verify_digest != digest:
        raise BaselineError(
            f"this baseline was measured with a different verify configuration: "
            f"baseline digest {baseline.verify_digest}, this run {digest}. "
            "The digest covers the test, typecheck and lint commands and the "
            "subprocess timeout; a comparison across two of those is not a "
            "comparison. Restore the configuration it was written under, or "
            "regenerate it with ks sense --write-baseline --force"
        )


def refuse_existing_baseline(path: Path, *, force: bool) -> None:
    """Raise unless ``path`` may be written.

    Called twice: once before the sensors run, so an operator who forgot
    ``--force`` is told in a tenth of a second rather than after a full test
    suite, and once immediately before the write, which is the authoritative
    refusal. The window between them is microseconds and there is no security
    boundary here; ``atomic_write_json`` cannot do an exclusive create because
    ``os.replace`` overwrites.
    """
    if not force and path.exists():
        raise BaselineError(f"{path} exists; pass --force to replace it")


def write_baseline(path: Path, baseline: Baseline, *, force: bool) -> None:
    """Write the baseline atomically, creating ``scripts/kstrl/`` if needed."""
    refuse_existing_baseline(path, force=force)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, baseline.to_document())


def write_summary_line(path: Path, baseline: Baseline) -> str:
    """The one line ``--write-baseline`` prints.

    The unmeasured sensors are named on it, always, ``none`` included: a
    baseline written while the test suite timed out is a baseline with a hole in
    it, and the operator has to be able to see that at the moment they commit
    the file rather than infer it from the JSON later.
    """
    unmeasured = ", ".join(baseline.unmeasured_checks) or "none"
    return (
        f"baseline written: {path} "
        f"({len(baseline.signatures)} signatures, {baseline.total_findings} total findings); "
        f"unmeasured: {unmeasured}"
    )


@dataclass(frozen=True)
class Comparison:
    """What a branch added to, and removed from, a baseline."""

    #: Signature absent from the baseline. Flags: may over-match.
    new: dict[str, int]
    #: ``(baseline count, current count)`` where the count rose. Flags.
    increased: dict[str, tuple[int, int]]
    #: In the baseline, absent now, and its check measured something now.
    #: Clears: proved, never inferred.
    fixed: dict[str, int]
    #: In the baseline, absent now, and its check measured nothing now.
    unmeasured: dict[str, int]
    #: Check name -> why it measured nothing now, for every check the BASELINE
    #: measured and this run did not. Flags: a sensor going dark is the thing
    #: this whole mechanism exists to notice, and the ``unmeasured`` bucket
    #: cannot cover it, because that bucket holds baseline SIGNATURES and a
    #: green baseline has none.
    stopped_measuring: dict[str, str]
    #: ``(baseline, current)`` when the sensor's own schema version moved under
    #: the baseline, else None. A note, not a refusal: see :func:`compare`.
    sense_schema_changed: tuple[int, int] | None
    #: ``(baseline, current)`` when the project is not the one the baseline
    #: records, else None. A note, for the reason on :attr:`Baseline.project`.
    project_changed: tuple[str, str] | None

    @property
    def regressed(self) -> bool:
        """The single verdict. ``fixed`` and ``unmeasured`` never affect it.

        ``stopped_measuring`` DOES, and that is the one asymmetry worth
        stating. A branch on which the test suite stops finishing produces no
        new signature and no increased one - it produces no signatures at all -
        so without this the single most important thing a pull-request check
        could catch was reported as "no regression" and exit 0. Measured on the
        head of #357 against this repository's own committed baseline.
        """
        return bool(self.new) or bool(self.increased) or bool(self.stopped_measuring)


def compare(baseline: Baseline, current: Baseline) -> Comparison:
    """Bucket every signature on either side.

    A signature whose check the BASELINE never measured still lands in ``new``
    when it appears now. That over-flags when a toolchain gains a binary rather
    than the tree getting worse, which is the safe direction for a flagging
    guard and costs an advisory comment.

    The reverse - a check the baseline measured and this run did not - is
    ``stopped_measuring``, and it is a REGRESSION rather than a note. A sensor
    that went dark produces no signature to put in any of the other four
    buckets, so before it existed the report for a branch whose test suite
    stopped finishing was "no regression".

    A differing ``sense_schema_version`` is a NOTE rather than exit 2, and this
    is the one place the house fail-closed rule is deliberately not applied. The
    document parses, the baseline schema is v1 either way, and the dangerous
    half of the ambiguity - a renamed check reading as fixed - is already closed
    by the ``unmeasured`` bucket. Failing closed instead would break every
    consumer's pull-request check the moment the sensor version moved.
    """
    new: dict[str, int] = {}
    increased: dict[str, tuple[int, int]] = {}
    for signature, count in sorted(current.signatures.items()):
        before = baseline.signatures.get(signature)
        if before is None:
            new[signature] = count
        elif count > before:
            increased[signature] = (before, count)

    fixed: dict[str, int] = {}
    unmeasured: dict[str, int] = {}
    measured_now = set(current.measured_checks)
    for signature, count in sorted(baseline.signatures.items()):
        if signature in current.signatures:
            continue
        check, _code = split_signature(signature)
        if check in measured_now:
            fixed[signature] = count
        else:
            unmeasured[signature] = count

    # The FIFTH bucket, and the only one keyed on a check rather than on a
    # signature. Set difference over the two `measured_checks` lists, so it
    # covers both ways a sensor goes dark: a row that measured nothing now, and
    # a check that produced no row at all because somebody turned it off.
    stopped: dict[str, str] = {
        check: current.unmeasured_reasons.get(check, NO_REASON_RECORDED)
        for check in sorted(set(baseline.measured_checks) - measured_now)
    }

    changed: tuple[int, int] | None = None
    if baseline.sense_schema_version != current.sense_schema_version:
        changed = (baseline.sense_schema_version, current.sense_schema_version)
    renamed: tuple[str, str] | None = None
    if baseline.project != current.project:
        renamed = (baseline.project, current.project)
    return Comparison(
        new=new,
        increased=increased,
        fixed=fixed,
        unmeasured=unmeasured,
        stopped_measuring=stopped,
        sense_schema_changed=changed,
        project_changed=renamed,
    )


def exit_code_for(comparison: Comparison, *, fail_on_regression: bool) -> int:
    """Advisory by default: 0 whether or not the branch regressed.

    One function so the graduation switch has one place to be wrong and one test
    to catch it.
    """
    if fail_on_regression and comparison.regressed:
        return 1
    return 0


@dataclass(frozen=True)
class WriteMode:
    """``--write-baseline``: where to write, and whether it may replace."""

    path: Path
    force: bool


@dataclass(frozen=True)
class CompareMode:
    """``--compare-baseline``: the baseline already read, and how to report it.

    The baseline is a FIELD rather than something a later phase fetches,
    because it is read before the sensors run (see :func:`resolve_mode`) and
    carrying it here is what makes "resolved" mean the command has everything
    it needs.
    """

    path: Path
    baseline: Baseline
    fail_on_regression: bool
    output_format: str


#: Two shapes, not one shape with a discriminant string. Roadmap doctrine 6
#: forbids a new status vocabulary, and the earlier ``action="write"`` /
#: ``action="compare"`` pair was one: it also forced three fields to be
#: hard-coded dead on whichever side did not use them, and an
#: ``assert baseline is not None`` in the CLI to say what the type could not.
Mode = WriteMode | CompareMode


def _baseline_path(value: str, root_dir: Path) -> Path:
    """Where the baseline lives, bare flag or explicit value: BOTH under ``--root``.

    ``Path.__truediv__`` returns the right-hand side unchanged when it is
    absolute, so one expression covers the three cases. The relative one is the
    fix: before it, passing the exact path ``--help`` advertises as the default
    together with ``--root`` read a different file and reported "no baseline
    at ..." for a file that exists. Two rules for one flag is the surprise; one
    rule, stated in ``--help``, is not.
    """
    if value == OPTIONAL_VALUE_SENTINEL:
        return root_dir / DEFAULT_BASELINE_PATH
    return root_dir / Path(value).expanduser()


def _refuse_dead_flags(
    *,
    write_baseline: str | None,
    compare_baseline: str | None,
    force: bool,
    fail_on_regression: bool,
    output_format: str | None,
    as_json: bool,
) -> None:
    """Refuse every combination in which a flag would silently do nothing.

    Naming both flags in the message, rather than only the ignored one, is what
    lets an operator see which of the two they meant.
    """
    if write_baseline is not None and compare_baseline is not None:
        raise DampenerUsage("--write-baseline and --compare-baseline cannot be used together")
    if force and write_baseline is None:
        raise DampenerUsage("--force does nothing without --write-baseline")
    if fail_on_regression and compare_baseline is None:
        raise DampenerUsage("--fail-on-regression does nothing without --compare-baseline")
    if output_format is not None and compare_baseline is None:
        raise DampenerUsage("--format does nothing without --compare-baseline")
    if as_json and output_format is not None:
        raise DampenerUsage(f"--json and --format {output_format} cannot be used together")
    if as_json and write_baseline is not None:
        # --write-baseline prints one line and writes a file. There is no JSON
        # document for it to produce, so --json would silently do nothing,
        # which is the same defect as the four above rather than a lesser one.
        raise DampenerUsage("--json does nothing with --write-baseline")


def resolve_mode(
    *,
    write_baseline: str | None,
    compare_baseline: str | None,
    force: bool,
    fail_on_regression: bool,
    output_format: str | None,
    as_json: bool,
    root_dir: Path,
) -> Mode | None:
    """The dampener mode, or None when no dampener flag was given.

    ``None`` is the whole of the "plain ``ks sense`` is unchanged" promise: the
    command takes exactly the same path it took before this feature existed.

    THE BASELINE IS SETTLED HERE, BEFORE THE SENSORS RUN, and that is a product
    attribute rather than tidiness: a full sense run on this repository costs
    327 measured seconds, so telling an operator who forgot ``--force`` after
    five minutes instead of a tenth of a second is latency they feel. Both ways
    this can refuse - :class:`DampenerUsage` for a flag that would do nothing,
    :class:`BaselineError` for a baseline that cannot be read - reach the CLI's
    one fail-closed handler and exit 2. The ``--force`` refusal is made again
    inside :func:`write_baseline`, and that second one is authoritative.
    """
    _refuse_dead_flags(
        write_baseline=write_baseline,
        compare_baseline=compare_baseline,
        force=force,
        fail_on_regression=fail_on_regression,
        output_format=output_format,
        as_json=as_json,
    )
    if write_baseline is not None:
        path = _baseline_path(write_baseline, root_dir)
        refuse_existing_baseline(path, force=force)
        return WriteMode(path=path, force=force)
    if compare_baseline is not None:
        path = _baseline_path(compare_baseline, root_dir)
        return CompareMode(
            path=path,
            baseline=read_baseline(path),
            fail_on_regression=fail_on_regression,
            output_format=output_format or FORMAT_HUMAN,
        )
    return None
