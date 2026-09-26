# A local web UI replaces the terminal UI

This plan is for the owner, who decides whether to build it, and for the engineers who will build it. It proposes a local web page served by kstrl itself that takes over every job the terminal UI (TUI) does today, read-only first and decisions second, and it retires the TUI once the web page has parity. Every screen is mocked up in `mockups/` with real content from the snippetvault build and the e3root integration-loop run; the images in this document are those renders.

Contents: the goal (1), why web and not the TUI or a native app (2), the information architecture (3), each screen and what it answers (4), the parity table (5), the architecture (6), the acceptance bar (7), delivery in slices and TUI retirement (8), what the second design review changed (9), open questions for the owner (10), and the visual system for builders (11).

## 1. Goal

The operator can answer eight questions from one page, on the machine that runs the factory or from another machine, and can act on the answers without the page ever hiding what an action does. The questions, from the UI audit:

- Q1 what is running and in what state
- Q2 what has it cost against the cap
- Q3 why did something fail
- Q4 what needs me
- Q5 how do I act
- Q6 is the agent alive
- Q7 what did the reviewers find and what happened to it
- Q8 is main green after the merges

The TUI answers most of them today, after six audit rounds. The goal of the web UI is to answer all eight by construction rather than by per-screen repair, and to remove the class of defect the audit kept finding.

## 2. Why a local web page, and not the TUI or a native app

The audit's six rounds are the evidence. Three defect classes account for most of the findings, and all three come from the terminal as a medium rather than from any one screen:

1. Clipping at the viewport edge. Tokens cut mid-number (round 0, F1), spec issues cut at the right edge (F11), history notes cut (round 5, H6), CI reasons cut (round 6, K3), config values cut twice (K5), integration round counts cut (K7), the ks serve title cut (K8). A terminal has a fixed number of columns; every fix moved the clipping somewhere else.
2. Modals that do not fit. The retry confirmation wrapped mid-phrase and orphaned text in the pane behind it (round 5, H3); the checkpoint modal scrolled its findings in a 15-row box (round 3, G7 and G8); the moved-project refusal wrapped three absolute paths mid-word (H4).
3. No room for evidence beside the decision. The checkpoint shows a diff excerpt in a box; the inbox states what each choice does in a footer; the failure queue and its scope preview share one column. Each round added a line and cut another.

A web page removes the first two classes outright: text wraps, panels scroll, and a long path is a detail the reader opens. It makes the third class solvable with layout rather than with line budgets. It also keeps what the TUI got right, because the readers behind the TUI (27 of its 59 modules do not import the terminal framework) are reused unchanged.

A native app was considered and rejected, for three reasons the owner has accepted: it is a second language and a second codebase to keep in step with a Python project; it is Mac-only, while the factory runs on any POSIX machine; and it cannot be opened from another machine, which the web page can (section 6.5). I found no evidence in the audit or the code that reverses this. The one thing a native app would add, system notifications, a web page can also do through the browser's notification permission (slice 4).

Why served by kstrl rather than a separate service: PRODUCT.md rule 4 says the files under `.kstrl/runs/<run_id>/` are the record and the dashboard is a view. A server inside kstrl reads those files with kstrl's own readers and calls kstrl's own decision code. A separate service would reimplement both, which is the drift the audit exists to catch.

## 3. Information architecture and navigation

The page is organised as views, one per operator job, with Home as the queue that points into the others. On a desktop the views are a left rail; on a phone they are a bottom bar of five (Home, Runs, Decide, Failures, More).

| View | Route | Job |
|---|---|---|
| Home | `/` | the operator queue: needs you, active, delivery, history |
| Runs | `/runs/<run-id>` | the live board of one run, and from it component detail, gate output, the decompose plan, spec issues and the integration review |
| Decisions | `/decisions`, `/decisions/<item-id>` | the inbox: every item waiting on a person, with what each choice does |
| Failures | `/failures`, `/failures/<run-id>/<component>` | the failure queue with the retry scope, the confirmation, and the blocked states |
| Serve queue | `/serve` | what ks serve is doing and why the next item will or will not start |
| Config | `/config` | resolved values and their sources |
| Learning | `/learning` | recurring failure patterns, trends and learning readiness |
| Start | `/start/<factory,decompose,feature,understand,init>` | the launchers |
| Safe mode | `/safe-mode` | the four signals behind the masthead chip |

