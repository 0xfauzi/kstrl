"""The blind-site ledger of ``tests/test_check_name_enrolment.py``.

Moved here whole by #696 so the guard file stays under the 800-line
ratchet; the guard imports it and asserts it exactly as before.
"""

from __future__ import annotations

#: Producer sites the walk DETECTS but whose check name it cannot read,
#: as ``(module, enclosing qualname, expression, why)``. Pinned by
#: ``TestTheBlindSitesAreEnumerated.test_the_ledger_is_exactly_these_sites``,
#: so a new one is a red test and a resolved one is a red test too.
#:
#: The standard is ``tests/test_journal_one_writer.py``: "The test below
#: asserts that miss, so this disclosure fails if it stops being true."
#: A disclosure with no test behind it is how #339 review found this
#: file claiming "a new gate cannot reach the journal uncategorised"
#: while a whole producer was invisible to it.
#:
#: WHAT THIS LEDGER DOES NOT CLOSE, said plainly because the resemblance
#: to its precedent is misleading. ``EXPECTED_JOURNAL_PATH_SITES`` in
#: ``tests/test_journal_one_writer.py`` inventories every place the
#: resource is OBTAINED, so a new way of obtaining it lands in the list
#: whatever it looks like: closed by construction. This inventories the
#: places the walk GAVE UP, which is a different quantity. It is closed
#: only over the producer shapes ``_name_sites`` and ``_signature_sites``
#: already enumerate. A producer written in a shape neither of them
#: matches is not resolved AND not recorded here: it is silence, and the
#: ledger staying the same length is not evidence of anything.
#:
#: That is not hypothetical - it is how ``pipeline._fail_pr_flow``
#: survived two review rounds. #324 is the tracking item that closes the
#: class properly, by giving every guard in this repo one shared AST
#: walker instead of a hand-rolled matcher each; this file is the tenth
#: instance of that defect and should be retired onto it rather than
#: widened again.
#:
#: ``tests/test_signature_chokepoint.py`` is the one pin in this story
#: that DOES have the precedent's guarantee, and it is the answer to the
#: paragraph above until #324 lands: a check name cannot reach
#: ``category_for_check`` without touching a ``*failure_signatures``
#: name, and it pins all five such sites. Measured: a new writer
#: reaching the mapping through a local alias, with a signature the fold
#: cannot read, is invisible to the census, to this ledger, to
#: ``tests/test_check_name_shapes.py`` and to the spellings net, and
#: fails that pin alone.
#:
#: No line numbers: they rot on every edit above them and would make
#: this list a thing to be regenerated rather than read. The expression
#: text is what pins the site, so changing what a blind site computes is
#: a red test.
#: Every row today is a PASS-THROUGH or a RUNTIME COMPOSER, which is
#: the shape of an acceptable blind site: the string is decided
#: somewhere the walk does read, and the row says where. A site whose
#: name is decided HERE and cannot be read is not acceptable, and there
#: are none left - the last one was ``_coverage_failure``, resolved by
#: :func:`_from_the_callers`.
BLIND_SITES: tuple[tuple[str, str, str, str], ...] = (
    (
        "kstrl/baseline.py",
        "Baseline.from_document",
        "_signatures_field(raw, 'signatures')",
        "pass-through of a validated baseline FILE (#227): the names in it "
        "were written by signature_counts_from_verification on some earlier "
        "run and are censused at that call site. This site decides no name; "
        "it refuses a document whose keys are not non-empty strings",
    ),
    (
        "kstrl/baseline.py",
        "baseline_from_result",
        "dict(sorted(counts.items()))",
        "runtime composer over signature_counts_from_verification's output "
        "(#227), from the CheckResult names the gates built; those names are "
        "censused at their CheckResult call sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._phase_verify",
        "signatures_from_verification(verification.checks)",
        "composed at run time from the CheckResult names the gates built; "
        "those names are censused at their CheckResult call sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._record_failure_signatures",
        "list(signatures)",
        "pass-through: re-emits what fail/retry_or_fail was handed, censused at those call sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._record_failure_signatures",
        "signature_for_error(phase or 'unknown', error)",
        "the phase= fallback itself; the phase is censused at the "
        "fail/retry_or_fail call sites and 'unknown' is enrolled outright",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._review_failure",
        "signatures_from_findings('review', review_result.as_findings())",
        "composed at run time as '<phase>:<Finding.category>'; the phase is "
        "the literal in this call and the category comes from findings.py",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.signatures",
        "pass-through: a PhaseFailure built at one of the sites above. "
        "This row appears TWICE on purpose - _route_failure has two "
        "branches spelling it, the comparison is a sorted list rather "
        "than a set, and collapsing them would hide one of the two",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.signatures",
        "pass-through, the second of the two branches named above",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.phase",
        "pass-through: the phase of a PhaseFailure built elsewhere in this "
        "module. PhaseFailure is a PHASE_FALLBACK_CALL in its own right, so "
        "each of those phases is censused where it is written",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._route_failure",
        "failure.phase",
        "pass-through, the second of the same two branches",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline._security_failure",
        "signatures_from_findings('security', sec_result.as_findings(), "
        "sec_result.failing_severities)",
        "composed at run time, as _review_failure above",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.fail",
        "phase",
        "pass-through of its own parameter into _record_failure_signatures; "
        "censused at the fail() call sites, which is why fail is itself a "
        "PHASE_FALLBACK_CALL",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.retry_or_fail",
        "phase",
        "pass-through of its own parameter into _record_failure_signatures; "
        "censused at the retry_or_fail() call sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.retry_or_fail",
        "phase",
        "the second of two, and a different call: the retries-exhausted route "
        "hands the same parameter on to fail(), censused at the same sites",
    ),
    (
        "kstrl/pipeline.py",
        "ComponentPipeline.retry_or_fail",
        "signatures",
        "pass-through of its own parameter, censused at its call sites",
    ),
    (
        "kstrl/verify.py",
        "check_stack_command",
        "row",
        "#696: row = f'stack:{name}', one of the function's four CheckResult "
        "calls (timeout, undecodable, pass, fail); the name after 'stack:' is "
        "the operator's [stack] check name, decided in kstrl.toml. The "
        "signature head is the literal 'stack', enrolled in "
        "_CATEGORY_BY_CHECK and pinned in test_signature_spellings.py",
    ),
    (
        "kstrl/verify.py",
        "check_stack_command",
        "row",
        "#696: row = f'stack:{name}', one of the function's four CheckResult "
        "calls (timeout, undecodable, pass, fail); the name after 'stack:' is "
        "the operator's [stack] check name, decided in kstrl.toml. The "
        "signature head is the literal 'stack', enrolled in "
        "_CATEGORY_BY_CHECK and pinned in test_signature_spellings.py",
    ),
    (
        "kstrl/verify.py",
        "check_stack_command",
        "row",
        "#696: row = f'stack:{name}', one of the function's four CheckResult "
        "calls (timeout, undecodable, pass, fail); the name after 'stack:' is "
        "the operator's [stack] check name, decided in kstrl.toml. The "
        "signature head is the literal 'stack', enrolled in "
        "_CATEGORY_BY_CHECK and pinned in test_signature_spellings.py",
    ),
    (
        "kstrl/verify.py",
        "check_stack_command",
        "row",
        "#696: row = f'stack:{name}', one of the function's four CheckResult "
        "calls (timeout, undecodable, pass, fail); the name after 'stack:' is "
        "the operator's [stack] check name, decided in kstrl.toml. The "
        "signature head is the literal 'stack', enrolled in "
        "_CATEGORY_BY_CHECK and pinned in test_signature_spellings.py",
    ),
    ("kstrl/verify.py", "_command_not_run", "gate", "pass-through; censused at its call sites"),
    (
        "kstrl/verify.py",
        "_failed_gate_result",
        "name",
        "pass-through of its own parameter; _failed_gate_result is itself a "
        "CHECK_NAME_CALL, so every caller is censused",
    ),
)
