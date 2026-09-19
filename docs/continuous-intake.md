# Continuous intake (R8.6)

How to make work flow into the factory without firing each run by hand,
and what each safety mechanism actually guarantees.

Three layers, each usable without the ones above it:

| Layer | Command | Needs |
|---|---|---|
| Local queue | `ks queue add/ls/show/retry/rm/pause/resume` | nothing |
| Daemon | `ks serve [--once]` | the queue |
| GitHub inbox | `ks queue sync` | `gh`, an opt-in config |
| Scheduling | launchd | `ks serve` |

**Start at the top.** For a single operator, the local queue plus
`ks serve` already delivers the thing that matters - work runs without you
firing it - with no remote surface, no tokens, and no polling. The GitHub
layer adds the ability to queue from your phone and see status where your
code lives. That is a convenience, not the capability.

---

## 1. The local queue

```bash
ks queue add specs/add-widget.md --priority 3
ks queue ls
ks queue show <id>
```

Items live under `.kstrl/queue/` as one directory each (spec + `meta.json`),
moved between `queued/ leased/ running/ done/ failed/ poison/` by a single
`os.replace`. The spec is **copied** at enqueue, so editing or deleting the
original afterwards cannot change what runs. Locks (`queue.lock`,
`serve.lock`) stay beside the queue. The pause marker and spend ledger do
**not**: under R8.9 they live in the XDG control directory
(`${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/`) so a worktree agent
cannot edit them. First use migrates any legacy in-tree copies and writes
`.kstrl/control_relocated` pointing at the new location. Clones that share
the same `origin` remote share one control dir: do not run two `ks serve`
daemons against that ledger at once.

When a run finishes, the PR URLs its manifest recorded are written onto the
item, and `ks queue show <id>` lists them; a manifest the run does not own, or
one that cannot be read, records nothing.

`ks queue pause` stops new work being claimed; it does not touch a run
already in flight. `ks queue resume` re-opens intake.

### Attempts are money

`[queue] max_attempts` bounds how many times one item may execute. The
counter is charged **before** the run starts, so an interrupted attempt
counts. That direction is deliberate: over-counting costs the item one
retry, while under-counting is an unbounded loop, and a measured engineer
iteration costs **$1.70-2.60** first-attempt and **$3.99-7.42** on a retry.

`ks queue retry <id>` refuses an item that has spent its attempts unless
you pass `--reset-attempts`. That flag is the point at which you are
authorizing more spend.

---

## 2. `ks serve`

```bash
ks serve --dry-run     # what would happen, spending nothing
ks serve --once        # one cycle
ks serve               # poll until interrupted
```

One factory run at a time, holding a daemon singleton lock. `--dry-run`
reports every admission gate in the order the real loop evaluates them.

### Only infrastructure failures retry, and only on positive evidence

The rule is not "retry unless it looks like a spec problem". It is
**nothing retries without affirmative evidence that the failure was
infrastructural**:

| Evidence | Verdict |
|---|---|
| exit 0 | success |
| launch failed before any spend | retry (free) |
| killed by signal, or our timeout with the process group confirmed dead | retry |
| exit 2 with a lock-contention marker in the output | retry |
| exit 2 with a spec-blocker marker | **poison** |
| the run halted on a configured ceiling (`max_total_tokens` / `max_cost_usd`) | **poison** |
| every failed component carries `infrastructure_error` | retry |
| any failed component failed on its merits (with findings) | **poison** |
| a component failed with NO finding at all | **poison** (unclassifiable; its own error is printed) |
| nonzero exit, nothing blamed | **poison** (unclassifiable) |
| manifest unreadable, or not produced by this invocation | **poison** (unclassifiable) |
| timeout whose process group could **not** be confirmed dead | **poison** |

A **budget halt is listed separately from infrastructure on purpose.** The
factory records a blown ceiling as an `infrastructure_error` finding, but
it is deliberate and deterministic: retrying re-runs the same work against
the same limit, and a retry costs MORE because it carries accumulated
context. Raising the ceiling or narrowing the spec is a human decision.

Poisoned items wait for a human. `ks queue ls --state poison` lists them;
`ks inbox ls` carries the decision.

### Five backstops, because a correct classifier is not enough

A *persistent* infrastructure fault is retryable by the rules above and
still burns money. So:

1. **`[queue] max_attempts`** - enforced inside the queue itself, not just
   by the daemon's policy.