The masthead is on every page: project, branch, whether kstrl.toml is valid, the safe mode chip ("safe mode off" when every signal is clear), the ks serve state, and for a run the elapsed clock and the spend readout. A checkpoint has no view of its own in the rail: it appears under Needs you and as a pinned panel on its run's board, and it opens as a page of its own (`/runs/<run-id>/checkpoint/<request-id>`).

Two rules hold across the architecture. First, a decision is never a modal: it is a page with the evidence above the choices, and the page behind it is not needed to understand it. Second, every count on a rail badge or a panel header is the length of the list it opens.

## 4. The screens

Each screen below states what it answers. The renders are at 1440x900; home, decisions, the run board and the checkpoint are also rendered at 390x844, the checkpoint and the integration review are also rendered whole. `mockups/build_mockups.py` generates every page and `mockups/render.sh` renders them.

### Home

![Home](mockups/home.png)

Answers Q4 (Needs you: two decisions and one retryable failure, each with its action), Q1 and Q6 (Active: the factory run with component, phase, iteration, last output age and worker state; the running ks serve item; the queued one), Q2 (the spend readout in the masthead and on the active row: amount, cap, percentage), Q8 (Delivery: every merge to main with the CI state for that exact commit, including an unknown with its reason and a not-yet-read with what will read it), and Q1 across time (History with a state word and reason for every run, and the whole note of the selected row under the table).

At phone width the four panels stack, the actions move under their rows, and the masthead keeps the spend readout.

![Home on a phone](mockups/home-phone.png)

### First run and a broken configuration

![Home before the first run](mockups/home-empty.png)

![Configuration unreadable](mockups/error-config.png)

The empty home teaches the three ways to start. The error state names the file, the line and the parser's message, says which commands refuse, and keeps the readable parts of the page readable.

### Run board

![Run board](mockups/run-board.png)

Answers Q1 (component, state word, phase lamps in order, what it is doing now), Q6 (the Agent panel: last output age against the staleness threshold, and the worker process state), Q2 (Spend: amount, cap, percentage, and whether it is a whole amount or a lower bound), Q8 (Delivery per merge commit), and Q3 in part (a failed row names its failed phase and cause; the detail carries the evidence). The activity feed is the run's event stream with clock times, newest last.

### Component detail with a failed gate

![Component detail](mockups/component-detail.png)

Answers Q3: the attempts with each phase's outcome and duration, the failed gate named with its cause, the evidence path, the last lines of the gate output, and the route to the retry with the CLI equivalent beside it. The skipped dependent is named.

### Gate output

![Gate output](mockups/gate-output.png)

The full output of one gate attempt, wrapped, with the path, a download and a copy control. Nothing is cut.

### Decompose board and spec issues

![Decompose board](mockups/decompose.png)

![Spec issues](mockups/spec-issues.png)

The plan (components, tiers, dependencies, whether each PRD was written), the spend, the architect transcript as text, and the spec issues by severity and kind with the selected issue whole: summary, where in the spec, the suggestion, and what happened to it (closed by the architect and recorded, or a blocker that halted).

### Integration review

![Integration review](mockups/integration.png)

Answers Q7 for the merged feature: the rounds with their outcomes and counts, the five criteria with verdict and reason, every finding with a disposition (open, fixed by which fix, handed off and why), and the selected finding whole. The note at the top says why the run halted and where the decision is.

### Failures, retry scope, confirmation and the blocked state

![Failure queue](mockups/failures.png)

![Retry confirmation](mockups/retry-confirm.png)

![Retry blocked](mockups/retry-blocked.png)

Answers Q3 and Q5. The queue names the gate, cause, attempt count and failed time, and states recovery availability. The scope is stated before the retry is offered: where it starts, what it resets, what stays out, worktree and branch, what is kept, the limits it runs under, what is not repeated and why, and what it is based on. The confirmation repeats the scope and names the command that will run. The blocked state puts the cause first and the three paths behind a disclosure.

### Decisions

![Decisions: merge gate](mockups/inbox.png)

![Decisions: halted run](mockups/inbox-halted.png)

![Decisions on a phone](mockups/inbox-phone.png)

