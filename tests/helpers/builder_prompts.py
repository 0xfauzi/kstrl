"""Tables for the 30 instruction-text fragments enrolled under H3: 24 from
#303, one from #233, four from #633 and one from #700.

These builders assemble optional, branching text (a fragment may or may
not appear in a given render), so a single per-module `*_PROMPT` body
cannot reproduce their output byte for byte the way one enrolled prompt
does elsewhere. Each fragment is its own constant instead, and the four
tables below are what `tests/test_prompt_versions.py` merges in.

They live here rather than in `tests/test_prompt_versions.py` because
that file is close to the repo's 800-line file-length ratchet and these
30 snapshot rows do not fit inside the remaining headroom.

`_BUILDERS` is the single source: eleven rows of (module, the name of that
module's shared `*_PROMPT_VERSION` constant, the fragment names that
module's builder assembles). `BUILDER_PROMPTS`, `BUILDER_VERSIONS` and
`BUILDER_RENDER_EXEMPT` are all derived from it, so a fragment enters the
census by appearing in exactly one row here.

BUILDER_PROMPTS: name -> the enrolled body.
BUILDER_VERSIONS: name -> the *_PROMPT_VERSION of the BUILDER that
    delivers it (11 distinct values for 30 names). The version's unit is
    the text one builder delivers to a role, which is what a role
    receives; `test_prompt_versions._drift_message` names
    `<NAME>_VERSION` in its instructions, and for these fragments the
    constant to bump is the builder's, declared directly above its
    bodies in the `kstrl/` module.
BUILDER_SNAPSHOTS: name -> (sha256, version) exactly like
    `_EXPECTED_SNAPSHOTS`, four lines per row because E501 is enforced
    here and a name plus a 64-character hash plus a version does not
    fit in 100 columns on one line. This table is the pin: it is not
    derived from `_BUILDERS`, and nothing in this file computes a hash.
BUILDER_RENDER_EXEMPT: every name, because each fragment is one branch
    of a multi-branch builder: no single production function returns
    any one of them verbatim, so the exact-equality orphan guard in
    `test_prompt_versions.py::test_renderer_renders_the_enrolled_body`
    cannot hold it. Their orphan guards are
    `tests/test_builder_prompts.py::test_enrolled_fragment_reaches_its_builder`
    (the 30 call-time constants) and the delivered-output digests in the
    same file.
"""

from __future__ import annotations

from types import ModuleType

from kstrl import context, factory, init_cmd, knowledge, loop, review_claims, verify

#: Eleven rows: (module, the name of that module's shared version constant,
#: the fragment names it assembles). This is the one place a fragment is
#: declared enrolled; BUILDER_PROMPTS, BUILDER_VERSIONS and
#: BUILDER_RENDER_EXEMPT are all read off it below.
_BUILDERS: tuple[tuple[ModuleType, str, tuple[str, ...]], ...] = (
    (
        context,
        "ITERATION_CONTEXT_PROMPT_VERSION",
        (
            "ITERATION_CONTEXT_HEADER_PROMPT",
            "ITERATION_CONTEXT_CURRENT_PROMPT",
            "ITERATION_CONTEXT_NOT_REMEASURED_PROMPT",
            "ITERATION_CONTEXT_NOT_REMEASURED_SINCE_PROMPT",
            "ITERATION_CONTEXT_RESOLVED_PROMPT",
            "ITERATION_CONTEXT_HISTORY_PROMPT",
            "ITERATION_CONTEXT_CLOSING_PROMPT",
        ),
    ),
    # #700 slice 6: the head of an engineer's retry after the operator's
    # acceptance checks did not pass (context.add_acceptance_failure).
    (
        context,
        "ACCEPTANCE_RETRY_PROMPT_VERSION",
        ("ACCEPTANCE_RETRY_PROMPT",),
    ),
    (
        factory,
        "IN_LOOP_SCOPE_VIOLATION_PROMPT_VERSION",
        ("IN_LOOP_SCOPE_VIOLATION_PROMPT",),
    ),
    (
        init_cmd,
        "CLAUDE_MD_PROMPT_VERSION",
        (
            "CLAUDE_MD_OVERVIEW_PROMPT",
            "CLAUDE_MD_VERIFICATION_PROMPT",
            "CLAUDE_MD_LEARNINGS_PROMPT",
        ),
    ),
    (
        knowledge,
        "KNOWLEDGE_CONTEXT_PROMPT_VERSION",
        ("KNOWLEDGE_CONTEXT_PROMPT", "KNOWLEDGE_OVERFLOW_PROMPT"),
    ),
    (
        review_claims,
        "CLAIM_RETRY_PROMPT_VERSION",
        (
            "CLAIM_RETRY_PROMPT",
            "CLAIM_REVERTED_PROMPT",
            "CLAIM_NOT_REVERTED_PROMPT",
            "CLAIM_PARTIALLY_JUDGED_PROMPT",
            "CLAIM_NO_VERDICT_PROMPT",
        ),
    ),
    (
        verify,
        "DIFF_SCOPE_DETAILS_PROMPT_VERSION",
        (
            "DIFF_SCOPE_BASE_BRANCH_PROMPT",
            "DIFF_SCOPE_ALLOWED_PATHS_PROMPT",
            "DIFF_SCOPE_HARNESS_PATHS_PROMPT",
            "DIFF_SCOPE_VIOLATIONS_PROMPT",
            "DIFF_SCOPE_TRUNCATION_PROMPT",
        ),
    ),
    (
        verify,
        "PRD_TAMPER_PROMPT_VERSION",
        ("PRD_TAMPER_FIELDS_PROMPT", "PRD_TAMPER_GATES_PROMPT"),
    ),
    (
        verify,
        "SCOPE_UNREADABLE_PROMPT_VERSION",
        ("SCOPE_UNREADABLE_EXPLANATION_PROMPT", "SCOPE_UNREADABLE_REMEDY_PROMPT"),
    ),
    (
        verify,
        "POLICY_ENVELOPE_PROMPT_VERSION",
        ("POLICY_DIFF_UNREADABLE_PROMPT",),
    ),
    # #233, not #303: the between-iteration measurement block, present in
    # an iteration's prompt only when the last iteration's gates failed.
    (
        loop,
        "LAST_ITERATION_MEASUREMENT_PROMPT_VERSION",
        ("LAST_ITERATION_MEASUREMENT_PROMPT",),
    ),
)