2. **Exponential backoff** - 60s doubling, capped at 30 minutes.
3. **`[serve] daily_budget_usd`** - checked *before* admitting each item,
   pausing until the next **local** midnight so a Friday-night stop is not
   a dead weekend.
4. **`[serve] max_consecutive_poison`** - pauses the whole queue. If the
   base branch is broken, every run fails verification, each failure is
   individually legitimate, and no per-item bound ever notices.
5. **`[serve] max_open_prs`** - flow control on the OUTPUT rather than
   the spend. The first four bound what the daemon starts; this one
   bounds what it leaves behind for a human to read.

### Flow control: `max_open_prs`

Scheduled admission stops while `max_open_prs` kstrl-authored pull
requests are open. The default is 1. Without a bound, a daily loop can
generate several unreviewed pull requests in a week, producing review
fatigue and merge conflicts. The rule: a loop may be handed only as much
autonomy as its output can be cheaply and reliably verified.

A pull request counts as kstrl-authored when its body ENDS with the
footer line kstrl writes, which is where both writers put it. Anchoring
at the end rather than matching anywhere in the body is deliberate, and
both error directions are worth knowing:

- **False negative.** A pull request opened before the footer took its
  current wording is not counted; the literal was a different one until
  the project was renamed. Neither is one whose body a human has edited.
- **False positive.** A pull request whose body was written by hand to
  end with exactly that line. Merely quoting or discussing the footer
  does not count, which is the case that mattered: a substring match
  counted a pull request whose description quoted the constant.

The count comes from `gh pr list` in the repo root and looks at the
newest 100 open pull requests. Anything that is not a usable number
refuses admission rather than reading as zero, and that includes a full
page: with 100 or more open pull requests the count is a lower bound, so
it is conclusive only when it already reaches the bound. The plain
consequence, since the scan window is not configurable: on a repository
holding 100 or more open pull requests the daemon refuses on every poll
and admits nothing at all, and `max_open_prs = 0` is the way out.

The refusal is a wait, not a pause: nothing needs to be resumed, and the
next cycle admits work as soon as the pull request is merged or closed.
That is right for a rate limit or a brief outage. It is not right for a
count that never works - an expired `gh` token, or `gh` missing from
launchd's PATH - so after three consecutive polls with an unusable count
the daemon files one inbox item and keeps waiting. One item per streak,
not one per poll; a successful count resets it.

The streak is counted **in both LaunchAgent modes**. It is kept in the
control state directory as `pr_count_streak.json`, beside the autonomy
level and the spend ledger, so `interval` mode - which runs one
`ks serve --once` process per firing - accumulates it across firings
exactly as `keepalive` mode accumulates it across polls. Five firings and
five polls file the same one item. If that file is unreadable the daemon
says so and treats the count as already at its threshold, so the next
unusable count files at once; it does not overwrite the file, because the
damaged bytes are the only thing there is to inspect.

**Manual `ks factory` and `ks run` bypass the bound entirely**, because a
human typing the command is the authorisation. Only the daemon's own
admission consults it. Set `max_open_prs = 0` to switch it off, or raise
it if 1 chafes.

### What `daily_budget_usd` can and cannot do

It counts only cost an adapter **reports**. The codex adapter reports
tokens and no cost, and `decompose` (the architect) emits no usage events
at all - so **every** queued item has some unmetered spend.

Three cases, and they are reported distinctly:

- **full coverage** - the cap is exact.
- **partial coverage** - a *lower-bound* cap: it fires at or after the
  threshold, never before. Still a real bound. Every total is labelled
  `(a FLOOR: ...)` with what was not counted.
- **zero coverage** - no call has ever reported a cost figure, so the cap
  can never fire. `ks serve` **refuses to run** unless you set
  `[serve] allow_uncovered_cost = true`.

The unreported spend is deliberately **never** estimated into a dollar
figure. A number that looks like a measurement but is a guess is worse
than an admitted gap.

---

## 3. GitHub Issues as an inbox

```toml
[intake_github]
enabled = true
repo = "owner/name"          # must match the checkout you run in
queued_label = "kstrl:queued"
max_items_per_sync = 5
```

```bash
gh label create "kstrl:queued"  --color 0e8a16
gh label create "kstrl:running" --color fbca04
gh label create "kstrl:done"    --color 0075ca
gh label create "kstrl:failed"  --color d93f0b
gh label create "kstrl:poison"  --color b60205

ks queue sync --dry-run
ks queue sync
```