Answers Q4 and Q5. Each item is laid out as evidence first, then choices: what is known about the item (branch, branch head, the gates, whether a pull request exists) and then one control per choice with a sentence stating exactly what it does. The merge gate offers approval in two forms because the CLI's approve both records and starts a run while the TUI's records only; the page names the difference instead of hiding it (round 6, K1). The halted run says plainly that no factory step reads its decision.

### Checkpoint

![Checkpoint](mockups/checkpoint.png)

![Checkpoint on a phone](mockups/checkpoint-phone.png)

Answers Q5 and Q7 at the moment of decision. The checks panel covers every gate before this point, the changed files and the spend so far with its lower-bound marker; the choices are four, each with its consequence, including "Decide later", which states that the run waits. Findings and the diff are below, whole.

### Config

![Config](mockups/config.png)

Every resolved value with a plain label, the key as written in kstrl.toml (kept, because it is what the operator edits), the value in operator terms, and the source. The selected row shows the explanation, the precedence and what the value applies to. Filter at the top.

### Learning

![Learning](mockups/learning.png)

The recurring patterns (or the sentence that says there are none and what a pattern is), the trends per finished run, and learning readiness in words rather than field names.

### Start

![Start a factory run](mockups/start.png)

![Initialise a project](mockups/start-init.png)

The launchers: factory, decompose, feature, understand and initialise as tabs of one page. Every field says what it resolves to when unset; the preflight panel shows what is checked before anything is spent, including the factory lock held by the live run. Initialise is detect, preview, then write, and says that an existing file is never overwritten.

### Serve queue

![Serve queue](mockups/serve.png)

Answers Q1 for unattended work: the daemon's state and pid, each queue item with its state, source and run, the journal, and the admission checks with the reason the next item will or will not start.

### Safe mode

![Safe mode](mockups/safe-mode.png)

The four signals with their state, the sentence each signal reports, and the runbook section that recovers it.

### Empty states

![No decisions](mockups/inbox-empty.png)

![No failures](mockups/failures-empty.png)

One sentence says what is absent and where the related things are; no table header and no action is offered.

## 5. Parity table

Every screen and modal in `kstrl/tui/screens/` and `kstrl/tui/app.py`, and its replacement.

| TUI screen or modal | Web replacement | Note |
|---|---|---|
| `HomeScreen` (home shell: context, needs you, active, delivery, history, commands, preview) | Home | The commands column becomes the Start view and the rail; the preview board becomes the run link on each active row |
| `OverviewScreen` (run board) | Run board | Same reducer state; delivery gets its own panel |
| `ComponentScreen` (component detail) | Component detail | Failed gate, findings and transcript on one page; the transcript follows live |
| `GateLogScreen` (full gate output) | Gate output | Adds download and copy path |
| `DecomposeScreen` (plan, issues strip, architect output) | Decompose board | Architect output shown as text, not JSON |
| `SpecTriageScreen` | Spec issues | Adds the filter and the "what happened to it" line |
| `IntegrationScreen` | Integration review | Same three readers |
| `RetryScreen` (failure queue, scope, carry) | Failures, Retry confirmation, Retry blocked | The refusal class "this screen cannot carry it" goes away because the web server spawns `ks retry` with the recorded options (section 6.3) |
| `InboxScreen` | Decisions | Approve is split into "approve and run" and "approve only" so the two existing behaviours are both stated |
| `CheckpointModal` | Checkpoint page | No longer a modal; reachable from Needs you and the run board |
| `OptionsModal` (retry confirm, feature review gate, evolve apply, guard and iteration prompts) | Retry confirmation for the retry; a generic decision page for the rest | Same request shape (`PromptRequest`); the generic page renders the header, the options and the detail |
| `QuitModal` (stop the run) | A "Stop this run" control on the run board with the same consequence text | Only for runs the web server started (section 6.3) |
| `SafeModePanel` | Safe mode | Same reasons and runbook anchors |
| `ConfigScreen` | Config | Adds plain labels; keeps the kstrl.toml keys |
| `EvolveScreen` (patterns, trends) | Learning | Readiness in words |
| `FactoryLaunchForm`, `DecomposeLaunchForm` | Start (factory, decompose tabs) | Adds preflight and the resolved defaults per field |
| `InitWizardScreen` | Start (initialise tab) | Same three steps |
| Feature and understand launchers (today: "shell: ks feature --tui") | Start (feature, understand tabs) | The web server spawns the command; the run opens as a board |
| Home commands and the command palette | The rail and Start | No key strip; a keyboard shortcut list is a slice 4 item |
| The masthead, safe mode chip, cost meter | The masthead | Same sources |
| The transcript follow toggle | Follows live by default; pauses when the reader scrolls up | |

