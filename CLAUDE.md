# CLAUDE.md - kstrl

## Project Overview

- **Language**: Python (FastAPI / pytest / uv toolchain)
- **Project**: kstrl - a software factory for AI coding agents, built as a loop that closes on independent measurement rather than on the agent's own report
- **Layout**: `kstrl/` is the canonical factory implementation and the only Python package.

## Verification commands

- **Test**: `uv run pytest tests/ -v`
- **Calibration (opt-in, real LLMs)**: `KSTRL_RUN_CALIBRATION=1 uv run pytest tests/test_calibration.py -v`
- **Typecheck**: `uv run mypy kstrl/ --strict`
- **Lint**: `uv run ruff check kstrl/ tests/`

Note on mypy scope: `pyproject.toml` declares `[tool.mypy] files = ["kstrl"]` so `uv run mypy` (no args) also checks `kstrl/`, keeping it in lockstep with the factory's smart-default typecheck command.

## Adversarial role taxonomy

kstrl's factory uses eight distinct roles. Three are LLM-driven adversarial passes; the rest are mechanical or computational. Full taxonomy with file:line references is in [docs/adversarial-design.md](docs/adversarial-design.md).

| Role | Prompt | What it catches |
|---|---|---|
| Architect / PRD red-team | `decompose.DECOMPOSE_PROMPT` | Spec ambiguity, missing failure modes, unstated assumptions |
| Engineer | per-project `scripts/kstrl/prompt.md` | Implements one story per iteration; emits required `## Self-Critique` block |
| Mechanical verifier | `verify.run_mechanical_verification` (no LLM) | tests / typecheck / lint / diff-scope / bad-patterns / self-critique-shape |
| Code reviewer | `review.REVIEWER_PROMPT` | PRD criteria + concerns (scope_creep, security_concern, test_quality, etc.) |
| Security reviewer | `security.SECURITY_PROMPT` | OWASP-mapped vuln categories |
| Contract tester | `contract.run_contract_testing` (no LLM) | Cross-component integration tests on merged tier branches |
| Knowledge distiller | `knowledge.DISTILL_PROMPT` | Durable facts about the artifact, written pre-PR (after the review gates pass, before the PR merges main in) |
| Human checkpoint (E6) | interactive UI | Optional opt-in approval before PR merge |

## When working on this codebase