An issue carrying `kstrl:queued` becomes a queue item. The verdict comes
back as a state label and a comment. Polling only - no webhooks, nothing
to keep reachable.

### What authorizes work

**Read this before enabling it on a repo that others can reach.**

A stranger can open an issue but cannot label it. Applying a label,
though, is a weaker check than it looks:

- It needs the **Triage** role, **not** push access. On an organization
  repo, a triager who cannot push a line of code could authorize factory
  spend.
- Any GitHub Action in the repo with `issues: write` can apply the label,
  so a workflow could trigger spend with no human involved.

`allowed_actors` makes the decision kstrl's own instead of inheriting
GitHub's:

```toml
[intake_github]
allowed_actors = ["0xfauzi"]   # or KSTRL_INTAKE_GITHUB_ALLOWED_ACTORS=0xfauzi,someone-else
```

Set, an issue is admitted only when the actor of the **latest**
`kstrl:queued` labelling event is on the list. Only the trigger label is
consulted: the `kstrl:running` / `kstrl:done` labels this adapter writes
back go on under the operator's own token, and counting those would let
the adapter authorize itself. Logins are compared without regard to case.

It fails **closed**. A timeline that cannot be read, a labelling event
carrying no actor login, and a sync that did not check at all are all
refusals, because "we could not tell who labelled it" is not evidence
that a trusted actor did. `ks queue sync` and `ks serve --dry-run` print
every refusal with the actor and the label named, so a label that did
nothing can be explained.

Left empty (the default), nothing changes: anyone who can label can
spend, and the two bullets above are the residual risk.

**What is enforced today:** an issue edited *after* it was labelled is
refused. GitHub lets an issue author rewrite the body after a maintainer
has labelled it, so the label is bound to the body revision it authorized.
Re-apply the label to authorize the new text.

**Remote items always stop at the PR.** No label and no config value can
grant auto-merge to remote-sourced work.

### Cross-repository intake is refused

`ks serve` always runs the factory against its own checkout, so an inbox
pointing at a different repo would open a PR in the wrong place. Sync
refuses unless `[intake_github] repo` matches the checkout's remote.

### One measured quirk

GitHub's issue-list endpoint can lag a label write. A sync issued
immediately after labelling returned `polled: 0`; the same sync a minute
later returned `polled: 1`. Invisible at any realistic poll interval, but
do not expect label-then-sync-in-one-breath to work.

---

## 3.5 Steering from a pull request

**Read this before enabling it.** With this on, `ks serve` becomes a
WRITER of your checkout: it appends to `[paths] memory`
(`scripts/kstrl/memory.md` by default) on your local disk, from a
comment somebody else typed on GitHub. The daemon does NOT commit that
change - an uncommitted edit appearing under `git status` after a cycle
is expected, and keeping it is your `git commit`.

Off by default:

```toml
[intake_github]
steer_enabled = true       # or KSTRL_INTAKE_GITHUB_STEER_ENABLED=1
```

Two commands, read from comments on any OPEN pull request `ks serve`
itself opened (identified by the same footer `ks queue show` and the
open-PR bound already key on):

- `/memory <text>` appends `- <text> (from PR #<n> by @<login>, <date>)`
  under the `## Guidance` heading in the memory file. kstrl finds that
  heading and inserts at the end of ITS section - a section you added
  after `## Guidance` keeps its own contents - and adds the heading if
  the file has none at all. A record over 500 characters, an empty one,
  or one containing a line starting with `#` (which would restructure
  the file) is refused: kstrl posts why, in one comment, and writes
  nothing.
- `/iterate <text>` does the `/memory` step above (skipped when `<text>`
  is empty) and then re-queues the work that produced this PR, found by
  the PR's URL recorded on the queue item (PR 1 of this feature). A PR
  from a manual `ks factory` run, or any PR no queue item recorded, gets
  one comment back: `Cannot iterate: no queue item recorded this PR`,
  and nothing is queued. The re-run does not start while this PR stays
  open - the R10.7 open-PR bound holds it, the same as any other queued
  item.

Who may steer is `allowed_actors`, the SAME list section 3 above
describes for issue labelling - there is no second allowlist. Non-empty,
the comment's author must be on it; empty, GitHub's own
`author_association` must be `OWNER`, `MEMBER` or `COLLABORATOR`. Every
refusal - unauthorized, malformed, or the per-cycle cap - is posted or
logged, never silent.