Dropped: nothing. The `ks dash` and `ks status --tui` entry points remain as commands that open the browser at the run's board (section 8).

## 6. Architecture

### 6.1 The server

A new command, `ks web [--port N] [--host 127.0.0.1] [--open]`, starts a small HTTP server inside kstrl. It serves three things: the static page (one HTML file, one stylesheet, one script, packaged in the wheel under `kstrl/web/static/`), a JSON API under `/api/`, and an event stream under `/api/runs/<run-id>/events` (server-sent events, which are plain HTTP and need no extra dependency). The standard library's threading HTTP server is enough for one operator on one machine; the choice between it and a small framework is an open question (section 10), because kstrl's core dependency set is deliberately small.

The page is a plain script that fetches JSON and renders; no build step, no package manager, no bundler. The mock-ups are the contract for what it renders.

### 6.2 Reading state and live events

The API is a thin JSON layer over readers that already exist and already run off the terminal event loop:

| API | Reader |
|---|---|
| `/api/home` | `tui.operator_queue.build_queue`, `tui.home_data.gather_home`, `tui.delivery.read_delivery`, `tui.serve_view.read_serve_state`, `safemode.safe_mode_reasons` |
| `/api/runs/<id>` | `reducer.load_run_state` on `events.jsonl`, `tui.state.StateStore` for the manifest join, `tui.run_status` for the state word and reason, `tui.agent_health.agent_health` |
| `/api/runs/<id>/events` | `tui.tail.RunTailer` polled at the TUI's measured 0.2s and pushed as server-sent events |
| `/api/runs/<id>/components/<cid>` | the component's `ComponentState`, the gate logs under `.kstrl/debug/`, the transcript tail |
| `/api/runs/<id>/integration` | `tui.integration_view.read_integration_review` |
| `/api/runs/<id>/decompose` | the `spec_issue_recorded` events and `scripts/kstrl/decisions.json` |
| `/api/failures` | `tui.operator_queue.failure_queue`, `tui.retry_scope.retry_scope`, `retry_plan.preview_retry`, `retry_plan.plan_resume` |
| `/api/decisions` | `inbox.Inbox`, `tui.inbox_consequences.consequences` |
| `/api/serve` | `tui.serve_view.read_serve_state`, the queue journal |
| `/api/config` | `config_report.build_config_report` |
| `/api/learning` | `evolve_report`, the evolution journal and experiments file |
| `/api/ci` | `ci_state.read_ci_ledger` |

Each reader's dataclass is serialised as it is. Where a reader returns rich text for the terminal, the web layer takes the underlying fields, not the rendered text. The rule from PRODUCT.md holds: nothing is shown that is not reconstructable from the files; the server keeps no state of its own beyond the processes it started.

### 6.3 Decisions call the code the CLI calls

| Action | Code path | Same as |
|---|---|---|
| Approve, reject, snooze an item | `inbox.Inbox.approve`, `.reject`, `.snooze` | `ks inbox approve/reject/snooze` |
| Approve and run | the above, then a factory run started as `ks serve` starts one (`serve.subprocess_factory_runner`) | `ks inbox approve` from a shell |
| Retry | `retry_plan.preview_retry` and `plan_resume` for the scope; then `ks retry <component>` spawned as a child process with the recorded options | `ks retry` |
| Checkpoint answer | a `PromptResponse` delivered to the run's channel (section 6.4) | the TUI's checkpoint modal |
| Start factory, decompose, feature, understand | the command spawned as a child process, the same way `ks serve` spawns a run, with the process group recorded so "Stop this run" can end it | the CLI |
| Initialise | `init_wizard.detect_context`, `plan_scaffold`, `init_cmd.run_init` | `ks init` and the TUI wizard |