- **Do not run `/code-review` on your own code.** Per H1 of `docs/adversarial-roadmap.md`, AI self-review of AI-generated code is prohibited. The user or `/code-review ultra` is the gating reviewer.
- **Calibration is the truth signal.** When changing ANY enrolled prompt (the nine in `tests/test_prompt_versions.py::_PROMPTS`: `DECOMPOSE_PROMPT`, `REVIEWER_PROMPT`, `SECURITY_PROMPT`, `DISTILL_PROMPT`, `VERIFY_COMMANDS_PROMPT`, `REPO_CHANGE_SOURCE_PROMPT`, `PASTED_CHANGE_SOURCE_PROMPT`, `DEFAULT_PROMPT` for the engineer role, and `DECISIONS_CONTEXT_PROMPT` since #260), re-run the calibration suite and compare detection rates against the saved baseline. A prompt edit without a calibration check is treated as untested (H2). One exception, stated rather than left implicit: `DECISIONS_CONTEXT_PROMPT` is engineer-facing CONTEXT, and the calibration suite scores the architect, reviewer, security and distiller roles against planted-bug fixtures, so it is enrolled for H3 and has no calibration fixture to re-run. Enrolling a prompt that no fixture scores does not create an H2 obligation it cannot discharge; it does create the H3 one.
- **Prompt edits require a version bump AND a hash update.** Every adversarial prompt (including the harness-shipped engineer prompt `DEFAULT_PROMPT`) declares a `*_PROMPT_VERSION` semver constant next to the body. `tests/test_prompt_versions.py` snapshots each prompt as a `(hash, version)` tuple in `_EXPECTED_SNAPSHOTS`; both must move together. The test also AST-walks `kstrl/` for any `*_PROMPT` constant and fails if a new one is not enrolled. The walk is depth-agnostic (a binding inside a function or class body counts) and keys on the target NAME plus a value it cannot prove is a non-string, so a body never bound to a `*_PROMPT` name - returned straight out of a function, or bound to a local called something else - is invisible to it: hoist such text to an enrolled constant and interpolate the run-time values back in (H3, #299). The audit trail is the PR diff with prompt body + version constant + snapshot tuple all moving (H3).
- **A `DEFAULT_PROMPT` bump also appends a scaffold-ledger row.** `ks init` never overwrites `scripts/kstrl/prompt.md`, so a version bump alone reaches no already-initialised project. `SCAFFOLDED_TEMPLATES` in `kstrl/init_cmd.py` records the SHA-256 of every body each scaffolded template has ever shipped; append the new row, never edit or drop an older one, because an old row is the only thing that can recognise a copy already on someone's disk (H3b).
- **Be explicit about what was tested vs assumed.** "Smoke passed" without listing what was checked is presence-testing, not behavior-testing (H4).
- **All adversarial-roadmap policies are tracked in `docs/adversarial-roadmap.md`**. Read it before changing the role architecture.

## Coding standards

- Type hints on all function signatures
- `from __future__ import annotations` at the top of every file
- `T | None` over `Optional[T]`; `A | B` over `Union[A, B]`
- `@dataclass` for data containers, `frozen=True` when immutable
- `Protocol` for interfaces (structural subtyping over inheritance)
- snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE for constants
- Absolute imports grouped: stdlib, third-party, local
- No bare `except:` - always specify the exception type
- No mutable default arguments

## Implementation principles

### Adversarial mindset for any role-related code

The whole factory rests on the idea that adversarial framing causes the LLM to find bugs it would otherwise miss. When editing prompts or role code, ask: "does this make the role more skeptical, or more eager to please?" Prefer the former.

### Calibration over claims

Any change to an adversarial role should include either a calibration delta or a test against the planted-bug fixtures in `tests/adversarial_fixtures/`. Self-reported flags like `exhaustively_searched` are hints, not signals - they cannot be trusted alone.

### Halt over heroics

The architect halts when it ESCALATES: a product, scope or risk judgement, or a choice between incompatible architectures that is expensive to unwind. Everything else it closes itself and records in `scripts/kstrl/decisions.json` (#260). Hard-mode reviewers halt on findings at or above the threshold. The pipeline should fail loudly when something is wrong, not silently degrade.

A gate that keys on parsed output must reject what it cannot parse, never count it as zero. Round 1 of #260 compared a COUNT of blocker-severity issues against a COUNT of parsed escalated decisions; a disposition of `"Escalated"` parsed to nothing, both counts fell to zero, and the two zeros agreed. Nine malformed shapes validated and produced a PRD and a manifest. The rule this leaves: validate the RAW payload entry by entry with an indexed message before anything is parsed, and join records by an id in the schema rather than by counting them. Validate against the SAME vocabulary the parser uses, from the same constant. The round-2 review of #260 found the identical fail-open one field over: the new raw validator took `severity` verbatim while `_parse_spec_issues` checked it against `_VALID_SEVERITIES`, so a severity of `"Blocker"` validated, was closed by a `decided` decision, and then vanished from the artifact, the routing and the UI. Two definitions of a valid record mean the weaker one is the one the gate consults. `enum_field_error` and `required_field_error` in `kstrl/decisions.py` are the one place both are now written.

Halting is not free. Five real runs against a real spec halted 5 of 5 on blocker-severity findings, and not one of the 26 blockers was a judgement only the owner could make: the architect halted on questions its own `suggestion` field had already answered. "Vague" means "the architect could not answer", not "the architect had a question".

### Audit trail

Every adversarial decision writes a record: review/security findings go to PR bodies, knowledge facts go to disk, evolution journal records component outcomes. Don't add silent code paths - if it's worth deciding, it's worth recording.

## What NOT to do

- Do NOT run `/code-review` on your own code (H1).
- Do NOT ship a prompt change without re-running calibration (H2).
- Do NOT update the hash in `tests/test_prompt_versions.py` without also bumping the matching `*_PROMPT_VERSION` constant (H3). The two changes always travel together.
- Do NOT edit or delete a row in `SCAFFOLDED_TEMPLATES`; only append (H3b).
- Do NOT use `pickle` to load untrusted data; the existing `tests/test_phase_c_coverage.py` C8 pickling test only round-trips configs we constructed in-test.
- Do NOT add unverifiable self-report claims to results without flagging them as hints (E9 added `infrastructure_error` precisely to distinguish verified from claimed; E3-infra lifts the same signal into the `Finding` stream so `len(findings)==0` is a safe "ran cleanly" check).
- Do NOT bypass the budget cap (`max_adversarial_calls`) without explicit user opt-in.

## Agent Learnings

> Maintained by agents working on this codebase.
> Append patterns, gotchas, and conventions discovered below.

### Codebase Patterns

- Atomic file writes go through `kstrl/atomicio.py` (`atomic_write_text` / `atomic_write_json`), never a hand-rolled `tempfile.mkstemp` + `os.replace`. `mkstemp` creates 0600 and `os.replace` carries that onto the destination, so a hand-rolled copy silently retightens an operator's 0644 file; #291 found ten copies, nine downgrading the mode and five of those also leaving the encoding to the locale. The helper preserves an existing file's mode, creates a new one at the umask default, and deliberately has no `mode` parameter. It does NOT preserve link identity: `os.replace` swaps the directory entry, so a symlinked destination becomes a regular file. `tests/test_atomicio.py` AST-walks `kstrl/` and fails on a new `mkstemp` call.
- **Encoding is a two-sided contract.** `atomicio` pins utf-8 on write and leaves `ensure_ascii` at its default, so JSON lands as pure ASCII that any locale can read back. Do not "improve" that to `ensure_ascii=False`: #291 tried it, and it made kstrl the SOURCE of non-ASCII bytes on ordinary LLM output (one curly quote in a component description), which broke six readers under `LC_ALL=C`. A reader of any file kstrl writes must still name `encoding="utf-8"`, and must catch `ValueError` alongside `OSError`, because `UnicodeDecodeError` is a `ValueError` and escapes a fail-closed `except OSError`. `init_cmd._read_text_or_none` is the worked example.
- Tests assert on a process they can name (a pid from a pidfile, a pgid they created), never on what else the machine is running. `pgrep -f "sleep 60"` matches an unrelated `sleep 600` in any session, which is #292. Helpers live in `tests/helpers/procs.py`; `tests/test_process_scoping.py` AST-walks `tests/` and fails on a new `pgrep`/`pkill`/`killall`/`pidof`.
- **A parser's error taxonomy belongs to the parser.** A `tomllib` reader catches `Exception`, never an enumeration of what it is believed to raise. `tomllib.load` decodes the stream before it lexes and parses by recursive descent, so it raises `TOMLDecodeError`, `UnicodeDecodeError`, plain `ValueError` (CPython's 4300-digit integer-string limit) AND `RecursionError` (~496 nested arrays; a `RuntimeError`, not a `ValueError`). #318 enumerated subclasses three times and was wrong three times, each escape taking 13 of 16 CLI commands down with a raw traceback, because `config_preflight` reads the document for every non-exempt command. Round 2 is the instructive one: it stated this rule correctly and then wrote `ValueError` as the ceiling in its docstring, its AST guard AND this line, so a future author could satisfy all three and still ship the hole. Four rules, all of them checkable. (1) `Exception` exactly: narrower is the defect itself, and `BaseException` or a bare `except:` is also an offender, because everything about the DOCUMENT derives from `Exception` while `KeyboardInterrupt`/`SystemExit` are about the process. (2) Broad clause LAST, or every specific message above it is unreachable. (3) ALL the I/O outside the guarded block - `path.read_bytes()` then `tomllib.loads(raw.decode())`, which raises the identical four types - so no widening can reach an `OSError` or a null-byte path's own `ValueError`; that is stronger than re-raising `OSError` from inside the guard, and it deletes a special case no test could enter. (4) Report individually only the causes you can actually name. `kstrl/config.py::load_toml_document` is the worked example. `tests/test_toml_readers.py` AST-walks `kstrl/` and fails rules 1 and 2, resolving `import tomllib as _tl`, `from tomllib import load` and module rebinds, and attributing a parse to its innermost SCOPE so a helper defined in a `try` and called elsewhere is not credited to it; it does not see a parse reached through a name it cannot resolve, and says so.
- An artifact one phase writes and another phase READS must carry the identity of the thing it belongs to, and the reader must check it. `kstrl/decisions.py` writes `project` and `specFile` into the register and `bind_register` refuses a register whose pair does not match the manifest about to be scheduled; round 1 of #260 discarded both on read, and a factory run on `project-b/b.md` with project A's register beside it handed the engineer project A's binding instruction, because both happened to have a component called `comp-a`. Three further rules came out of the same finding: write the dependent artifact AFTER the thing it depends on commits (the register is written after `manifest.save`, so a decompose that dies later leaves no register beside the older manifest); stamp the states the reader must refuse (`halted`) rather than hoping it infers them; and make unreadable a REFUSAL, not an empty read, because an empty read is a silent removal of the mechanism. A failed WRITE is the same silence from the other end: if a later phase reads the artifact, the write failure is the writing phase's failure, so `_write_decompose_artifact` takes `required=True` for the register and lets the `OSError` out. Round 2 swallowed it, and one full disk would have left a manifest whose register was missing, which reads as the legal pre-feature state and disables the mechanism for every later run with no message anywhere. Route the refusal through the surface the other pre-spend refusals use (`_report_preflight`, exit code 2), not out of the top of the run as a traceback.
- **A static guard fails in the skip direction, so design it to fail red instead.** This is the most-repeated defect in the repo: eleven logged instances of *a guard that passes the mutation it exists to catch*, every single miss in the skip direction, the guard going blind rather than red. Four rules came out of them. (1) Prefer **closed by construction** over a **ledger of give-ups**: inventory every place the resource is OBTAINED and count what you saw, so a new shape shows up as an unexplained census delta; a ledger of the places the walk gave up is closed only over the shapes someone already enumerated. `EXPECTED_JOURNAL_PATH_SITES` in `tests/test_journal_one_writer.py` is the worked example, and `tests/test_event_names_have_one_home.py` is the current best: its outer layer enumerates no node types at all and counts every expression whose folded value is an enrolled name. (2) **`assert hits(...) == []` is not a control.** It passes when the walk is correctly narrow AND when the walk has been switched off, which is the same shape as the defect. Express a genuine disclosed miss as an explicit blind-spot record plus a strict xfail, so a later widening XPASSes and fails loudly instead of silently agreeing. (3) **Know which direction your guard is wrong in.** A guard that FLAGS may over-match: widening costs a false positive someone will read. A guard that CLEARS must be narrow, because over-matching converts a resolution into a clearing and deletes the mechanism; #342 measured exactly that in three of sixteen migrated guards, one of them letting a config load escape onto the Textual event loop. If a clearing guard cannot PROVE a site is compliant, it must flag. (4) **A guard you did not mutate is a guard you did not test**, per layer, not per file.
- **Reading a pin is not running a guard, and a guard must not depend on the code path whose absence it is detecting.** Two ways a control stops being true with nothing failing. The first: a lane reported a census pin identical at two revisions as evidence that a large refactor moved nothing, when EXECUTING the guard at those revisions was red, because the refactor split a file without updating the pin, so the census found `13 + 4` where the pin still said `17`. The identical dict was a symptom of the stale pin, not evidence of stability. Re-derive a census by running it; never by diffing the literal. The second: a runaway-detector fuse sat inside a stubbed `pause` that the very defect it detects stops calling, so it **hung for 26 minutes instead of failing**. Every earlier instance of the class went blind, which at least returns; this one went slow, which is worse. Put the fuse somewhere the defect cannot switch off, such as real time rather than the mocked clock.

- **A mutation result that did not delete `__pycache__` first is not a result.** CPython invalidates bytecode on `(mtime_seconds, size)`, so two edits of the same size inside one second reuse stale bytecode and report a FALSE PASS. Every mutation measurement runs as `find . -name __pycache__ -type d -exec rm -rf {} + ; PYTHONDONTWRITEBYTECODE=1 uv run pytest ...`. This has produced a wrong answer in this repo more than once.
- Shared AST-walking machinery for static guards lives in `tests/helpers/astwalk/` (#324), not hand-rolled per guard. Measure the fit before adopting it rather than assuming it: one lane found three of twenty helpers were drop-in and fifteen had no equivalent, five of those needing a parent map astwalk deliberately does not keep, being strictly top-down. `Bindings.attributes` marks an origin it inferred as `guessed`, and a `guessed` origin must not be enough to CLEAR.
- Cross-module JSON extraction from agent output reuses `decompose._extract_json` + `decompose._select_agent_output`.
- The review and security reviewers do NOT receive a diff: they run with `cwd` set to the worktree and are told to run git themselves (#266). Paste sites that remain (HITL checkpoint excerpt, knowledge distiller) truncate through `git.truncate_diff_for_prompt`.

### Gotchas

- `os.replace` is not atomic on Windows; the codebase is POSIX-first.
- `fcntl.flock` (Phase A4 concurrent worktree lock) is POSIX-only; tests skip on Windows.
- Confidence value `"verified"` is legacy and aliased to `"review_passed"` on read; new code should use the new tier names.

### Conventions

- Phase numbers are sticky: Phase 0 feedforward, Phase 1 verify, Phase 2 review, Phase 2.5 security, Phase 3 contract. New phases get fractional numbers to preserve ordering semantics.
- Every config dataclass should have `from_env()` AND `load(root_dir)`; the load method reads `[<section>]` from `kstrl.toml` and overlays env on top.
- **One defect class, one PR.** Fix every instance plus the guard that catches instance N+1, rather than one site per PR with sibling issues filed behind it. The issue list grew by thirteen in three days under the per-site habit, and the growth was almost entirely the same five findings re-filed under new numbers. Work that genuinely will not fit goes in the PR body as a measured handoff with the numbers attached, not as a new issue.
- **Merging into a moving main is where the defects are.** Four defects in one batch survived a textually clean merge and were caught only by running the full suite on the merged tree: a fixture id collision, a line-number pin, a constant left with zero readers and both names ungated, and an import hunk. So merge current `origin/main` and re-run the full suite before pushing, and **re-run every static guard's site census before and after the merge and diff the two**, because a code move in another file changes what a walk sees without touching yours.
