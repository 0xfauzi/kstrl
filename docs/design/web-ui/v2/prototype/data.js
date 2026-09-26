// Fixture content for the prototype. Every value comes from round 1's
// content.py and mock-ups: the snippetvault build (runs live01, fail01,
// 8d80e8 and the rest of the history) and the e3root integration-loop run
// (fda682). Nothing here is invented beyond what those fixtures held.
window.DATA = {
  project: "snippetvault",
  branch: "main",
  config: "kstrl.toml valid",
  safeMode: "off",
  serve: { state: "running", pid: 46812, running: "snippetvault slice 3: export and import", queued: ["snippetvault slice 4: sharing"], checked: "2s" },

  live: {
    id: "live01",
    kind: "factory",
    state: "running",
    started: "16:08",
    elapsed: "1:03:42",
    lastEvent: "2s",
    done: 3,
    total: 6,
    spend: { amount: 19.24, cap: 78.0, pct: 25, tokens: "14.37M", whole: true },
    now: { component: "http-app", phase: "engineer", iteration: 3, iterations: 10, output: "8s", worker: "alive", pid: 46889, checked: "2s", staleAfter: "1m" },
    phases: ["engineer", "verify", "diff", "review", "security", "distill", "pr"],
    components: [
      { id: "snippet-rules", tier: 0, deps: [], state: "completed", phases: [1, 1, 1, 1, 1, 1, 1], tries: 2, iter: 4, time: "19m", tokens: "4.47M", cost: 5.71, what: "Snippet domain rules" },
      { id: "token-crypto", tier: 0, deps: [], state: "completed", phases: [1, 1, 1, 1, 1, 1, 1], tries: 3, iter: 1, time: "8m", tokens: "1.90M", cost: 3.48, what: "Token generation, hashing and header parsing" },
      { id: "storage", tier: 1, deps: ["snippet-rules", "token-crypto"], state: "completed", phases: [1, 1, 1, 1, 1, 1, 1], tries: 2, iter: 1, time: "33m", tokens: "8.00M", cost: 10.05, what: "SQLite storage for tokens and snippets" },
      { id: "http-app", tier: 2, deps: ["snippet-rules", "token-crypto", "storage"], state: "running", phases: [2, 0, 0, 0, 0, 0, 0], tries: 1, iter: 3, time: "11m", tokens: null, cost: null, what: "HTTP routing and endpoint logic" },
      { id: "http-server", tier: 3, deps: ["http-app"], state: "pending", phases: [0, 0, 0, 0, 0, 0, 0], tries: null, iter: null, time: null, tokens: null, cost: null, what: "Socket server, body limits and lifecycle" },
      { id: "cli", tier: 4, deps: ["storage", "token-crypto", "http-server"], state: "pending", phases: [0, 0, 0, 0, 0, 0, 0], tries: null, iter: null, time: null, tokens: null, cost: null, what: "Command-line entry point" }
    ],
    // phase spans per component, minutes from run start, for the phase timeline
    spans: {
      "snippet-rules": [["engineer", 0, 12], ["verify", 12, 12.5], ["diff", 12.5, 12.7], ["review", 12.7, 15.5], ["security", 15.5, 16.8], ["distill", 16.8, 17.5], ["pr", 17.5, 19]],
      "token-crypto": [["engineer", 0, 5], ["verify", 5, 5.3], ["diff", 5.3, 5.4], ["review", 5.4, 6.8], ["security", 6.8, 7.4], ["distill", 7.4, 7.8], ["pr", 7.8, 8]],
      "storage": [["engineer", 19, 43], ["verify", 43, 43.6], ["diff", 43.6, 43.8], ["review", 43.8, 47.5], ["security", 47.5, 49], ["distill", 49, 50.2], ["pr", 50.2, 52]],
      "http-app": [["engineer", 52, 63.7]]
    },
    feed: [
      ["17:10:32", "storage", "finding", "review finding [advisory] test_quality at tests/test_storage.py:1094-1107"],
      ["17:10:32", "storage", "ok", "review passed in 140s"],
      ["17:11:26", "storage", "finding", "security finding [low] information_disclosure at src/snippetvault/storage.py:141-167"],
      ["17:11:26", "storage", "ok", "security passed in 53s"],
      ["17:11:55", "storage", "ok", "distill passed in 30s"],
      ["17:12:06", "storage", "", "PR #3 opened, then merged"],
      ["17:12:06", "storage", "ok", "completed after 1 iteration"],
      ["17:12:06", "http-app", "start", "started"],
      ["17:16:48", "http-app", "", "iteration 1 (281s)"],
      ["17:21:51", "http-app", "", "iteration 2 (301s)"],
      ["17:26:40", "http-app", "", "iteration 3 running"]
    ]
  },

  needs: [
    { id: "gate-client-commands", kind: "merge gate", state: "parked", title: "client-commands", sub: "merge gate · run 8d80e8", age: "2s", action: "Decide", detail: "The branch holds reviewed work. Nothing is pushed until you decide." },
    { id: "checkpoint-comp-c", kind: "checkpoint", state: "parked", title: "comp-c", sub: "approve PR creation and merge", age: "2s", action: "Decide", detail: "The run is waiting on this answer." },
    { id: "halt-fda682", kind: "halted run", state: "waiting", title: "integration loop stopped", sub: "run fda682 · e3root", age: "2s", action: "Decide", detail: "integration-fix-1 failed review and the fix budget (1) is spent." },
    { id: "fail-client-commands", kind: "failed", state: "failed", title: "client-commands", sub: "verify · tests failed · run fail01", age: "17:14", action: "Review retry", detail: "2 attempts. A retry is available and its scope is known." }
  ],

  delivery: {
    mainAt: "cea97b4",
    mainState: "unknown",
    mainReason: "gh api failed (4): HTTP 401: Bad credentials",
    read: "2s",
    merges: [
      { pr: 3, component: "storage", commit: "cea97b4", state: "unknown", reason: "gh api failed (4): HTTP 401: Bad credentials", read: "2s", run: "live01" },
      { pr: 2, component: "snippet-rules", commit: "23dd9ac", state: "failed", reason: 'check "test" failed', read: "2s", run: "live01" },
      { pr: 1, component: "token-crypto", commit: "4d74d12", state: "passed", reason: "7 checks passed", read: "2s", run: "live01" },
      { pr: 9, component: "client-commands", commit: "4c4706b", state: "unread", reason: "not read yet · ks serve refreshes CI on its own", read: null, run: "8d80e8" },
      { pr: 8, component: "client-http", commit: "4ab99ae", state: "passed", reason: "7 checks passed", read: "0s", run: "8d80e8" }
    ]
  },

  history: [
    { id: "live01", kind: "factory", state: "running", when: "2s", age: 0, comps: "3 of 6", tokens: "14.37M", cost: 19.24, cap: 78, note: "" },
    { id: "fail01", kind: "factory", state: "failed", when: "2s", age: 0, comps: "0 of 1, 1 failed", tokens: "2.41M", cost: 3.74, cap: 20, note: "current: see needs you" },
    { id: "8d80e8", kind: "factory", state: "completed", when: "2d", age: 2, comps: "2 of 2", tokens: "18.51M", cost: 19.3, cap: 35, note: "" },
    { id: "1490e8", kind: "factory", state: "unknown", when: "2d", age: 2, comps: "0 of 2", tokens: null, cost: null, cap: null, note: "Refusing to run: stale component branches found: branch kstrl/factory/client-http (component client-http) already exists with commits not merged into main; refusing to silently reuse it. Merge it or delete it (git branch -D kstrl/factory/client-http) and re-run." },
    { id: "7ad3ae", kind: "factory", state: "failed", when: "2d", age: 2, comps: "0 of 2, 1 failed", tokens: "8.02M", cost: 10.37, cap: 45, note: "superseded by 8d80e8" },
    { id: "4965fb", kind: "factory", state: "completed", when: "3d", age: 3, comps: "6 of 6", tokens: "35.60M", cost: 42.35, cap: 78, note: "" },
    { id: "e3e393", kind: "factory", state: "unknown", when: "3d", age: 3, comps: "0 of 6", tokens: null, cost: null, cap: null, note: "Refusing to run: stale component branches found" },
    { id: "22f40e", kind: "factory", state: "unknown", when: "3d", age: 3, comps: "0 of 6", tokens: "1.22M", cost: 1.64, cap: 78, note: "Disallowed changes detected" },
    { id: "db9df0", kind: "decompose", state: "completed", when: "3d", age: 3, comps: "6 planned", tokens: "342.4k", cost: 1.47, cap: null, note: "" }
  ],

  // failed components across runs, for views (this week = the last 7 days)
  failedComponents: [
    { id: "client-commands", run: "fail01", when: "today 17:14", age: 0, gate: "verify", cause: "Tests failed (exit code 1)", cost: 3.74, tries: 2 },
    { id: "client-http", run: "7ad3ae", when: "09-23 22:32", age: 3, gate: "merge", cause: "parked awaiting merge approval; superseded by 8d80e8", cost: 10.37, tries: 1 },
    { id: "integration-fix-1", run: "fda682", when: "09-25 23:21", age: 1, gate: "review", cause: "Review failed: 1 blocking finding at src/snippetvault/snippets.py:121-124", cost: 6.2, tries: 2 }
  ],

  checkpoint: {
    id: "checkpoint-comp-c",
    title: "Approve PR creation and merge for comp-c?",
    asked: "2s",
    branch: "kstrl/factory/comp-c",
    spend: "at least $4.50",
    spendNote: "no cost cap · some calls did not report a cost, so this is a lower bound",
    gates: [
      { name: "verify", state: "passed", note: "tests, typecheck, lint, diff scope" },
      { name: "review", state: "passed", note: "0 blocking · 2 advisory" },
      { name: "security", state: "passed", note: "1 low" }
    ],
    files: [
      { path: "src/snippetvault/cli.py", add: 201, del: 0 },
      { path: "src/snippetvault/__init__.py", add: 8, del: 2 },
      { path: "src/snippetvault/__main__.py", add: 5, del: 0 }
    ],
    findings: [
      { phase: "review", severity: "advisory", where: "cli.py:621-634", text: "A connection refused error prints a traceback instead of the exit-2 message the PRD names." },
      { phase: "review", severity: "advisory", where: "test_cli_client.py:1923-1929", text: "The test asserts the exit code only; the stderr text the criterion pins is never checked." },
      { phase: "security", severity: "low", where: "cli.py:419-429", text: "The --host value reaches the URL unescaped; a crafted host can add a path segment." }
    ],
    diff: [
      ["file", "src/snippetvault/__init__.py"],
      ["hunk", "@@ -1,2 +1,8 @@"],
      ["del", "-def main() -> None:"],
      ["del", '-    print("snippetvault")'],
      ["add", '+"""snippetvault: a private snippet server and its client."""'],
      ["add", "+"],
      ["add", "+from __future__ import annotations"],
      ["add", "+"],
      ["add", "+from snippetvault.cli import main"],
      ["add", "+"],
      ["add", '+__all__ = ["main"]'],
      ["file", "src/snippetvault/__main__.py"],
      ["hunk", "@@ -0,0 +1,5 @@"],
      ["add", "+from __future__ import annotations"],
      ["add", "+"],
      ["add", "+from snippetvault.cli import main"],
      ["add", "+"],
      ["add", "+raise SystemExit(main())"],
      ["file", "src/snippetvault/cli.py"],
      ["hunk", "@@ -0,0 +1,201 @@"],
      ["more", "201 added lines · open the whole diff"]
    ],
    // The five choices and their consequences, carried whole from round 1 (styles/c-command/checkpoint.html).
    choices: [
      { id: "approve_run", key: "1", label: "Approve and run", kind: "primary", does: "Pushes kstrl/factory/comp-c, opens its PR and merges it.", then: "comp-c completes once the merge is confirmed; without gh it stays unpushed and the run says so." },
      { id: "approve_only", key: "2", label: "Approve only", kind: "secondary", does: "Records the approval and nothing else.", then: "Nothing merges until the next factory run, which then behaves as above." },
      { id: "reject", key: "3", label: "Reject", kind: "danger", does: "comp-c fails and its dependents are skipped. Nothing is pushed.", then: "The branch is kept for you to read." },
      { id: "send_back", key: "4", label: "Send back to the engineer", kind: "plain", does: "The engineer runs comp-c again with a note that a human reviewer asked for changes; no reason is passed on.", then: "Uses one retry; with none left, comp-c fails as on Reject." },
      { id: "later", key: "5", label: "Decide later", kind: "plain", does: "Leaves the question open.", then: "The run waits at this point; the item stays under Needs you and the elapsed clock keeps running." }
    ]
  },

  mergeGate: {
    id: "gate-client-commands",
    title: "Merge client-commands?",
    branch: "kstrl/factory/client-commands",
    head: "4c4706b",
    run: "8d80e8",
    gates: [
      { name: "verify", state: "passed", note: "tests, typecheck, lint, diff scope" },
      { name: "review", state: "passed", note: "0 blocking · 3 advisory" },
      { name: "security", state: "passed", note: "0 findings" }
    ],
    pr: "PR #9 exists · not read yet",
    choices: [
      { id: "approve_run", key: "1", label: "Approve and run", kind: "primary", does: "Records the approval and starts a factory run that pushes, opens the PR and merges it.", then: "Starts a spend against the run's cap." },
      { id: "approve_only", key: "2", label: "Approve only", kind: "secondary", does: "Records the approval and nothing else.", then: "Nothing merges until the next factory run." },
      { id: "reject", key: "3", label: "Reject", kind: "danger", does: "Closes the item as rejected. Nothing is pushed.", then: "The branch is kept for you to read." },
      { id: "later", key: "4", label: "Snooze 24 hours", kind: "plain", does: "Hides this item for 24 hours; it returns after that.", then: "ks serve admits no new work while a merge is parked." }
    ]
  },

  halt: {
    id: "halt-fda682",
    title: "The integration loop stopped without a clean verdict",
    run: "fda682",
    project: "e3root",
    rounds: [
      { n: 1, state: "failed", text: "5 findings opened, 1 handed off", at: "21:54" },
      { n: 2, state: "unknown", text: "not run: integration-fix-1 ended failed, not merged", at: "22:22" },
      { n: 3, state: "failed", text: "0 opened · 1 carried closed · 3 carried still open", at: "23:21" }
    ],
    findings: { open: 3, handedOff: 1, fixed: 1 },
    choices: [
      { id: "close", key: "1", label: "Close as noted", kind: "primary", does: "Closes this item. No factory step reads a halted-run decision, so nothing else changes.", then: "To fix the findings, start a new run; it carries the open findings forward." },
      { id: "later", key: "2", label: "Decide later", kind: "plain", does: "Leaves the item open.", then: "It stays under Needs you." }
    ]
  },

  failure: {
    id: "fail-client-commands",
    component: "client-commands",
    run: "fail01",
    gate: "verify",
    cause: "Tests failed (exit code 1)",
    attempts: [
      [["engineer", "passed", "312s"], ["verify", "failed", "1s"]],
      [["engineer", "passed", "312s"], ["verify", "failed", "1s"]]
    ],
    evidence: ".kstrl/debug/factory-20260926-171433.450564-fail01/client-commands/attempt-2/test_suite.log",
    output: [
      "tests/test_tokens.py F.F                                    [100%]",
      "E       AssertionError: assert 32 == 43",
      "E        +  where 32 = len('1yTqgojHVxU9NIFBpmv14tT232INPESs')",
      "FAILED tests/test_tokens.py::test_generate_token_length",
      "FAILED tests/test_tokens.py::test_rejects_empty_token",
      "========================= 2 failed, 1 passed in 0.02s ========================="
    ],
    scope: {
      resets: ["client-commands to pending", "branch kstrl/factory/client-commands is deleted and recreated from main"],
      keeps: ["the run records under .kstrl/runs", "the debug directory", "the learning journal", "nothing else is failed or skipped"],
      limits: "max cost $45 · 2 in parallel · agent timeout 900s · component timeout 2400s",
      notRepeated: "the recorded verify command option is no longer used; verification runs the test, typecheck and lint commands",
      command: "ks retry client-commands --max-parallel 2"
    }
  },

  findings: [
    { run: "live01", component: "storage", phase: "review", severity: "advisory", kind: "test_quality", where: "tests/test_storage.py:1094-1107", disposition: "open" },
    { run: "live01", component: "storage", phase: "review", severity: "advisory", kind: "other", where: "src/snippetvault/storage.py:532-542", disposition: "open" },
    { run: "live01", component: "storage", phase: "review", severity: "advisory", kind: "dead_code", where: "src/snippetvault/storage.py:433-438", disposition: "open" },
    { run: "live01", component: "storage", phase: "security", severity: "low", kind: "information_disclosure", where: "src/snippetvault/storage.py:141-167", disposition: "open" },
    { run: "fda682", component: "integration", phase: "review", severity: "blocking", kind: "IF-1", where: "snippets.py:121-124", disposition: "open" },
    { run: "fda682", component: "integration", phase: "review", severity: "blocking", kind: "IF-2", where: "IC2", disposition: "fixed by integration-fix-1" },
    { run: "fda682", component: "integration", phase: "review", severity: "major", kind: "IF-3", where: "IC3", disposition: "open" },
    { run: "fda682", component: "integration", phase: "review", severity: "major", kind: "IF-4", where: "IC4", disposition: "handed off" },
    { run: "fda682", component: "integration", phase: "review", severity: "minor", kind: "IF-5", where: "IC5", disposition: "open" }
  ],

  config: [
    { key: "factory.max_cost_usd", label: "cost cap per run", value: "$78.00", source: "kstrl.toml" },
    { key: "factory.max_parallel", label: "components in parallel", value: "2", source: "kstrl.toml" },
    { key: "factory.max_retries", label: "retries per component", value: "3", source: "default" },
    { key: "factory.agent_timeout", label: "agent timeout", value: "900s", source: "kstrl.toml" },
    { key: "review.hard_mode", label: "review halts on blockers", value: "on", source: "kstrl.toml" }
  ]
};
