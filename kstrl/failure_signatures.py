"""The failure signature of a check error, its category, and the routing of recurring patterns."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kstrl.plan import ADVERSARIAL_BUDGET_CHECK
from kstrl.verify_diff import SCOPE_UNREADABLE_CHECK, SCOPE_UNREADABLE_ERROR_PREFIX

if TYPE_CHECKING:
    from kstrl.plan import Finding
    from kstrl.verify_model import CheckResult


@dataclass
class FailurePattern:
    description: str
    frequency: int
    total_components: int
    affected_components: list[str]
    check_name: str  # e.g. "test_suite", "typecheck", "linter", "review"
    # structured failure code (e.g. "S608" for ruff, "arg-type" for mypy,
    # "scope_creep" for a review concern) - the part after the colon in
    # the full "<check>:<code>" signature
    error_signature: str
    # A _CATEGORY_BY_CHECK value, or UNENROLLED_CATEGORY; see category_for_check.
    category: str
    # #496: True when every row the signature was counted from is a
    # findings_superseded row, so it was never on a component's final attempt.
    superseded_only: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Regex for linter rule codes like S608, E501, W291, etc.
_LINTER_CODE_RE = re.compile(r"\b([A-Z]\d{3,4})\b")

# Regex to strip file paths (unix and windows style)
_PATH_RE = re.compile(r"(?:/[\w./-]+|[A-Z]:\\[\w.\\-]+)")

# Regex to strip line/column numbers like ":42:" or "line 42"
_LINENO_RE = re.compile(r"(?::\d+:?\d*|line \d+|col(?:umn)? \d+)", re.IGNORECASE)

# Regex to strip quoted variable/argument names
_QUOTED_NAME_RE = re.compile(r"['\"][\w.]+['\"]")


def normalize_error(error: str) -> str:
    """Normalize an error string into a stable signature.

    - Keeps linter rule codes as-is (e.g. S608, E501).
    - Strips file paths, line numbers, and variable names.
    - Converts the remaining message to a slug.
    """
    if not error:
        return ""

    # Check for a linter rule code first - it is the most stable identifier.
    code_match = _LINTER_CODE_RE.search(error)
    if code_match:
        return code_match.group(1)

    normalized = error
    normalized = _PATH_RE.sub("", normalized)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)

    # Take the first meaningful line only.
    first_line = normalized.strip().split("\n")[0].strip()

    # Extract "ErrorType: message" pattern if present.
    colon_idx = first_line.find(":")
    if colon_idx > 0:
        error_type = first_line[:colon_idx].strip()
        message = first_line[colon_idx + 1 :].strip()
        # Slugify message portion.
        slug = re.sub(r"[^a-z0-9]+", "-", message.lower()).strip("-")
        if slug:
            return slug[:80]
        return re.sub(r"[^a-z0-9]+", "-", error_type.lower()).strip("-")[:80]

    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:80] if slug else "unknown"


def classify_check(error: str) -> str:
    """Return the check name inferred from the error text.

    The legacy fallback, used wherever a component has no in-memory
    ``failure_signatures`` entry - which is EVERY ``ks evolve`` or
    metrics read over a manifest written by an earlier process. So a
    failure whose text lands here unrecognised is filed as
    unknown/iteration no matter how carefully it was categorised at the
    time it happened.

    #294 round 2: that is what happened to the scope refusal. Its own
    ``_CATEGORY_BY_CHECK`` row was unreachable on this path, so ``ks
    evolve`` filed it under a check name that was not its own. Matched
    FIRST because the text also contains words the later rules claim.

    #315: this returned ``(check_name, category)`` until every caller
    was measured to discard the category, making it a SECOND place a
    category was decided that no consumer could reach. A dead answer
    that can silently disagree with the live one is how the live one
    later gets "fixed" in the wrong file, so the category is gone from
    here and ``category_for_check`` is the only decision.
    """
    lower = error.lower()

    if SCOPE_UNREADABLE_ERROR_PREFIX.lower() in lower:
        return SCOPE_UNREADABLE_CHECK
    if any(kw in lower for kw in ("ruff", "flake8", "pylint", "lint")):
        return "linter"
    if any(kw in lower for kw in ("mypy", "pyright", "typecheck", "type error")):
        return "typecheck"
    if any(kw in lower for kw in ("pytest", "test", "assert", "unittest")):
        return "test_suite"
    if any(kw in lower for kw in ("review", "finding", "reviewer")):
        return "review"
    if any(kw in lower for kw in ("contract", "integration")):
        return "contract"
    if any(kw in lower for kw in ("mechanical verification failed",)):
        return "verification"

    return "unknown"


# ---------------------------------------------------------------------------
# Structured failure signatures (R6.1)
#
# A failure signature is "<check_name>:<code>", e.g. "stack:tests:<slug>",
# "review:scope_creep", "diff_scope:files-outside-allowed-scope". The check
# prefix comes from the gate that fired. A failed mechanical check has ONE
# signature, a slug of kstrl's own message for it (#696 decision 5): kstrl
# parses no check's output, so the code never comes from what the tool
# printed. A reviewer's code is its finding category.
# ---------------------------------------------------------------------------

# Digit runs are counts/limits ("3 files outside scope", "600s wall
# clock") - stripping them keeps slugs stable across runs whose only
# difference is the number.
_DIGIT_RUN_RE = re.compile(r"\d+")

# The categories are verification, review, security, contract,
# iteration and, since #315, infrastructure. That last one is for a
# failure that is neither a gate's verdict on the change nor the
# engineer's loop: the run was shut down, hit the token ceiling, could
# not merge, could not fetch its own diff, could not provision a
# worktree. Before it existed those fell through to "iteration", so the
# journal filed them as engineer-loop problems and the evolve screen's
# category column said so: a verdict about an agent that could not have
# prevented any of them.
#
# NOT Finding.category's "infrastructure_error" (kstrl/plan/findings.py),
# which marks one ROLE RUN that failed to execute inside an otherwise
# healthy component. Different taxonomy, different consumer,
# deliberately different spelling so a grep for either does not silently
# return the other.
#
# They also disagree, on purpose and measurably. factory's live autonomy
# accounting asks the FINDING question (`_infra_casualty`) while the
# replay asks the SIGNATURE question, and #339 review counted the
# divergence rather than leaving it at the one example this comment used
# to give. Seven signatures, in both directions:
#
#   - the whole `pr:` family, `provisioning:` and `aborted:shutdown` are
#     infrastructure to the replay and attach no finding at all, so the
#     live side calls them judgement;
#   - `review:infrastructure` and `security:infrastructure` are the
#     reverse: a finding is attached, but `review`/`security` are not
#     infrastructure prefixes, so the replay counts them as evidence;
#   - `scope_unreadable:` depends on which producer fired - the Phase 1
#     gate attaches a finding, the pre-launch refusal in factory does
#     not;
#   - `adversarial_budget:claim` is the seventh, added by #226 round
#     2 and of the first kind: the R10.3 claim gate refuses a
#     component whose reviewer never ran, the only finding on it is the
#     phase_skipped trace, so the replay calls it plumbing and the live
#     side calls it judgement. Its two siblings do NOT diverge -
#     `adversarial_budget:review` and `adversarial_budget:security` come
#     from `pipeline._budget_refusal`, which attaches the
#     infrastructure_error finding, so both consumers read the same
#     answer. Enrolling the check is what fixed those two and what
#     exposed this one: before the sweep it was spelled
#     `review:claim-budget-exhausted` and both consumers agreed by
#     both being wrong. tests/test_claim_agreement.py::
#     test_the_claim_refusal_is_a_disclosed_divergence asserts both
#     halves, so this row fails if it stops being true.
#
# Reconciling those is not this table's job (#332 holds factory.py), but
# an undercount was, because it read as a single known exception.
#
# tests/test_check_name_enrolment.py pins the whole table row by row, so
# a new row, a dropped row or a typo in a category is a red test with
# the diff as its audit trail.
_CATEGORY_BY_CHECK = {
    "linter": "verification",
    "typecheck": "verification",
    "test_suite": "verification",
    "diff_scope": "verification",
    # #294 split this out of diff_scope; diff_scope stays because
    # journal entries written before the split carry its signatures.
    SCOPE_UNREADABLE_CHECK: "verification",
    "bad_patterns": "verification",
    "self_critique": "verification",
    "dead_code": "verification",
    # #335 split the fused dead-code row in two: `dead_code` stayed on
    # the vulture-or-dead_code_command phase and this is the ruff
    # F401/F811/F841 phase beside it.
    "dead_code_ruff": "verification",
    "mutation_testing": "verification",
    # R8.5 Layer 1 (#152). Advisory and it never fails, so it files no
    # signature today; enrolled anyway because the table is the list of
    # check names, not the list of names that have failed so far.
    "patch_coverage": "verification",
    # R8.5 Layer 2 (#152). Same reasoning as patch_coverage: advisory,
    # never fails, no signature today, enrolled because the table is
    # every check name.
    "diff_mutation": "verification",
    "prd_stories": "verification",
    "verification": "verification",
    # #315: mechanical gates that the table did not carry, so every
    # approved-fixtures failure, policy-envelope breach and
    # test-adequacy block was filed under the engineer loop.
    "fixtures": "verification",
    "policy_envelope": "verification",
    "test_adequacy": "verification",
    # #696: a [stack] check's row is "stack:<name>", so its signature's
    # check part is "stack". A Phase 1 gate like the three above.
    "stack": "verification",
    # #700 slice 6: a failure of the operator's acceptance checks is filed
    # under its phase, a verdict on the change as Phase 1's are.
    "acceptance": "verification",
    # #315 round 2: a failure recorded with no signatures= is filed
    # under its PHASE (pipeline._record_failure_signatures), so these
    # two are check names as much as any gate is. "verify" is the phase
    # spelling of the mechanical gates above; "provisioning" is a
    # worktree that would not build, which no agent can write code
    # against.
    "verify": "verification",
    "provisioning": "infrastructure",
    # #315: recorded outside a CheckResult, by pipeline.fail(signatures=
    # ["<prefix>:<code>"]). None of these is a verdict on the change.
    "aborted": "infrastructure",
    "token_budget": "infrastructure",
    "pr": "infrastructure",
    "diff": "infrastructure",
    # R10.5 (#226): a hard-mode reviewer that never ran because
    # max_adversarial_calls was already spent. Infrastructure for the
    # same reason token_budget is: it is a ceiling the operator set,
    # not a verdict on the change, and no reviewer looked at the
    # component. Enrolling it here is what makes the replay agree with
    # factory's live accounting, which reads the infrastructure_error
    # finding the same refusal attaches.
    ADVERSARIAL_BUDGET_CHECK: "infrastructure",
    # #315: these two rows state "iteration" explicitly, and since #496
    # they are the only way a name reaches it: the fallback for a name
    # the table does not carry is UNENROLLED_CATEGORY. "unknown" is what
    # classify_check returns when it cannot recognise a legacy error
    # string: not the engineer's fault so much as nobody's, and inventing
    # a category for "we could not tell" would be a worse answer than
    # the one this table has always given.
    "engineer": "iteration",
    "unknown": "iteration",
    "review": "review",
    "security": "security",
    "contract": "contract",
}

#: Checks whose failures are infrastructure, derived from the table so
#: there is ONE answer rather than two lists to keep in step:
#: :data:`kstrl.autonomy_replay.INFRA_FAILURE_PREFIXES` builds its
#: prefixes from this, so enrolling a check as infrastructure reaches
#: both consumers in one edit (#315).
INFRASTRUCTURE_CHECKS: frozenset[str] = frozenset(
    name for name, category in _CATEGORY_BY_CHECK.items() if category == "infrastructure"
)

#: The categories a recurring failure can teach a lesson in (#217 Slice 1).
#: ``route_patterns`` puts a pattern in ``lessons`` when its check's
#: category is one of these. "infrastructure" is not here because no agent
#: can act on it, "iteration" because it names the engineer loop rather
#: than a check, and :data:`UNENROLLED_CATEGORY` because a name the table
#: does not carry must never become a lesson.
LESSON_CATEGORIES: frozenset[str] = frozenset({"verification", "review", "security", "contract"})

# Cap on distinct signatures one reviewer phase contributes, so one
# catastrophic review cannot flood the journal entry.
_MAX_SIGNATURES_PER_CHECK = 5


def signature_slug(text: str) -> str:
    """Stable low-cardinality slug for a failure message.

    Strips file paths, line/column numbers, quoted names, and standalone
    counts, then slugifies the first line. Unlike ``normalize_error``
    this never extracts linter codes and never keeps varying counts."""
    if not text:
        return ""
    normalized = _PATH_RE.sub("", text)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)
    normalized = _DIGIT_RUN_RE.sub("", normalized)
    first_line = normalized.strip().split("\n")[0].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:60]


def signature_for_error(check_name: str, error: str) -> str:
    """The signature of a failure: its check name and a slug of its message."""
    slug = signature_slug(error) or "failed"
    return f"{check_name or 'unknown'}:{slug}"


def split_signature(signature: str) -> tuple[str, str]:
    """Split "check:code" into (check_name, code).

    Splits on the LAST colon, not the first (#696 flag day): a check name
    is now itself ``stack:<name>`` and carries a colon of its own, while a
    parsed linter code and :func:`signature_slug`'s output never do (the
    slug regex keeps only ``[a-z0-9-]``). The first colon would cut
    ``stack:lint:some-slug`` into ``("stack", "lint:some-slug")``, a check
    name ``_CATEGORY_BY_CHECK`` and the baseline's measured-check match
    cannot recognise, silently moving every stack-check signature from
    ``fixed`` to ``unmeasured``. Measured: ``tests/test_check_baseline_cli.
    py::test_compare_reports_a_fixed_signature_and_exits_0`` red on
    ``partition``, green on ``rpartition``. :func:`category_for_check`
    files such a check name under the part before its FIRST colon.
    """
    check, sep, code = signature.rpartition(":")
    if not sep:
        return "unknown", signature
    return check or "unknown", code or "failed"


#: The category of a check name ``_CATEGORY_BY_CHECK`` does not carry
#: (#496). Not a learnable category: ``route_patterns`` sends it to
#: ``unrouted``. Before #496 an unlisted name fell through to
#: ``"iteration"``, a learnable category, which
#: docs/continuous-learning-design.md section 9 requires inverted before
#: anything writes to a store shared across repositories.
UNENROLLED_CATEGORY = "unenrolled"


def category_for_check(check_name: str) -> str:
    """Map a check/gate name to a FailurePattern category.

    An unlisted name returns :data:`UNENROLLED_CATEGORY`. Until #496 it
    fell through to "iteration", which filed a gate under the engineer
    loop. Enrolling a new check in
    ``_CATEGORY_BY_CHECK`` was a convention with no mechanism, and
    measured during #294 the convention did not hold for eight of the
    nineteen names ``kstrl/`` emits. All eight are enrolled as of #315,
    four of them into the ``"infrastructure"`` category that had to be
    invented to hold them, and nothing is grandfathered any more.

    ``tests/test_check_name_enrolment.py`` is the mechanism, and #339
    review corrected what it establishes. It AST-walks ``kstrl/`` for
    the four places a component's failure signatures are written - a
    ``CheckResult`` name, a ``signatures=`` argument, the ``phase=`` of
    a failure recorded with neither, and a direct
    ``component_failure_signatures[...] = [...]`` assignment - and fails
    on a name this table does not carry. What it does NOT establish is
    that no gate can reach the journal uncategorised, which is what this
    docstring claimed while the fourth producer was invisible to it and
    eight of nine shape mutations survived.

    What it establishes now, in three files and stated as three separate
    claims because they hold three separate things:

    - a producer site the walk RECOGNISES and cannot read is enumerated
      in its ``BLIND_SITES`` ledger rather than dropped, which is what
      the old walk did;
    - ``tests/test_signature_spellings.py`` pins every signature-shaped
      string in the package whatever container it sits in, so a producer
      in a shape nobody has thought of still has to appear somewhere;
    - ``tests/test_check_name_shapes.py`` pins what the walk SAYS about
      each shape, including the shapes it says nothing about, because a
      shape the walk does not recognise leaves no trace in either of the
      two above. That is a limit, not a proof, and it is the limit
      ``pipeline._fail_pr_flow`` lived in until #339 review.

    The answer is computed here, at read time, and never stored in the
    journal - measured: a ``record_run`` entry has no ``category`` key
    and experiments.tsv records the signature only. So a correction to
    the table also corrects what is reported about runs that already
    happened. The one surface that displays it is the evolve screen's
    patterns table; the ``ks evolve`` CLI prints the check name.

    The table is keyed on the check's FAMILY, the part of the name before
    its first colon. :func:`split_signature` splits on the last colon, so
    a ``[stack]`` check comes back as ``stack:<name>`` and a coverage
    failure as ``<phase>:coverage-unverified``; looked up whole, both
    missed their row and were filed ``unenrolled`` (#696 slice 5). A name
    with no colon is its own family.
    """
    family = check_name.partition(":")[0]
    return _CATEGORY_BY_CHECK.get(family, UNENROLLED_CATEGORY)


@dataclass(frozen=True)
class PatternRouting:
    """Where each cross-run pattern goes. Three buckets, one census.

    The three tuples partition the input: their lengths sum to the
    number of patterns routed, and ``tests/test_evolve_routing.py``
    asserts that over every name in ``_CATEGORY_BY_CHECK``. A census
    rather than a ledger of exceptions, because a ledger is closed only
    over the shapes someone already enumerated (CLAUDE.md).

    - ``mechanical``: something is broken, not a lesson. The pipeline
      already opens a deduped ``ItemKind.HALTED_RUN`` inbox item for
      this traffic, so it has a destination; what it must not have is a
      lesson telling an agent to take extra care about a failed git push.
    - ``lessons``: a check whose category is in
      :data:`LESSON_CATEGORIES`. Nothing writes them anywhere yet (#217
      Slice 1 deleted the proposal generator); ``ks evolve`` prints them
      as candidate lessons.
    - ``unrouted``: everything else: the ``iteration`` rows, and every
      check name ``_CATEGORY_BY_CHECK`` does not carry, whose category
      is :data:`UNENROLLED_CATEGORY` since #496.
    """

    mechanical: tuple[FailurePattern, ...]
    lessons: tuple[FailurePattern, ...]
    unrouted: tuple[FailurePattern, ...]


def route_patterns(patterns: list[FailurePattern]) -> PatternRouting:
    """Split patterns into mechanical, lessons and unrouted.

    Keyed on ``check_name`` through ``category_for_check``, never on the
    stamped ``FailurePattern.category``: the table is the one answer, and
    a record whose stamped field disagrees with it routes by the table.
    The stamped field stays what it has always been: what the evolve
    screen displays.
    """
    mechanical: list[FailurePattern] = []
    lessons: list[FailurePattern] = []
    unrouted: list[FailurePattern] = []
    for pattern in patterns:
        category = category_for_check(pattern.check_name)
        if category == "infrastructure":
            mechanical.append(pattern)
        elif category in LESSON_CATEGORIES:
            lessons.append(pattern)
        else:
            unrouted.append(pattern)
    return PatternRouting(
        mechanical=tuple(mechanical),
        lessons=tuple(lessons),
        unrouted=tuple(unrouted),
    )


def signature_counts_from_verification(checks: Iterable[CheckResult]) -> dict[str, int]:
    """Each failed check's one signature, with its count.

    ONE signature per failed check, ``"<check>:<slug of its message>"`` from
    :func:`signature_for_error` (#696 decision 5). kstrl parses no check's
    output, so the signature names the check and never what the tool
    printed: a ``[stack]`` check's message is "`<command>` exited <n>" with
    the digits stripped, so an exit 1 and a launcher's exit 2 are the same
    signature and the second can never read as the first one fixed. The
    accepted loss: a new failure inside an already-failing check changes
    no signature.

    Counted rather than listed because :mod:`kstrl.baseline` records a
    count per signature.

    The ``"<check>:<code>"`` spelling lives in this ONE MODULE, in the
    functions that have to agree: this one composes it through
    :func:`signature_for_error`, and :func:`split_signature` takes it apart
    again, which is how :func:`kstrl.baseline.compare` recovers a check
    name before deciding ``fixed``.
    """
    failed = (check for check in checks if not check.passed)
    return dict(Counter(signature_for_error(check.name, check.message) for check in failed))


def signatures_from_verification(checks: Iterable[CheckResult]) -> list[str]:
    """The signatures of the failed checks, one each, in check order.

    The KEYS of :func:`signature_counts_from_verification`, so the journal
    and the baseline cannot disagree about what a failure is called.
    """
    return list(signature_counts_from_verification(checks))


def signatures_from_findings(
    phase: str,
    findings: Iterable[Finding],
    gating: frozenset[str] = frozenset({"fail", "critical", "high"}),
) -> list[str]:
    """Derive signatures from the typed findings that failed a review or
    security gate: "<phase>:<category>" for every finding whose severity
    is in ``gating`` and "<phase>:infrastructure" when the role itself
    failed to run. Phase 2.5 passes its result's own failing severities,
    because its ``fail_threshold`` decides which findings failed (#524)."""
    signatures: list[str] = []
    for finding in findings:
        if finding.is_infrastructure_error:
            signatures.append(f"{phase}:infrastructure")
        elif finding.severity in gating:
            signatures.append(f"{phase}:{finding.category}")
    return list(dict.fromkeys(signatures))[:_MAX_SIGNATURES_PER_CHECK]
