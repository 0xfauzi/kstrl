# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work in progress toward the Dark Factory cycle (continuous intake, a release
stage, runtime feedback, and an earned-autonomy ladder). See
[`docs/dark-factory-roadmap.md`](docs/dark-factory-roadmap.md) and the
[R8 milestone](https://github.com/0xfauzi/kstrl/milestone/1).

### Added

- Two of the autonomy ladder's five declared demotion triggers now fire.
  `DemotionTrigger` has listed `CALIBRATION_REGRESSION` and
  `HEALTH_BREACH` since R8.2 and neither had an emitter, so a factory
  getting quietly worse without breaching the policy envelope kept its
  level, and a calibration regression a human had already measured
  changed nothing. `python -m kstrl.calibration compare` takes a
  `--root` and, when the ladder is enabled, opens a `calibration_drift`
  inbox item on a regression; `[autonomy] demote_on_calibration_regression`
  (env `KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION`) additionally revokes one
  level, once per comparison however often that comparison is re-run.
  With the ladder off it says so on stdout, naming the root it consulted,
  rather than doing nothing quietly. The R8.4 half is the seam only: a
  new `health_breach` inbox kind and `[autonomy] demote_on_health_breach`
  (env `KSTRL_AUTONOMY_DEMOTE_ON_HEALTH`), inert until `kstrl/health.py`
  exists and suppressed - out loud - while a cool-down is running. Both
  switches default off, because every threshold in the ladder is still an
  unmeasured placeholder, and they are refused unless written as unquoted
  booleans, because `= "false"` would otherwise arm them. `[autonomy]
  enabled` is read the same strict way for the same reason, which is a
  behaviour change: a config that spells it `"false"` or `"0"` now
  reports a configuration problem instead of quietly switching the ladder
  on. Every automatic
  demotion now goes through one `autonomy.apply_demotion`, so its four
  writes cannot drift apart per trigger. Compare's exit codes are
  unchanged, except that on a regression a `kstrl.toml` that will not
  load or cannot be read exits 2 instead of being read as "ladder off".

### Changed

- A repeat of an open inbox item now refreshes its `evidence` alongside
  its `detail`. Only the prose half was refreshed before, so a deduped
  item whose numbers move - a health breach, whose whole point is that
  the value moves - reported the latest observation in `detail` and the
  first one in the structured payload that `ks inbox` and the TUI render.
  `title` is still not refreshed: it is the row's label, and a repeat
  must not relabel a row somebody has already read.

### Fixed

- A factory run now resolves every configuration section it enforces
  exactly once, at run start, and every phase enforces that resolution
  for every component. Phase 1 re-read `[policy]`, `[adequacy]` and the
  autonomy level from `kstrl.toml` per component while
  `manifest.policyHash` was computed once, so an edit to `kstrl.toml`
  while a run was in flight changed what later components were held to
  without changing the hash that records it: measured, a two-component
  run enforced two different envelopes (`max_files_changed` 5 then 500,
  `deps_allow_new` false then true) against one recorded hash, and the
  adequacy posture flipped with nothing recording either posture. A
  malformed mid-run edit raised out of a per-component load, and its
  caller runs outside the `try` that wraps the component future, so it
  aborted the whole run rather than failing one component; driving a
  two-component run confirms both halves, the abort before and the
  completion after. Phase 1 also used the raw stored autonomy level
  rather than the clamped level the run operates at, so a run clamped
  to L1 by `[autonomy] max_level` judged its adequacy gate at L4 (no
  verdict changed at either level today; both consumers test only
  `>= 1`). `[sandbox]`, `[fixtures]`, `[inbox]` and `[divergence]` are
  resolved with them, which also removes the second `[sandbox]`
  resolution a run used to make. Editing `kstrl.toml` mid-run now has no
  effect on the running factory and takes effect at the next run (#192).

- A configuration section a run cannot resolve is now refused before the
  run starts, with exit code 2 and the section and the offending key
  named, instead of a traceback. The entry preflight resolves every
  section before the command body, but on `ks factory --spec` the
  architect runs between that check and the run itself - measured at 119
  to 210 seconds against a frontier model - so an edit made inside that
  window arrives at the run's own resolution. Because the refusal
  happens before the run directory exists, no run is recorded as having
  cost nothing (#192, #257).

- `dead_code` no longer reports a pass when nothing was measured. One row
  covered two phases - a ruff F401/F811/F841 auto-fix that ran and a vulture
  scan that did not - so nine states in which one of them measured nothing
  still produced `dead_code  pass`, and `kstrl/review.py` copied that row into
  the LLM code reviewer's verification summary as `dead_code: PASS`. The row is
  now two: `dead_code_ruff` for the ruff phase and `dead_code` for the vulture
  or `[verify] dead_code_command` scan. A phase that could not run appends no
  row and is recorded under `not_measured` with its reason (`tool_missing`,
  `no_target`, `timed_out`, `command_failed`), the rule #306 set for
  `mutation_testing`, so a real ruff measurement is no longer discarded along
  with an absent vulture one and an adversarial reviewer is no longer told a
  scan passed that never happened. `[verify] dead_code_cleanup` still owns both
  phases. `ks sense --json` schema 2 -> 3: an absent `dead_code` row now also
  means "asked for, measured nothing", and `dead_code_ruff` is a new name in
  `checks`. Three more states of the same defect go with it: ruff output with
  no summary line in it (a project setting `[tool.ruff] output-format` to a
  non-text format) was read as zero fixes and reported as a clean phase over a
  worktree ruff had just edited and nothing had committed, a diff git could not
  read at all was reported as `no_target` ("nothing to scan, not a fault")
  rather than `command_failed`, and a `git commit` that did not land was
  reported as `ruff auto-fixed N`. `ks feature` now also names `dead_code_ruff`
  among the checks it is not running.

- `ks sense` reports the number of findings ruff would actually remove, not
  every finding it reported: the two differ by ruff's unsafe fixes, so a tree
  where `ks sense` said "3 auto-removable" had 2 removed by the factory. The
  factory row now also carries what was left behind (`ruff auto-fixed 2, 1
  remaining`), which `ruff auto-fixed 0` could not tell from a clean tree.

- The `dead_code_ruff` phase requires **ruff 0.2.0 or newer** (January 2024).
  It pins `--output-format=concise` so a measured project's own `[tool.ruff]
  output-format` cannot remove the summary line the phase parses, and that
  flag value does not exist before 0.2.0: 0.0.272 rejects `--output-format`
  outright and 0.1.0 and 0.1.15 reject the value `concise`. All three exit 2,
  so an older ruff produces a `command_failed` gap carrying ruff's own
  `error:` line rather than a number. A project's own pinned ruff is far past
  0.2.0; the reachable case is `ks sense` against a live checkout with a
  system-wide old ruff first on PATH. The set of output shapes the phase reads
  is now measured against real ruff 0.2.0, 0.3.0 and 0.16.1 over five trees
  rather than reasoned about: a fixing run with nothing safely fixable prints
  `Found N errors.` alone, and ruff 0.2.0 through 0.3.2 print nothing at all
  on a clean tree, and both were being reported as a tool failure over a run
  that had measured something.

- The mutation gate passes its changed files to mutmut as one comma-separated
  argument. `mutmut run` takes one positional slot, so the space-separated
  form was a usage error (`Error: Got unexpected extra argument`) for three or
  more changed non-test Python files and silently consumed the second as that
  positional for exactly two. The gate is opt-in (`[verify] mutation_testing`,
  default off).

- The dead-code detector's file list goes behind a `--` separator, so a
  changed file whose name starts with `-` reaches vulture as a path rather
  than as an option. Without it vulture exits 2 with `unrecognized
  arguments`, which the check read as findings and reported as a dead-code
  failure naming the wrong cause. Related: a detector that exits non-zero and
  reports no finding the check can read is now a `command_failed` gap rather
  than `no remaining dead code`, which is decided on the exit code instead of
  on the output being empty.

- Six more record files survive an interrupted write. A crash leaves a
  tail with no newline, the next append concatenates onto it, and the
  tolerant reader then drops BOTH lines: the fragment, which was never
  readable, and the record written after it, which was. Each was
  reproduced with a real tear, a real append and the production reader.
  `progress.jsonl` lost the entry after the tear, and the reducer left a
  component `running` when the lost row was its `component_completed`;
  `events.jsonl` and `engineer.jsonl` lost the next event, and the fold
  reported no components at all; the queue journal lost the transition
  after it; the inbox lost the item after it; the dependency-scope
  telemetry lost the row after it. A tail that lost only its terminator
  is worse than a fragment, because the append destroys a whole record
  as well as the new one: measured, two records for one interrupted
  write. Every appender now writes through `kstrl/appendio.py`, which
  probes the tail through the same file description it appends with and
  repairs it in one write.

- `experiments.tsv` no longer renders a corrupted run. It is the same
  interrupted write as above, and it cost more than the JSONL files
  because its reader displays the damage instead of dropping it: the
  run after the tear was lost AND the run before it was rendered by
  `ks evolve --status` and the TUI trends tab with its columns shifted,
  a timestamp under `completed` and extra fields on the end. The file
  now pads its own tail, with no marker row, because TSV has none a
  reader would not render as a run, and both of its readers drop any row
  whose width is not one this writer emits. Two widths are legal, not
  one: a file written before R3.1 has a shorter header, and a filter
  that took the current header's width as the only legal one would
  answer a rendering defect by deleting every row of a legacy file. The
  second reader is `ks autonomy replay`, which fed the shifted columns
  to the ladder through `_as_int`, turning each one into a 0 without
  raising, so a run that never happened counted towards a promotion.

- `experiments.tsv` is read on the dialect its writer writes. The reader
  used `csv`'s default quoting against a writer that joins its fields on
  a tab and never quotes or escapes, so a `"` at the start of any field
  opened a quoted region that swallowed every byte to the next one: a
  project named `"proj` hid every run recorded after it from
  `ks evolve --status`, the TUI trends tab and `ks autonomy replay`, and
  past 128 KiB of swallowed text it raised `_csv.Error` out of all three,
  which is neither an `OSError` nor a `ValueError` and so escaped every
  handler. Reading with `QUOTE_NONE` returns those runs. A field longer
  than `csv`'s own limit is now a refusal the callers already handle:
  `ks autonomy replay` names it and exits 2, and the trends tab logs it
  and shows no runs rather than crashing the TUI.

- An out-of-space error on the FIRST event of a run no longer leaves the
  event log writing onto a torn tail. The sink probes and repairs the
  tail once per run and then holds the handle open, and it bound that
  handle before flushing: an event line is smaller than the 8 KiB buffer,
  so a full disk surfaces at the flush and the sink was already bound and
  in the no-probe branch when it did. The bind now happens only after the
  first write and its flush both land.

- `ks autonomy replay` names an unreadable `experiments.tsv` instead of
  reporting that the project has too little history. The reader returned
  no runs on a permission or a decode error, and no runs is the same
  state as a project that has never been run, so the operator got
  "VERDICT: INSUFFICIENT DATA" for a file problem. The exit code is
  still 2, because nothing was replayed either way; the line above it
  now names the file and the error.

- A repaired `experiments.tsv` write is logged. It is the one record
  file that cannot carry a repair row, because every marker a TSV can
  hold is a field and a row of fields is a run to its reader, so a crash
  that tore this file and lost a run left nothing anywhere: the pad
  leaves a short fragment, the reader drops it on width, and there is no
  counter on that path.

- The evolution journal's probe and append happen under one exclusive
  lock (POSIX). They shared a file description, which removes the
  path-level races but is not a lock, so a concurrent writer could
  crash mid-line between this process's probe and its write, and two
  processes repairing one tear each wrote a repair row. Measured, two
  processes and 74 planted tears, eight runs of each arm: unlocked, 244
  to 269 of 300 records readable and 76 to 86 rows; locked, 300 of 300
  and exactly 74. The lock is `fcntl.LOCK_EX` on the journal's own
  descriptor rather than a sibling lock file, because the journal is one
  file with one writer function. Without `fcntl` there is no exclusion,
  the same degradation the control, queue and factory locks already take
  there, and `get_repair_count` and `docs/evolution-metrics.md` both say
  which case they are describing. A `flock` that RAISES takes the same
  path as a missing `fcntl`: some FUSE, 9p and DrvFs mounts answer
  `ENOLCK` or `EINVAL`, and an unguarded acquisition made every journal
  append raise on such a mount where it used to write the entry, so the
  lock added to protect the record was the thing losing it.

- `.kstrl/autonomy.json` is no longer overwritten when the file it was
  read from could not be parsed. `AutonomyState.load` fails closed to a
  fresh L1 and records why; saving that fresh state back replaced the
  damaged bytes, which are the only thing an operator could have
  repaired, and the next load then found a clean file, so nothing
  reported the damage again. The refusal now lives in
  `AutonomyState.save`, the one function that writes that file, rather
  than in the branches that remembered to ask, and it is reported on the
  run's own surface every time a save is attempted until the file is
  fixed.

- An inbox write that could not take the control lock no longer takes
  its caller down. `Inbox._append` locks on every write and raises a
  `RuntimeError` subclass, which the `(OSError, ValueError)` pair each
  inbox site was written with does not catch; seven sites had that hole,
  including `ks serve`'s decision filing, the pipeline's item resolve and
  the TUI's approve/reject/snooze, where it reached the Textual event
  loop. A malformed `[inbox]` value is now caught too: the config casts
  per key, so a TOML date raised `TypeError`, which is not a
  `ValueError` either, and at the demotion notice that arrived after the
  demotion had already been saved.

- `ks decompose --project-name`, `ks factory --project-name` and
  `ks queue add --project-name` refuse an explicit empty or
  whitespace-only name at the command line, exiting 2 and naming the
  option, instead of running the architect against it. `queue add`
  keeps its `""` default, which is how a queued item asks `serve` to
  name it `queue-<id>`: the refusal is gated on the parameter source,
  so only a blank the operator typed is refused. A queue item already
  on disk with a whitespace-only name, added before this change, is
  poisoned on its next attempt instead of run: the child `ks factory`
  refuses the name and `serve` files the refusal as needing a human.
  The name is an identity: it keys the journal audits, the decision
  register and, under `--single-pr`, the branch. Related, and the reason
  the boundary check
  is worth having: the convergence report's accounting of audits the
  trend leaves out is now computed by one classifier per audit, so the
  three buckets (this project's, another project's, no project recorded)
  always sum to the audits on disk. At an empty project name the two
  counts were separate predicates asking the same question, so an audit
  recording no project was counted both as this project's and as
  unattributed: five audits on disk reported as eight (#338).

- `kstrl.toml` and the `KSTRL_*` environment are now resolved once, at
  command entry, before a command constructs anything. They used to be
  parsed lazily, by whichever config dataclass first needed its section,
  so a typo failed at the first loader that reached it: on the decompose
  path that is `LinearConfig.load`, which runs after the architect has
  been invoked and paid for (measured at 119 to 210 seconds against a
  frontier model on a real spec), and `KSTRL_MUTATION_THRESHOLD=many` or
  `KSTRL_SECURITY_TIMEOUT=many` left a raw `ValueError` traceback out of
  `ks factory`. The blast radius of a typo therefore depended on which
  section it was in and which command was run. Every section is now
  checked up front and the error names the section, the offending key or
  environment variable, and its value. `[evolution]` is the one section
  that warns and continues, because the journal is an optional audit
  trail; every other section configures a gate, a budget, a boundary or
  a destination, where substituting a default would measure the run with
  something other than what the operator configured. Bare `ks` on a
  terminal is checked too, before the home shell opens, because the TUI
  launches runs in-process.

  One command is exempt from the check itself: `ks init`, which writes
  the file, and would otherwise refuse to replace the very file it
  cannot parse. Three more skip only the entry seam and run the same
  check in their own bodies, under their own contracts: `ks config show`
  prints every row it can resolve and then names each rejected section
  with its key and value, `ks sense` reports through exit 2 and a JSON
  error document, and `ks serve` through exit 2 before it can poison a
  queue item. `ks config show` is the surface guaranteed to run and
  explain whatever else refuses (#272).

- Safe mode on the dashboard: six defects an independent review
  reproduced after the change merged. All three `dock: top` siblings
  reserved row zero and painted over each other, so the checkpoint
  banner hid the safe-mode warning and the warning hid the run header;
  the banners now flow under the docked top bar, which also repairs a
  pre-existing bug where a checkpoint banner covered the run header. The
  panel key moved from `m` to `f2` because a text input consumes
  printable keys before application bindings, so the advertised key
  typed a letter into the launch, config, decompose and init fields
  instead of opening the panel. The background check no longer relies on
  `exclusive=True`, which cancels the asyncio wrapper and not the
  thread: a superseded check still posted its answer, and a slow nominal
  result landing after a fast degraded one cleared the warning, so
  results now carry the sequence they started with and only one check
  runs at a time. The panel and a freshly mounted screen both replay the
  last completed check rather than starting from nothing, so opening the
  panel early no longer left it reading "not checked yet" forever and
  navigating between screens no longer hid an active warning. The panel
  finally has CSS, without which its border title never rendered and the
  dialog filled the screen (R10.4 follow-up).

  A second review round on those fixes found four more, one of them a
  defect the first round's own fix introduced: the in-flight guard
  dropped a timer tick outright, and because the queue is sampled before
  the expensive event-stream read, a check could sample a nominal queue,
  spend seconds on the stream, and keep that stale answer authoritative
  while the pause that arrived during the read was never sampled. A
  dropped tick is now remembered and rerun. The panel binding is also
  marked priority, without which it never reached Textual's command
  palette, which is a system modal that excludes ordinary application
  bindings. The panel's scroller laid out taller than its dialog, so
  overflowing reasons were clipped while `max_scroll_y` stayed zero and
  no key could reach them. And replay-on-mount was unconditional, so a
  panel constructed with real findings rendered the app's nominal state
  instead.

### Security

- File names taken from `git diff --name-only` no longer reach `/bin/sh`
  unquoted in the dead-code and mutation gates. The diff is agent-authored,
  which is the least trusted input in the factory, and a changed file named
  ``$(id).py`` was command substitution the shell executed; a name with a space
  in it split into two paths the tool could not find and failed the component
  for a reason that named the wrong cause. vulture is now invoked with an
  argument list and no shell, and the mutation gate quotes each path.

### Added

- Golden patterns: an operator-authored file, injected into every factory
  engineer prompt and every `ks run` prompt (`ks feature` and
  `ks understand` do not read it). `ks init` scaffolds
  `scripts/kstrl/golden-patterns.md` and you write what a good change
  looks like in this repository, with a file to copy from for each
  pattern. The distiller records what happened and feedforward computes
  structure; neither says what is wanted, and nothing in kstrl did. The
  path is `[paths] golden_patterns` in `kstrl.toml` or
  `KSTRL_GOLDEN_PATTERNS_FILE`. The block sits between the distilled
  knowledge and the architect's decisions, is read from the repo root
  (never from a component worktree, which the engineer can write to), and
  is read verbatim rather than filtered, the way `CLAUDE.md` already is:
  the operator authored it. Its delimiter lines carry a fresh random
  token per build, so no line of the file can close the block early.
  Nothing is injected while the file is absent, empty, unreadable, or
  still unchanged since `ks init` wrote it: kstrl recognises its own
  scaffold by digest over the decoded text, so a CRLF copy counts as
  unchanged and an unedited skeleton costs no tokens. Past 6000
  characters (about 1500 tokens) the text is cut at a line boundary
  where that still delivers 90 percent of the budget and at the budget
  boundary otherwise, so unwrapped markdown is not thrown away; the
  prompt says how much arrived and the run warns once on your terminal
  with the path and the remedy. A `[paths] golden_patterns` you set that
  names no file is named on the terminal too, rather than silently
  omitting the block. A file kstrl cannot even stat, because its parent
  directory is mode 000 or its name is longer than the filesystem
  allows, warns the same way instead of ending the run.

- **Breaking:** for daemon users, `ks serve` now stops admitting work
  while a kstrl-authored pull request is open. A repository with one
  open kstrl PR will admit nothing until it is merged or closed. The new
  `[serve] max_open_prs` defaults to 1; set it to 0 to restore the old
  unbounded behaviour, or raise it. The refusal is a wait rather than a
  pause: nothing needs resuming, and the next cycle proceeds on its own
  once the PR is gone. A PR counts as kstrl-authored when its body ENDS
  with the footer line kstrl writes, so pull requests opened since the
  footer took its current wording are counted too, and a pull request
  that merely quotes the footer in prose is not. Anything that is not a
  usable count refuses admission, because an unknown number of open PRs
  is not zero; that includes a `gh pr list` page filled to its limit,
  where the count is only a lower bound, so a repository holding 100 or
  more open pull requests admits nothing until the bound is switched
  off. After three consecutive polls with an unusable count the daemon
  files one inbox item, since an expired `gh` token and a `gh` missing
  from launchd's PATH never clear themselves. That count is kept in the
  control state directory as `pr_count_streak.json`, so it accumulates
  in BOTH LaunchAgent modes: `keepalive` runs one polling process, and
  `interval` runs one `ks serve --once` process per firing, where a
  count held in memory could never reach the threshold. Manual
  `ks factory` and `ks run` are unaffected: a human typing the command
  is the authorisation. `ks serve --dry-run` lists the new gate last
  (R10.7, #228).
- The evolve screen reports repaired journal writes. `ks evolve
  --status` has reported them since the repair was added and the TUI did
  not, which was the gap: the argument for writing a durable
  `journal_repair` row at all is that under the TUI the logger warning
  goes to `orchestrator.log` where nobody is looking. A line above the
  three tabs now carries the count, the path and which of the two
  outcomes the line above each row is, in the CLI's own words. Silent at
  zero, and it goes back to silent on reload when the count does.
- The architect's non-blocker spec findings now reach the engineer. They
  were written to `scripts/kstrl/spec-issues.json` on every decompose and
  nothing in `kstrl/` ever opened that file: across five recorded runs
  against one real spec, 91 majors and minors were printed once and
  discarded. Each finding is now routed into the PRD of the component
  whose surface it touches, under a new optional `specIssues` key that
  carries the severity, kind, summary, location and suggestion verbatim
  plus an `appliesTo` of `component` or `spec`. The rule matches the
  distinctive words of a component's id and title against the finding's
  own `location` and `summary` text and needs two of them, which scored
  precision 1.00 and recall 0.53 against the 31 real findings whose
  location names the component the architect meant. A finding the rule
  cannot place is not dropped: it goes into every component's PRD as
  `appliesTo: spec`, so nothing the audit produced is lost. Halting is
  untouched, a blocker still stops the decomposition before any PRD is
  written, and `spec-issues.json` remains the full durable record. The
  field is deliberately the loosest thing in the PRD: validated only as
  an array, not compared by `PRD.tamper_changes`, and stripped out
  before the PRD is pasted into the security reviewer's or the
  knowledge distiller's prompt, neither of which asked for it. So an
  engineer may annotate, resolve or delete the block and nothing will
  report it. That is the trade a note nothing is judged against should
  make, and it is the opposite of `fixtures`, which is strict because
  it is both pinned and executed.
- Safe mode: one name, and one question, for the four degraded states
  kstrl already entered separately. An untrusted control directory stops
  the daemon spending, a damaged `autonomy.json` falls back to L1
  Supervised (or a ceiling clamps the run below the level it earned),
  the queue pauses, and an adversarial phase can be skipped for a
  component. Each was correct on its own and each spoke on a different
  surface: a warning line at run start, `ks queue`, `ks serve` output,
  and a callout inside a pull request body. `safe_mode_reasons(root_dir)`
  reads all four and returns a source, a detail sentence taken verbatim
  from the existing signal, and the runbook anchor that recovers it;
  the plain `ks status` report, `ks serve --dry-run` and the dashboard
  read it. On the dashboard `f2` opens a panel from any screen, a warning
  banner appears under the run masthead when a signal degrades, and the
  home masthead carries a chip. The panel is what keeps three facts
  apart that a banner alone would merge: not checked yet, checked and
  clear, and a list of reasons in each signal's own words with the
  runbook section that recovers it. The run masthead carries no chip
  because it has no room: at 120 columns the run header and the cost
  meter already want 126 cells, so anything added there cost the run its
  own state label. The dashboard re-checks on a slow background thread
  rather than on its event poll, because the predicate reads a run's
  whole event stream. It never raises: a signal that cannot be read is
  itself a reason, because a reader that failed is not evidence that the
  signal is clear. No behaviour changed and no gate was added, since
  every signal it reads already refuses where refusing is right
  (R10.4, #225).

- Set-point agreement: a story counts as done only when the reviewer's
  per-story verdicts confirm the engineer's `passes: true`. The engineer
  agent was the only writer of that flag, so the thing doing the work
  also filed the report on it; the reviewer's verdicts were already
  being produced and thrown away at parse time. `[factory]
  setpoint_agreement` (`advisory` by default, or `block`) decides what
  happens on a disagreement: advisory records a `setpoint_disagreement`
  finding on the pull request and in the journal and lets the component
  proceed, while block also resets `passes` to false in the PRD with a
  note saying why and retries the story. Confirmation needs a pass on
  every acceptance criterion, not just on the ones the reviewer chose to
  judge: the existing coverage gate checks that every story got a
  verdict, never that every criterion did. In blocking mode a reviewer
  that crashed, returned nothing usable, or never ran because the
  adversarial budget was exhausted also fails the component, because a
  sensor that did not report has not confirmed anything. An outage is
  recorded as an infrastructure failure rather than as a disagreement,
  so the retry context and the evolution journal do not report a
  reviewer disagreeing when none reported. The autonomy ladder forces
  blocking from L1 upward and can never turn it off. No prompt body
  changed: the engineer is still told to set the flag, the flag has just
  stopped being the sole authority (R10.3, #224).

- `ks sense`: run the mechanical sensors (test suite, typecheck, linter,
  diff scope, bad patterns, plus any opt-in policy / adequacy / dead-code /
  mutation checks) against any tree by hand, with no PRD, branch, worktree
  or agent spend. `--json` emits one machine-readable document; exit 0 on
  pass, 1 on any failed check, 2 when the measurement could not run.
  `run_mechanical_verification` now accepts `prd_path=None` and skips only
  the PRD-dependent checks, and `read_only=True` to measure a tree without
  changing it. Because `ks sense` runs against your live checkout rather
  than a worktree kstrl owns, the measurement never edits, stages, commits
  or leaves bytecode: the dead-code check reports what it would remove
  instead of removing it, and mutation testing is skipped because mutmut
  works by rewriting source. A base branch git cannot resolve is exit 2,
  never a pass on an empty diff (R10.1, #222).

### Changed

- The retry context now records which of the review and security phases
  produced a reading in each attempt, so an earlier finding from one of
  them is dropped from the next attempt's prompt once that reviewer has
  run again and passed, and the "Resolved or superseded" line names it.
  Before this, a passing phase wrote nothing, so a criterion the
  reviewer had already cleared still rendered under "Not re-measured"
  and the agent was told to re-check something it had fixed. A phase
  that did not run records nothing, so a finding from a reviewer the
  operator turned off, or one an exhausted adversarial budget skipped in
  advisory mode, is still shown; a reviewer that crashed is not a
  reading and retires nothing either, and neither is one whose reported
  diffstat git disagrees with, which #266 describes as a verdict reached
  without the whole change in hand and which in advisory mode is neither
  an infrastructure error nor a refusal. An attempt that records no
  failure entry at all also retires what its readings measured: the
  merge-conflict restart reaches the PR phase only after review and
  security have both run and passed, so those readings are real and were
  previously discarded. The contract gate goes through the same merge,
  so a contract failure no longer re-raises a review finding an earlier
  attempt cleared. Note on the issue's second acceptance criterion,
  which asked for an attempt that skips review on an exhausted budget
  and fails security: that is unreachable, because both phases consult
  the same counter and review runs first, so a budget that skipped
  review has already skipped security. The criterion's intent is pinned
  three ways instead - the operator's explicit skip, the budget skip
  with the HITL checkpoint as the failing gate, and the no-reading case
  at the unit layer - and the two pipeline-layer tests assert the skip
  itself rather than only its consequence, because a reviewer that runs
  and crashes retires nothing either and would otherwise pass them
  (#247).

- **Breaking:** a hard-mode review or security phase that finds
  `max_adversarial_calls` already spent now halts the component instead of
  downgrading itself to a skip. Before this, an operator running with a cap and
  `review_mode = "hard"` got every component past the cap merged on mechanical
  verification alone, with the reviewer silently shed: the reviewer accounts for
  14 of the 17 failure signatures in this repository's own journal, so the phase
  dropped under budget pressure was the one doing most of the catching. The halt
  is recorded as `failed_check = adversarial_budget`, journalled as
  `adversarial_budget:review` or `adversarial_budget:security`, and carries an
  `infrastructure_error` finding for the phase. The signature leads with the
  check name rather than the phase so that the journal and `ks autonomy replay`
  answer the same way about the run: the replay reads everything before the
  first colon as the check name, and `adversarial_budget` is enrolled as
  infrastructure, so a run whose reviewer never ran is not counted as a verdict
  about the factory's judgement. The set-point gate's own budget refusal moved
  to `adversarial_budget:setpoint` in the same sweep. It does not retry, and `ks
  serve` now classifies such a run as the existing terminal `budget_halt`
  verdict rather than as retryable infrastructure, so the queue item is not
  requeued against a cap that starts again at zero. Three consequences of a
  halt: every component depending on the halted one is skipped by the usual
  cascade, each halt files an inbox item, and because the verdict is terminal
  `ks serve` poisons the queue item, so three under-budgeted runs in a row trip
  `[serve] max_consecutive_poison` (default 3) and stop the daemon admitting
  work. Advisory mode is unchanged and still
  records a `phase_skipped`, and the knowledge distiller is still skipped rather
  than halted in every mode because it is not a merge gate. Nothing changes for
  a default configuration: `max_adversarial_calls = 0` means unbounded, so the
  refusal is unreachable unless an operator sets a cap. If you set one, budget
  one call for every phase that runs, the distiller included even though it
  gates nothing: hard review plus hard security plus knowledge costs 3 per
  component. Anything less halts something, but which component and which phase
  depends on the component count, so see `docs/runbook.md` rather than
  generalising from one run (R10.5, #226).
- The retry context handed to the engineer is now level-triggered: it renders
  the failures measured in the latest attempt, lists earlier findings whose
  sensor did not run again under "Not re-measured", and replaces the rest with
  a count. Before this it was an integrator with no discharge - every failure
  ever accumulated was re-rendered on every retry under "Fix ALL issues listed
  above before completing", so an agent on attempt 3 was told to fix attempt
  1's failures whether or not attempt 2 had already fixed them. Each failure
  now records the attempt it was measured in and the phase that measured it.
  A finding is only retired when that is observed (the same phase produced a
  fresh reading) or safely inferred (a phase that always runs once its
  predecessor passes). Review and security are excluded from the inference
  because an exhausted `max_adversarial_calls` budget downgrades them to skip
  mid-run, so a later failure does not prove the reviewer ran. A sensor that
  crashed rather than reported retires nothing either: a crashed reviewer, an
  unfetchable diff and an unsplittable diff are recorded as infrastructure
  entries, the same line `Finding.infrastructure_error` already draws.
  `IterationContext.from_json` still reads contexts serialised in the old
  shape, and those undated findings always render as un-re-measured
  (R10.2, #223).

### Removed

- **Breaking:** the one-release compatibility layer for the pre-rename
  names. The legacy environment-variable prefix, config filename, state
  directory, and console script are no longer read or installed. Move to
  `KSTRL_*`, `kstrl.toml`, `.kstrl/`, and the `ks` (or `kstrl`) command.

## [0.2.0] - 2026-07-21

The first release under the **kstrl** name.

### Added

- **Adversarial factory pipeline**: an architect red-teams the spec and
  decomposes it into a component DAG; each component is built by a coding agent
  in an isolated git worktree and gated through mechanical verification, code
  review, security review, and cross-component contract testing before its PR
  merges. An optional human checkpoint can pause before merge.
- **Textual TUI and events substrate**: every run writes a typed
  `events.jsonl` that every surface projects - a live dashboard, the bare-`ks`
  home shell with a run browser, `ks dash` (attach read-only to any run), and
  `ks status` for scripts and CI.
- **Agent adapters**: `claude-code`, `codex`, `custom`, and an opt-in
  `claude-sdk` adapter (installed via the `kstrl[sdk]` extra) with in-loop
  budget enforcement.
- **Safety systems**: per-phase and per-component timeouts, a no-progress
  circuit breaker, adversarial-call and token budgets, an OS-level agent
  sandbox, and a sandboxed approved-fixtures oracle.
- **Learning loop**: a calibration suite with planted-bug fixtures, an
  evolution journal, knowledge distillation across runs, and `ks evolve`
  harness-improvement proposals.
- **Linear mirror**: an optional one-way outbound sink that reflects factory
  progress into a Linear tracker.
- **Dark Factory roadmap**: `docs/dark-factory-roadmap.md` plus the R8 issue
  set defining the path to a governed autonomous factory.

### Changed

- Renamed the project to **kstrl** (CLI `ks`/`kstrl`, config `kstrl.toml`,
  state `.kstrl/`, env prefix `KSTRL_*`). The previous names were honored
  for one release with a deprecation warning.

[Unreleased]: https://github.com/0xfauzi/kstrl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/0xfauzi/kstrl/releases/tag/v0.2.0