The comment id is recorded in the processed ledger, under the same XDG
control directory the issue adapter's ledger lives in, AFTER the write
(or the re-queue) succeeds. A write that fails is retried the next
cycle; one that succeeded is never re-applied, even if the acknowledgement
comment itself fails to post.

`max_items_per_sync` caps how many comments one cycle acts on, oldest
first; the rest wait for the next poll.

`dry_run = true` logs what a comment would do and writes or posts
nothing.

**Cost.** Every cycle steering is on adds 2 + P `gh` subprocess calls,
where P is the number of open, kstrl-authored pull requests: one `gh
repo view` and one `gh pr list` (both shared with issue intake's own
resolution, when that is also on), plus one comments fetch per marked
PR, plus one `gh pr comment` per command actually acted on. Off, it adds
zero. Measured against this repository, per-call wall time over three
runs each: `gh repo view` 0.39/0.59/0.51 s, `gh pr list`
0.41/0.74/0.39 s, `gh api ... --paginate` 0.30 to 0.55 s.

**The watermark.** Every cycle used to re-fetch a marked pull request's
ENTIRE comment history, even the comments already recorded - measured on
this repository, one PR with three comments: 130477 bytes with no
filter, 2 bytes with a `since` set past all three. At the 60 second
default poll that is roughly 179 MiB/day for a single long-lived PR, all
of it already in the ledger. Each pull request now carries a persisted
watermark, sent as `since` on the next fetch: the greatest `updated_at`
among comments SEEN this cycle that is still less than the smallest
`updated_at` among comments that did NOT resolve (capped, dry-run, or
errored), or the greatest of everything seen when nothing is
unresolved. A PR with nothing eligible keeps its previous watermark
unchanged.

Two corrections to the first version of this rule, both found by a
review that ran a repro. First, the watermark is ordered by `updated_at`
VALUE, never by a comment's position in the fetch, because the fetch is
ascending by `created_at` while `since` filters on `updated_at`: an
older comment edited after a newer one was created sits earlier in the
fetch and later in `updated_at` order, so a rule keyed on fetch position
could advance the watermark past a still-unresolved comment. Second,
every VALIDATED comment counts as "seen", not only the ones that parsed
as `/memory` or `/iterate` - a pull request carrying nothing but
ordinary review prose used to get no watermark at all and was refetched
in full every cycle, which is precisely the case the 130477-byte
measurement came from. An ordinary comment is trivially resolved, so it
still advances the watermark once seen.

The watermark is derived only from `updated_at` values GitHub actually
returned, never from a clock reading, so it cannot skip a comment this
process has not seen.

**The one behaviour change.** Because an authorisation refusal now also
advances the watermark, widening `allowed_actors` later does not
resurrect a comment that was refused before its pull request's watermark
passed it - that comment is simply never fetched again. Before this, an
unauthorised comment was re-read and re-refused on every cycle
indefinitely, so widening the allowlist would pick it up on the very
next poll. An operator who wants an old, already-refused comment acted
on after widening the allowlist should edit it (which bumps its
`updated_at` past the watermark) or leave a fresh comment instead.

---

## 4. Scheduling with launchd

Generate a LaunchAgent for this checkout. Every command below was run as
written:

```bash
ks serve --print-plist --root "$PWD" > /tmp/kstrl.plist
plutil -lint /tmp/kstrl.plist                      # sanity check
LABEL=$(plutil -extract Label raw -o - /tmp/kstrl.plist)
cp /tmp/kstrl.plist ~/Library/LaunchAgents/$LABEL.plist
launchctl load  ~/Library/LaunchAgents/$LABEL.plist
launchctl list | grep kstrl                        # confirm it is loaded
```

To stop it:

```bash
launchctl unload ~/Library/LaunchAgents/$LABEL.plist
```

Logs land in `.kstrl/logs/`. **Measured on a real launchd run: everything
goes to `serve.err.log` and `serve.out.log` stays empty** - the console UI
writes to stderr, so `serve.err.log` is the normal operating log, not an
error-only one. Tail that:

```bash
tail -f .kstrl/logs/serve.err.log
```

### Two modes

```bash
ks serve --print-plist                                        # keepalive
ks serve --print-plist --plist-mode interval --plist-interval 10
```