A test asserts, per action, that the web handler calls the same function as its CLI command. That is the mechanism behind "never reimplement".

Retry through the web removes one refusal class. The TUI could carry only two recorded flags and no run limit, so it told the operator to retry from a shell (round 3, G1; round 5, H5). The web server spawns the real `ks retry` command, which reads the launch record itself, so the only refusals left are the record's own: a moved project (the blocked screen) and a missing limit, which the confirmation page asks for per limit, as the CLI's options do.

### 6.4 The checkpoint across processes

Today the checkpoint prompt lives in the factory process: the pipeline calls `InteractionChannel.request` and blocks until the TUI, embedded in the same process, answers through `QueueInteractionChannel`. When no UI can answer, the pipeline parks the component and files a merge-gate inbox item, and the next run applies the decision. A web page in another process cannot answer an in-process prompt.

Two ways exist. The first keeps today's shape: a run started by the web server runs in the server's process as the TUI's embedded mode does, and the checkpoint is answered in-process. That covers runs the web page started and no others. The second is a small file-backed channel: the pipeline writes the pending `PromptRequest` to `.kstrl/runs/<run-id>/prompts/<request-id>.json`, waits on the answer file, and falls back to today's park when the wait expires; any UI (web, TUI, a new `ks checkpoint answer` command, another machine over ssh) answers by writing the answer file. I recommend the second, because it makes the checkpoint answerable from anywhere the files are, which is the point of a web page, and because the fallback is the behaviour that already exists. It is an owner decision (section 10).

### 6.5 Binding, authentication, and another machine

The server binds `127.0.0.1` by default. Every response carries `Cache-Control: no-store`. Every state-changing request must carry a token the server printed at start (kept in a cookie set on the first visit through the printed URL) and an `Origin` header that matches the server, so a page from another local origin cannot act. Reads are unauthenticated on loopback, as the TUI's reads are.

Another machine reaches it the way the TUI is reached over ssh today: `ssh -L 7777:127.0.0.1:7777 host`, then the same URL. Binding a non-loopback address is an explicit flag, and with it the token is required for reads too. No TLS is offered; the ssh tunnel is the transport.

## 7. Acceptance bar

The bar from the audit (A1 to A8), restated for the web, plus three that the architecture makes checkable.

- A1 Every active run shows state, component or phase, progress and last output age at 1440 and at 390 wide, with no horizontal scroll.
- A2 Every cost summary shows the amount, the cap or "no cap", and a percentage computed with one rounding rule; a lower bound says so in words.
- A3 Every failure names the gate, attempt count, cause, evidence location, and an action whose availability agrees with its scope.
- A4 Every decision shows what each choice does before the operator acts, on the same page as the evidence.
- A5 Every review finding has a visible disposition, and every count agrees with the list it summarises.
- A6 Every merged release shows the CI state for its exact main commit; unknown states why and never reads as green.
- A7 No actionable text, path, log or explanation is cut; the full text is one control away.
- A8 No operator-facing screen shows an internal id, raw JSON, a language representation or a raw field slug where a plain label conveys the fact. The kstrl.toml keys on the Config screen are the exception, because the operator edits them.
- A9 Every action handler calls the function its CLI command calls, asserted by a test.
- A10 Every value on a page is derived from files under `.kstrl/` or from the running command's output; the server holds no other state.
- A11 The page works with the same fixtures the TUI screenshot harness uses, so the audit can continue on the web with the same scenarios.

### 7.1 Definitions a builder must not guess

The second design review asked for these; each is a rule the mock-ups follow.