BUILDER_PROMPTS: dict[str, str] = {
    name: getattr(module, name) for module, _version, names in _BUILDERS for name in names
}

BUILDER_VERSIONS: dict[str, str] = {
    name: getattr(module, version) for module, version, names in _BUILDERS for name in names
}

#: Every name is exempt from the exact-equality render guard in
#: tests/test_prompt_versions.py: each is one branch of a multi-branch
#: builder (see the module docstring above), so no single production
#: function returns any one of them verbatim.
BUILDER_RENDER_EXEMPT: frozenset[str] = frozenset(BUILDER_PROMPTS)

BUILDER_SNAPSHOTS: dict[str, tuple[str, str]] = {
    "ACCEPTANCE_RETRY_PROMPT": (
        "0b06d06d2b4e32ed7bd37833045d2b95a24b7843e63f1f2da105134be3c883ae",
        "1.0.0",
    ),
    "CLAUDE_MD_LEARNINGS_PROMPT": (
        "769eb292886f035a073fdabefdb6e36a9e364b4d92e8003c81c2ad47095aa647",
        "2.0.0",
    ),
    "CLAUDE_MD_OVERVIEW_PROMPT": (
        "576c0c2b75c5c1321aa5e9044c70904e5c696533271786a0b07ae6d693ca2a09",
        "2.0.0",
    ),
    "CLAUDE_MD_VERIFICATION_PROMPT": (
        "834d83261fca8c051d46101a9aa43e530b13a1dddd60fcd6c7680e1954d59048",
        "2.0.0",
    ),
    "DIFF_SCOPE_ALLOWED_PATHS_PROMPT": (
        "5b2550599700eee6d5a8518edd16c64452a5431e98f18a223c9ec4d8f5a2ea7f",
        "1.0.0",
    ),
    "DIFF_SCOPE_BASE_BRANCH_PROMPT": (
        "628635ca28fd469942e741354617bcfe8865e0ecafb8765545b26a4d809c86aa",
        "1.0.0",
    ),
    "DIFF_SCOPE_HARNESS_PATHS_PROMPT": (
        "b43f6beff374ce2b7a8b57ce1a2c4845129c92f57da34c5abac3de2165cccfb2",
        "1.0.0",
    ),
    "DIFF_SCOPE_TRUNCATION_PROMPT": (
        "c90e3dc5b2aedb5549c8901c52c26ad550073332a637297f29e8175f0923395d",
        "1.0.0",
    ),
    "DIFF_SCOPE_VIOLATIONS_PROMPT": (
        "66e8d8f67d35d7fe5847ad242a0b1c6284b52ed71497ccbfb027d5a14530969c",
        "1.0.0",
    ),
    "IN_LOOP_SCOPE_VIOLATION_PROMPT": (
        "080ba85407fd75c4cc5ea9cfa885290f2cf989c0715c1cf5b7f23555fd311d14",
        "1.0.0",
    ),
    "ITERATION_CONTEXT_CLOSING_PROMPT": (
        "69182df7dffb179be3bb69f296dc09411ab903878cdffbda22f3892125c58ea4",
        "1.0.0",
    ),
    "ITERATION_CONTEXT_CURRENT_PROMPT": (
        "1fdc376755c067e96ba454ef88f236cb2a3115c5a3dcc4786bd5b41b2dd7d5e0",
        "1.0.0",
    ),
    "ITERATION_CONTEXT_HEADER_PROMPT": (
        "61f06ed1ee1d2ec2ba0de8b7d587de7a373e2ce61fcf8dd28742127020f96553",
        "1.0.0",
    ),
    "ITERATION_CONTEXT_HISTORY_PROMPT": (
        "b1e49fa806cdffec95ffc849a7215e9525ac3bfc456640c26a360ab9fdf7d09e",
        "1.0.0",
    ),
    "ITERATION_CONTEXT_NOT_REMEASURED_PROMPT": (
        "804758401e22c5467fc0b42bad3d6c3b10857f3e10d40500e2f492d45c4e3515",
        "1.0.0",
    ),
    "ITERATION_CONTEXT_NOT_REMEASURED_SINCE_PROMPT": (
        "7643bd5bec70def08fcf83a1afa5b56002c91f63b2cd06a3617b4ac35bc773fb",
        "1.0.0",
    ),
    "ITERATION_CONTEXT_RESOLVED_PROMPT": (
        "4d82e5cb634b4407b12dd1ba3644fe3c99ba6c11e430bcb0d83f4bb766a63734",
        "1.0.0",
    ),
    "KNOWLEDGE_CONTEXT_PROMPT": (
        "f89c1fd62121d7569152e08712b5a0d3511b1d74aa98e0dfb8f13b1c14ca4d61",
        "1.0.0",
    ),
    "KNOWLEDGE_OVERFLOW_PROMPT": (
        "10512d293d1c6e0f46b1922626171970bb35979c3d1e85f2374a82779c7d7380",
        "1.0.0",
    ),
    "LAST_ITERATION_MEASUREMENT_PROMPT": (
        "f6f5329dbe821f18fce2ae140c0c798e48fe35f0e34275517e67e995017b337b",
        "1.0.0",
    ),
    "POLICY_DIFF_UNREADABLE_PROMPT": (
        "f628c8378f20bb097023e0a39dc113f268f8e75d7b06755b58f8d9cc97a82677",
        "1.0.0",
    ),
    "PRD_TAMPER_FIELDS_PROMPT": (
        "5262f73dacffed6e33c5b2e4775af0808d5c8d6dbed936f71a852d31079e1ddf",
        "1.0.0",
    ),
    "PRD_TAMPER_GATES_PROMPT": (
        "7527567703aaf7e585806d2c26ff735bd3416e7f4f55d5d08b034edd1afc6ae3",
        "1.0.0",
    ),
    "SCOPE_UNREADABLE_EXPLANATION_PROMPT": (
        "ba997dc039fb09ff4a2f636feac7dad2b7c2f60ce5d2774c9c8bd424bdb47074",
        "1.0.0",
    ),
    "SCOPE_UNREADABLE_REMEDY_PROMPT": (
        "7cc370f16b5c5dcf54c778a8d62b22f8fcfe812cb589ccc0dd950786e7e4c442",
        "1.0.0",
    ),
    "CLAIM_NOT_REVERTED_PROMPT": (
        "d6b43489cf6f89f23f5996f8b992c859305a3bb851a4ed5376d05235e251bb9c",
        "1.0.0",
    ),
    "CLAIM_NO_VERDICT_PROMPT": (
        "9777274411a0cb3ecdf80709632e0e94834bf20807babe069d4733846dd8506a",
        "1.0.0",
    ),
    "CLAIM_PARTIALLY_JUDGED_PROMPT": (
        "abff54828bf5edc6c16ede627ac48aecc8dbffd796b512e8949ca51d3d405009",
        "1.0.0",
    ),
    "CLAIM_RETRY_PROMPT": (
        "98312c3db2e6c5ec153d19b7340701841ea078e51475a9ac3f5d3a376d0194cd",
        "1.0.0",
    ),
    "CLAIM_REVERTED_PROMPT": (
        "e8950e5273a446cc2c6fc3445300c620e97e81410ef4dd7cc9fae8fdf3e0be40",
        "1.0.0",
    ),
}

assert set(BUILDER_PROMPTS) == set(BUILDER_SNAPSHOTS), (
    "BUILDER_PROMPTS (derived from _BUILDERS) and BUILDER_SNAPSHOTS (the "
    "pin) name different fragments: "
    f"in _BUILDERS only: {sorted(set(BUILDER_PROMPTS) - set(BUILDER_SNAPSHOTS))}; "
    f"in BUILDER_SNAPSHOTS only: {sorted(set(BUILDER_SNAPSHOTS) - set(BUILDER_PROMPTS))}. "
    "A name dropped from _BUILDERS must also drop its snapshot row, and a "
    "new fragment needs both a _BUILDERS entry and a snapshot row."
)