- **`keepalive`** (default) - one long-lived `ks serve` pacing itself
  with one call, `sleep(cfg.poll_interval_seconds)` in
  `kstrl/serve.py::serve`, after every cycle; launchd relaunches it when
  it **exits**. The poll is a delay between cycles, not a period: a
  cycle that takes T pushes the next poll to T plus the interval, and a
  bounded `--max-cycles N` run costs N-1 polls
  (`tests/test_serve_poll_pacing.py` pins that against the real CLI).
  The daemon survives a suspend (measured, §7). What is **not** settled
  is whether the suspended seconds are charged against an in-flight
  poll; see §7.
- **`interval`** - `ks serve --once` on a **calendar** schedule.
  `--plist-interval` is in MINUTES and must divide an hour evenly
  (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60) or be whole hours.
  Firings missed during a suspend **coalesce into one** (measured
  2026-08-03, numbers in §7).

On a laptop that sleeps the two modes differ: keepalive resumes its
interval, interval collapses the missed firings into one run. Which you
want depends on whether a missed window should be made up.

### What launchd does NOT do

Two guarantees people assume and `launchd.plist(5)` explicitly denies:

> **`StartInterval`** - "If the system is asleep during the time of the
> next scheduled interval firing, that interval will be missed due to
> shortcomings in `kqueue(3)`. **If the job is running during an interval
> firing, that interval firing will likewise be missed.**"

> **`StartCalendarInterval`** - "Unlike cron which skips job invocations
> when the computer is asleep, launchd will start the job **the next time
> the computer wakes up**."

So:

1. **Only `StartCalendarInterval` catches up after sleep.** Interval mode
   uses it for that reason. `StartInterval` would silently skip every
   firing that elapsed while the lid was shut.
2. **launchd never bounds how long a job runs, and never replaces one
   still running.** It just skips the firing. A wedged cycle is therefore
   not killed - it silently stops every later one.

Because of (2), interval mode **refuses to generate** unless
`[serve] factory_timeout_seconds` is set. That timeout is the only real
bound on a cycle; `ThrottleInterval` limits relaunch after exit, not
runtime.

### Why the label is a path hash

Two checkouts of the same repo would otherwise collide on one launchd
`Label`, and launchd keeps only the last job loaded for a given label -
silently. The hash means a worktree and its parent can each be served.

### `PATH` is set explicitly

A LaunchAgent inherits none of your shell environment. Both `gh` and `git`
must be findable, so the plist sets `PATH` to the interpreter's directory
plus the usual system and Homebrew locations. Getting this wrong produces
a daemon that runs and silently fails every poll - the hardest setup bug
to see. If your tools live elsewhere, edit the `PATH` entry.

### The restart throttle is a spend control

`ThrottleInterval` is 60s, not launchd's 10s default. At 10s a
crash-looping daemon would attempt six restarts a minute.

---

## 5. caffeinate: what it does and does not prevent

`[serve] caffeinate = true` (the default on macOS) runs each factory run
under `caffeinate -i`, so the machine will not fall asleep mid-run, and
sleeps freely between runs.

**Measured** (2026-09-07, macOS 26.6.2 build 25G83, Darwin 25.6.0 arm64)
by enumerating the run's whole process group with `ps -g <pgid>` and
reading `pmset -g assertions` against the pids in it:

- `caffeinate -i <utility>` **forks**; it does not exec the utility in
  place. The utility keeps the pid the daemon's `Popen` returned, and a
  second `caffeinate` process runs as a **child of the utility**, inside
  the same process group and session. So the daemon's direct child is
  the factory itself, and the group holds the factory plus one
  caffeinate helper, plus whatever the factory itself spawns.
- The assertion is held by that second process, not by the factory.
  `pmset -g assertions` prints two lines for it, and both matter:

  ```
     pid 43437(caffeinate): [0x...] 00:00:00 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
  	Details: caffeinate asserting on behalf of '/bin/sleep' (pid 43436)
  ```

  The row header is keyed on the **helper's** pid and its `named:` field
  is the generic `"caffeinate command-line tool"`. The `Details:` line
  underneath names the **utility's** pid, which is the one the daemon
  holds. So a search for the run's own pid finds the `Details:` line, not
  the row header.
- After the utility exits, both the helper and the assertion row are
  **gone** at the first sample. So are they after a SIGKILL of the
  direct child alone, and after a `killpg` on the spawn-time group.
  Nothing lingers, and no power assertion outlives a run.