- Counts. The Decisions badge counts open inbox items plus open checkpoints. The Failures badge counts failed components with an available retry. Home's Needs you count is their sum. A snoozed item is not open. Every badge is the length of the list it opens.
- Cost. The amount is the run's reported cost to the cent. The percentage is the ceiling of the unrounded amount divided by the cap, so the display never implies less spent than the truth; the History panel says "percentages round up". A run with unreported calls shows "at least" before the amount and the percentage is a lower bound too. No cap reads "no cap" and shows no percentage. On a phone the same three facts appear as "$19.24 of $78 · 25%".
- The current main commit. Delivery opens with "main is at <sha>", the newest merge commit the run records recorded, and its CI state from the CI ledger with the reason and the reading's age. A merge with no reading says "not read yet" and what will read it (ks serve refreshes on its own; a "read now" control runs `ks ci poll`).
- Finding dispositions. Every integration finding is exactly one of open, fixed (by which fix), or handed off (with why); the three counts sum to the number of findings, and "handed off" is not a subset of "open". Review and security findings on a component carry their severity and phase; a disposition for those is a slice 2 item, because no record of one exists yet beyond the retry loop.
- Clocks. "Last event" is the age of the newest event in the run's event stream. "Last output" is the age of the newest write to the component's agent transcript; it is stale after the configured threshold, which the Agent panel names. "Worker alive" is a process check on the pid in the newest heartbeat event, made by the server on a 5-second cycle and shown with its age ("checked 2s ago"); "process unknown" when there is no heartbeat. "Elapsed" is the run's clock since its first event. Each is labelled with these words wherever it appears.
- Stale decisions. Every action carries the fact it was shown: the branch head for a merge gate, the run id and manifest for a retry, the request id for a checkpoint. The server refuses an action whose fact has changed, returns the changed fact, and the page shows the current scope before offering the action again.
- Sources. Values come from the run's files (events, manifest, launch record, inbox, CI ledger, integration state, knowledge, journals) or from a live observation the server writes down before showing (a process check, a CI poll). A live observation carries its source and time on screen ("read 2s ago", "checked 3s ago").
- Phone layout. The rail becomes a bottom bar; the masthead keeps the project and the compact spend; panels stack in reading order (evidence, then the choices); every table becomes labelled stacked rows; long text wraps; nothing scrolls sideways; the bottom bar never covers the last control because the page keeps a margin above it.

## 8. Delivery in slices, and retiring the TUI

Each slice ships on its own and is usable without the next.