- Because the helper is inside the run's process group, the timeout
  path's group kill releases the power assertion as well as reaping the
  factory. `tests/test_serve_process_tree.py` pins that membership by
  spawning through the shipping supervisor and counting the group: a
  childless utility gives two members with caffeinate and one without, a
  utility that spawns one child of its own gives three and two. A future
  caffeinate that forks the helper OUT of the group fails those counts
  instead of quietly pinning the machine awake after a timeout.

**Which flag asserts what**, measured the same way: `-i` gives
`PreventUserIdleSystemSleep`, `-s` gives `PreventSystemSleep`, `-d` gives
`PreventUserIdleDisplaySleep`, and `-im` gives `PreventUserIdleSystemSleep`
plus `PreventDiskIdle`. All are named `"caffeinate command-line tool"`.

**The caveat that matters:** the assertion is
`PreventUserIdleSystemSleep`, not `PreventSystemSleep`. It prevents *idle*
sleep. It does **not** prevent an explicit sleep - **closing the lid will
still suspend the machine mid-run.**

What that actually means is narrower than "the run is lost". Sleep
*suspends* processes; it does not kill them. On wake the same `ks serve`
and the same factory child resume and the cycle finishes. The lease TTL
may have elapsed during the suspend, but nothing reaps it, because the
process holding the run is the same one that would do the reaping and it
is busy running.