1. Read-only core: `ks web`, Home, Run board, Component detail, Gate output, History, the masthead, Safe mode. The readers exist; this slice is the server, the page shell and the first six views. `ks dash` gains `--web`, which starts the server if needed and opens the browser at the run.
2. Read-only rest: Decompose board, Spec issues, Integration review, Failures (scope shown, retry withheld), Decisions (read, choices withheld), Serve queue, Config, Learning, the empty and error states.
3. Decisions: inbox approve, reject and snooze; retry with confirmation and the blocked states; the checkpoint through the file channel (or in-process, per the owner's answer); Start for factory, decompose, feature, understand and initialise; Stop for runs the server started.
4. Finish: the phone layout verified on a phone, browser notifications for Needs you, keyboard shortcuts, and the screenshot harness pointed at the web page.

Retirement: after slice 3, the owner runs one real build on the web page alone. If the parity table holds and the audit's scenarios pass on the web, `ks` with no arguments opens the browser instead of the TUI, `ks dash` and `ks status --tui` do the same, and the TUI code is removed one release later. The 27 non-terminal readers stay where they are; they are the web page's data layer.

## 9. What the second design review changed

Two rounds of review by a second designer were run against the rendered mock-ups, with the eight decisions, the acceptance bar and the operator questions as the brief. I checked each point against the screens before acting on it.

Round one ranked nine defects. I accepted eight and changed the mock-ups and the plan:

1. The Decisions badge counted decisions plus failures while the page said "2 open". Now each badge counts the list it opens (section 7.1).
2. Cost percentages had no stated rule (56% for 19.30 of 35.00). The rule is now stated on the History panel and in section 7.1: round up from the unrounded amount.
3. The integration header said "4 findings open" while the list held 3 open, 1 handed off, 1 fixed. The header and the Findings panel now state the three counts and say they agree with round 3.
4. Phone cost summaries omitted the cap. They now read "$19.24 of $78 · 25%".
5. Delivery listed merges without saying which commit main is at. It now opens with "main is at <sha>" and that commit's CI state.
6. Phase lamps depended on a legend. The column header now names the seven phases in order.
7. Three clocks ("age", "last output", "last event") were unlabelled. History's column is now "Last event"; section 7.1 defines all four clocks.
8. Home's Active rows showed a pid and a queue id ahead of the work. The pid moved to the run board's Agent panel; the queue id moved after the title.
9. Phone coverage was thin. The run board and the whole checkpoint page are now rendered at phone width, and every table stacks into labelled rows there.

Two of its points I rejected. It disagreed with the local-only server because "a server bound to 127.0.0.1 cannot be opened from another machine"; section 6.5 already specified the ssh tunnel, so the design did not change. It disagreed with "every value is reconstructable from the files" on the grounds that liveness and CI need a live source; the files already carry those observations (the CI ledger, the heartbeat event) with their times, so the rule stands and section 7.1 now says a live observation is written down before it is shown.

Round two re-judged the changes against the revised renders. It scored four of the nine resolved (counts, phone cost summaries, the current main commit, phase names) and five partly, where "partly" meant a static render cannot prove a rule (the rounding calculation, that every table stacks) or the crop hid the evidence (the findings list was below the fold, so the integration review is now also rendered whole, `mockups/integration-full.png`). It found one new defect, which I accepted: "worker alive" is a live observation and showed no check time, so the Active rows and the Agent panel now say "checked 2s ago" and section 7.1 defines the check. It repeated that the queue item ids on Home's Active rows fail A8; I accepted that too, and they now appear only on the Serve queue page, where the operator acts on them. Its verdict: ready to build from, with the unproven acceptance points carried into implementation checks, and the largest remaining operator risk being a stale live status read as current, which the check time and section 7.1's stale-decision rule address.

A mechanical design check over three pages after the rounds found three things: muted text on the selected row's tint (darkened), header labels at 11px (raised to 12px), and uppercase panel labels (kept; they are labels, not body text).

## 10. Open questions for the owner

1. The server: the standard library's threading HTTP server (no new dependency, server-sent events written by hand) or a small framework as an optional extra, as the SDK agent is today.
2. The checkpoint channel: in-process only (covers runs the web page started) or the file-backed channel (covers every run and every UI, with today's park as the fallback).
3. "Approve and run" as the primary control on a merge gate: it matches the CLI and starts a spend. "Approve only" matches the TUI and starts nothing. The mock-up makes the first primary.
4. Whether `ks serve` should host the page itself when it is running, so one process serves both, or `ks web` stays a separate command.
5. Whether the TUI is removed one release after parity or kept as a fallback for machines without a browser.

## 11. Visual system for builders

The page is a light grey-green ground with dark insets for dense data (logs, diffs, the phase matrix), small rectangular coloured indicators (lamps) for state, one amber accent, one sans face for the interface and one mono face for identifiers, numbers and logs. The one commitment kept from the TUI is the amber: selection, focus, primary action and the running thing.

Tokens (from `mockups/build_mockups.py`, the source of every mock-up):

| Role | Value | Use |
|---|---|---|
| ground | `#d3d9d1` | page |
| face | `#eceeea` | panels, masthead |
| glass | `#141a1c` | insets: lamp matrix, logs, diffs, feeds |
| ink, muted | `#161c19`, `#4b544f` | text on the ground (both above 4.5:1) |
| amber lamp, amber ink | `#e5a84f`, `#7a4a00` | running, primary action, selection |
| passed | `#79c26b` lamp, `#23611f` ink | completed, passed, CI passed |
| failed | `#e26d5a` lamp, `#8f2214` ink | failed, blocking, CI failed |
| unknown | `#d9b036` lamp, `#6b5200` ink | unknown, halted, caution |
| verifying | `#82a7ba` lamp, `#2f5a6c` ink | the adversarial phases while running |
| parked | `#b48ec9` lamp, `#5a3a70` ink | merge parked, handed off |

Rules:

- A lamp never appears without its word. Colour is never the only signal.
- Numbers are tabular and set in the mono face; a lower bound is written as "at least".
- Elevation is a 1px rule; there are no shadows. Corner radius is 4px on panels and 3px on lamps and controls.
- The only motion is the running lamp's pulse, and it stops under a reduced-motion preference.
- Decisions are pages, not modals. The evidence is above or beside the choices, never behind them.
- At phone width the rail becomes a bottom bar, panels stack in reading order (evidence first, call second), tables become stacked rows, and nothing scrolls sideways.
- Faces: B612 for the interface (a face designed for aircraft cockpit displays), JetBrains Mono for data. Both are open and self-hostable; the mock-ups load them from a font service, the product should ship them in the wheel.