That last sentence is about **keepalive mode only.** In interval mode
the next firing is a different process, so "the same one that would do
the reaping" does not apply, and what protects the suspended run is
`serve_lock`: the second firing takes the daemon lock *before* it
reaches the lease reaper, fails to get it, and exits 2 without touching
the lease. That ordering is the whole safety property, because
`reap_leases` requeues on `lease_expired(moment)` compared against **wall
clock**, which advances across a suspend - so a run suspended overnight
blows its 3600s lease while its pid is very much alive.
`tests/test_serve_process_tree.py::TestTheReaperRunsOnlyUnderTheDaemonLock`
pins it in one process, and `tests/test_serve_lock_before_reaper.py` pins
it the way interval mode runs it: a separate process holds the
lock file and a real `ks serve --once` subprocess exits 2 with the lease
untouched and no reaper row in the journal (#203 item 3). Before those
existed, a `serve()` mutated to run its cycle before acquiring the lock
left all 18 lock tests green. The second class in the new module covers
what neither of those reaches. A `serve()` that takes the lock, RELEASES
it and then runs the cycle leaves every contended test green, because
acquisition still raises whichever side of the cycle it sits on; so a
child process is asked whether the lock is held WHILE an item runs.
Measured 2026-09-16: under that mutation all 361 tests in the five serve
suites pass and that one test is the only red in them.

**What the assertion does against a DARK WAKE is not settled** (#203 items
1 and 2). A dark wake ends on a SleepService or Maintenance timer, not an
idle timer, and interval mode can fire inside one. §7 has the observation,
the script, the two legs with their power conditions, and what each pair
of outcomes decides.

The recovery machinery exists for the case where the process really is
gone - a crash, an OOM kill, a reboot - not for an ordinary lid close.

---

## 6. Troubleshooting

| Symptom | Check |
|---|---|
| Daemon runs, nothing happens | `ks serve --dry-run` - it prints every gate and which one blocks |
| Queue paused unexpectedly | `ks queue ls` shows the reason; budget pauses clear at local midnight |
| `ks serve` refuses to start | a budget is set with no cost coverage; see §2, or set `allow_uncovered_cost` |
| Items poisoned in a row | the poison breaker paused the queue; something systemic is failing |
| `sync` finds nothing | the label may not have propagated yet (§3); confirm with `gh issue list --label kstrl:queued` |
| launchd job not running | `launchctl list \| grep kstrl`; then `.kstrl/logs/serve.err.log` |
| `serve.out.log` is empty | expected - the UI writes to stderr; read `serve.err.log` |
| Component failed, cause unclear | an unevidenced failure now prints the component's own error; check it before suspecting the spec |
| Every poll fails silently under launchd | `PATH` - `gh` is not findable (§4) |
| Daemon says `N kstrl PR(s) open` | flow control is holding the queue; merge or close the PR, or set `[serve] max_open_prs = 0` |
| Daemon says `cannot count open kstrl PRs` | the open-PR bound has no usable number; check `gh auth status` and that `gh` is on the daemon's PATH. After three consecutive polls, in either LaunchAgent mode, it files an inbox item |

---

## 7. What is verified, and what is not (H4)

**Verified by test:** the queue state machine and its money-safety
invariants, the retry classifier's every branch, all four backstops, the
lease reaper, the daemon lock being held before and across the lease
reaper (#203), process-group termination, the GitHub adapter's parsing,
planning, idempotency, authorization binding and writeback, and that the
generated plist parses with `plistlib`/`plutil` in both modes.

**Verified by hand, once:** the GitHub round-trip against a real
repository - label polled, item enqueued with provenance, label swapped,
re-sync correctly skipped, terminal writeback leaving exactly one state
label plus a comment. And the caffeinate assertion lifetime above.

**Verified by a live run (2026-08-03):** a LaunchAgent generated by
`--print-plist --plist-mode interval --plist-interval 1` loaded with
`launchctl load`, fired twice on its calendar schedule, ran
`ks serve --once` to completion each time, and reported
`LastExitStatus = 0`. And `ks serve` drove a real `ks factory` run end to
end - claiming the item, charging the attempt, launching the factory,
classifying the outcome, transitioning the item, filing the inbox entry
and exiting nonzero.

**Verified against a real suspend (2026-08-03, #204):** both launchd
modes were exercised on a laptop that genuinely slept, against launchd's
own unified log and `pmset -g log` rather than against Apple's
documentation.

- *Interval mode catches up, and coalesces.* A 975s clamshell sleep on
  battery (16:41:25 to 16:57:40) passed three 5-minute boundaries and
  produced exactly one catch-up run, completing 16:57:40.783. Confirmed
  independently on the 17:00 boundary: that catch-up completed 17:03:17,
  33s after a 17:02:44 lid open. Across the session `serve.err.log` held
  three `ks serve on ...` header lines against three launchd
  `service inactive` events, 1:1.
- *The keepalive daemon survives suspend.* One `ks serve` lived through
  three suspends totalling 1272s, held `serve.lock` throughout, and
  exited normally.

**NOT verified:**

- **Whether a suspend is charged against an in-flight poll sleep.** This
  is all that is left of "sleep and wake end to end" after the run above,
  and it is a real gap rather than a formality. The loop's only pacing is
  `sleep(cfg.poll_interval_seconds)` in `kstrl/serve.py::serve`, so the
  answer belongs to the OS and not to kstrl. The one measurement is 29
  seconds short of settling it: `--max-cycles 8` at a 60s poll needs 7
  polls, so 420s of whatever the sleep counts, and the `pmset -g log`
  accounting for that run gives 391s awake against 1272s suspended,
  summing to the 1663s wall exactly. The printed `cycles:` line was not
  kept, so a run cut short before its eighth cycle is not ruled out
  either. **Neither explanation is written here, because neither was
  measured.**

  `scripts/sleep_poll_experiment.sh` settles it and costs no LLM spend:
  the scratch queue is empty, so every cycle is a gate check. It captures
  what the 2026-08-03 run did not: the exit code and the `cycles:` line
  (an exit-0 run that prints one prints exactly N), and the suspended
  seconds summed from `pmset -g log` inside its own window, so a run
  with no suspend is reported as void. Only the one poll in flight when
  the lid closes is affected, so it defaults to 2 cycles at a 300s poll
  and the margin is that poll's remaining seconds. If awake time is at
  or above (N-1) polls, the poll counts awake time only and a suspend
  pauses it; if below, suspend seconds were credited against it. Write
  the number and the branch here and delete this bullet.

- **Whether `caffeinate -i` holds a run up against a dark wake's return to
  sleep** (#203 items 1 and 2). §5's assertion topology is measured;
  this is not, and it needs a real suspend on real hardware, so it has not
  been guessed. Interval mode can fire inside a dark wake: one observed
  firing landed in a 2-second `DarkWake ... SleepService` window with the
  lid still shut, which is fine for an empty cycle at 0.08-0.12s and is not
  fine for a factory run at 10-20 minutes. A dark wake ends on a
  **SleepService or Maintenance timer**, not an idle timer, and whether
  `PreventUserIdleSystemSleep` blocks that transition is unknown.

  A leaning, not a measurement, because the holder was not started for
  this purpose, the machine was doing many other things, and `-s` was
  never tried: on 2026-09-15 this laptop entered `Sleep Service Back to
  Sleep` five times (20:21:43, 20:38:10, 21:06:17, 21:24:10, 21:41:42),
  each about two seconds after a `DarkWake ... rtc/SleepService`, while a
  `caffeinate` process alive since 2026-09-06 07:49:35 held
  `PreventUserIdleSystemSleep` throughout, the same assertion `caffeinate
  -i` takes. Read with `pmset -g log` and `pmset -g assertions`. That
  leans towards branch B below, on an unrelated and uncontrolled holder.

  `scripts/dark_wake_caffeinate_experiment.sh` is the controlled run. It
  costs no LLM spend, the child is `/bin/sleep`. The `-i` leg runs on
  BATTERY with the lid shut; the `-s` leg runs on AC with the lid shut,
  because `man caffeinate` limits `-s` to AC power and the script refuses
  that leg on battery rather than reporting a void result as a branch.
  Run both on the default 1800s window: the observed dark wakes came 17
  to 28 minutes apart, so a shorter window can end before one lands. It
  reads `pmset -g log` only between the child starting and the child
  exiting, the interval the assertion was held, and reports VOID rather
  than a branch when no dark wake landed inside it.

  What each result decides: **branch A** (a dark wake in the window and no
  return to sleep) means a run started in a dark wake completes; document
  it and change nothing. **branch B on battery under `-i`** decides
  between the two remedies #203 names: document that a run can suspend
  and resume across a dark wake (open dependency on #204: whether the run
  timeout counts suspended seconds), or require AC for unattended interval
  mode. If `-s` on AC also gives branch B, interval mode is not safe
  unattended at all and §5 must say so. Write the branch and the `pmset`
  lines here either way, and delete this bullet.
- **Automated coverage of a real factory run.** Still true, and still
  deliberate: a suite that spawned real runs would cost dollars per
  assertion, so no test runs a factory. The end-to-end path above is
  verified by hand only.

  Narrowed since (#205, `tests/test_serve_seam.py`): the *launch* half no
  longer depends on that hand check. `subprocess_factory_runner` is now
  executed for real against a stub interpreter - only `sys.executable` is
  replaced, so the argv, the cwd, the `KSTRL_NO_TUI` env, the caffeinate
  wrapping, the process-group spawn and the `RunOutcome` mapping are all
  shipping code - and the argv it builds is then parsed by the real
  `ks factory` Click command, so a flag renamed on either side fails a
  test instead of the next unattended run. A further set drives
  `serve_cycle` with no injected runner at all, which is the only path
  that executes `_default_runner`.

  Measured by mutation, each of these passed the whole suite before those
  tests existed:

  | Mutation | Suite before |
  |---|---|
  | Rename the merge-gate flag in the runner | 3,125 pass |
  | `_default_runner` forwards a wrong `project_name` | 3,140 pass |
  | Drop `caffeinate_prefix` from the runner's command | 3,140 pass |
  | `_default_runner` ignores `serve.caffeinate` | 3,140 pass |

  The caffeinate ones are worth knowing about, and the reason recorded
  here in #205 was wrong. It said `caffeinate -i` **execs in place**
  (measured, macOS 25.5); #209 re-measured by enumerating the process
  group and it **forks a helper into the run's group** instead (§5).
  What makes those mutations invisible is the half that is true either
  way: the utility keeps the pid, argv and exit status it was given, so
  nothing the seam tests read moves when the prefix is dropped. Pid
  identity cannot tell the two mechanisms apart, which is why the 25.5
  reading did not establish what it claimed and why **no claim is made
  here about whether caffeinate behaved differently then**. The seam
  tests use a fake `caffeinate` on `PATH` that touches a marker before
  exec'ing, which is the only way the wiring is observable at all - and
  they patch `sys.platform` so they run on CI's ubuntu rather than
  skipping there, which is where a macOS-gated test would have been
  useless. The group census that *does* catch a dropped prefix on macOS
  is `tests/test_serve_process_tree.py`.

  What remains uncovered is everything *below* `ks factory`'s argument
  parsing: no component is built, no review runs, and the classification
  of a real run's on-disk artifacts is still exercised only through stub
  runners. A regression there is still caught only by a live run.
- **Rate-limit behaviour under sustained polling.** One call per poll
  interval is ~60/hour against a 5,000/hour budget, so this is expected to
  be a non-issue, but it has not been driven to a limit.
- **Conditional requests.** The R8.6 plan credited ETags for cheap
  polling. `gh issue list` exposes no ETag, so that saving is not
  realised. It is not needed at this cadence.
