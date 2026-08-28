"""What the parts of kstrl are to each other, in sentences a reader can use.

The logical model says which components exist and which flows connect them.
This file says what each connection MEANS: when the reader clicks a component,
the panel lists its neighbours and reads these sentences, one per edge, so
"PathGuard is next to EngineerLoop" becomes "PathGuard reverts anything the
engineer wrote outside its allowed paths before the loop honours the
completion marker".

It also names the LAYERS a reader can switch between on one map, and the
JOURNEYS a reader can step through. Every flow belongs to exactly one layer.
Every journey step names the components it lights and the sentence it shows.

Nothing here is derived from code. It is the hand-authored meaning of the
system, kept beside the hand-authored topology in logical_model.py, and it is
wrong the moment the code disagrees with it: keep it short, keep it checked.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Plain words. The map shows the code's name; the panel leads with the plain
# word and gives the code's name once, so a reader who does not know the
# codebase can still follow.
# ---------------------------------------------------------------------------

PLAIN: dict[str, str] = {
    "ServeDaemon": "the daemon that admits work",
    "GitHubIntake": "the issue reader",
    "WorkQueue": "the queue",
    "SpendLedger": "the spend ledger",
    "Inbox": "the decisions waiting for you",
    "FlowControl": "the open-PR bound",
    "Steering": "your PR comments, read back",
    "Architect": "the planner that attacks the spec",
    "Manifest": "the component graph",
    "PRD": "what done means, per story",
    "Feedforward": "what the agent is told before it starts",
    "KnowledgeInjector": "facts from earlier components",
    "RetryContext": "what failed last time",
    "EngineerLoop": "the inner loop around the agent",
    "AgentAdapter": "the wrapper around the coding agent",
    "Breaker": "the stall detector",
    "PathGuard": "the scope fence",
    "OperatorContext": "your standing instructions",
    "MechanicalVerifier": "the checks that need no model",
    "FailureParsers": "tool output turned into targets",
    "Reviewer": "the second opinion",
    "SecurityReviewer": "the security opinion",
    "ContractTester": "the integration check",
    "FixturesOracle": "the answers the agent cannot rewrite",
    "AdequacyGate": "the check on the tests themselves",
    "PolicyEnvelope": "the written rules for a merge",
    "Findings": "the error signal",
    "Calibration": "the check on the checkers",
    "Sense": "run any check by hand",
    "Pipeline": "the decision per component",
    "Scheduler": "the decision per run",
    "HumanCheckpoint": "the pause before a merge",
    "Configuration": "the settings every loop runs on",
    "Worktrees": "one working copy per component",
    "PullRequests": "the merge",
    "Distiller": "what was learned about the artifact",
    "Dampener": "the regression guard on every PR",
    "ReleaseStage": "the deploy",
    "AutonomyLadder": "how much the factory may do alone",
    "Replay": "what the ladder would have done",
    "StateDir": "the governor's own files, out of reach",
    "SafeMode": "is the factory degraded, and why",
    "HealthTrending": "is the factory getting worse",
    "EvolutionJournal": "the record of every outcome",
    "Proposals": "suggested harness changes",
    "Playbook": "lessons shared across projects",
    "RuntimeSignals": "what production says",
    "EventBus": "the event stream",
    "Reducer": "the current state, rebuilt from events",
    "Dashboard": "the live view",
    "ProgressLog": "the older event log, kept compatible",
    "LinearMirror": "the tracker mirror",
    "CLI": "the ks command",
    "Operator": "you",
    "GitHubIssues": "issues, the way in",
    "GitHubPRs": "pull requests, the way out",
    "CodingAgent": "the coding agent itself",
}

# ---------------------------------------------------------------------------
# Layers. One map, several readings. Each flow kind maps to one layer; a
# reader switches layers and the map shows only that layer's edges with the
# rest of the system dimmed but present. The order here is the order the
# layer switch shows them, and the first is on by default.
# ---------------------------------------------------------------------------

LAYERS: list[dict[str, str]] = [
    {
        "id": "work",
        "label": "Work",
        "question": "How does a spec become a merged pull request?",
        "sub": "intake, plan, build, ship: the forward path",
    },
    {
        "id": "measure",
        "label": "Measurement",
        "question": "Who checks what, and who is never allowed to check their own work?",
        "sub": "every sensor and what it reads",
    },
    {
        "id": "feedback",
        "label": "Feedback",
        "question": "What comes back to the agent, and from where?",
        "sub": "the gap between done and measured, fed forward",
    },
    {
        "id": "operator",
        "label": "You",
        "question": "Where do you stand, and what reaches you?",
        "sub": "every channel between the operator and the factory",
    },
    {
        "id": "trust",
        "label": "Trust",
        "question": "How does the factory earn and lose the right to act alone?",
        "sub": "the ladder, the envelope, the governor's files",
    },
    {
        "id": "learn",
        "label": "Learning",
        "question": "What carries from one run to the next?",
        "sub": "facts, journal, playbook",
    },
    {
        "id": "record",
        "label": "Record",
        "question": "How do you see what happened, live or after the fact?",
        "sub": "events in, views out; never control flow",
    },
]

# Flow kind (from logical_model.FLOWS) -> layer id. A flow whose meaning
# belongs to a different layer than its kind suggests is overridden below by
# (from, to) in LAYER_OVERRIDES.
LAYER_OF_KIND: dict[str, str] = {
    "intake": "work",
    "plan": "work",
    "build": "work",
    "decide": "work",
    "ship": "work",
    "measure": "measure",
    "trust": "trust",
    "learn": "learn",
    "observe": "record",
}

LAYER_OVERRIDES: dict[tuple[str, str], str] = {
    ("RetryContext", "EngineerLoop"): "feedback",
    ("PRD", "EngineerLoop"): "feedback",
    ("EngineerLoop", "PRD"): "feedback",
    ("Breaker", "EngineerLoop"): "feedback",
    ("PathGuard", "EngineerLoop"): "feedback",
    ("Findings", "Pipeline"): "feedback",
    ("Pipeline", "RetryContext"): "feedback",
    ("ContractTester", "Scheduler"): "feedback",
    ("Distiller", "KnowledgeInjector"): "learn",
    ("Operator", "Architect"): "operator",
    ("Operator", "Inbox"): "operator",
    ("Operator", "AutonomyLadder"): "operator",
    ("Dashboard", "Operator"): "operator",
    ("Pipeline", "Inbox"): "operator",
    ("Inbox", "ServeDaemon"): "operator",
    ("GitHubIssues", "GitHubIntake"): "operator",
    ("PullRequests", "GitHubPRs"): "operator",
    ("HumanCheckpoint", "Operator"): "operator",
    ("Operator", "Configuration"): "operator",
}

# ---------------------------------------------------------------------------
# Relationships. Keyed by (from, to), matching logical_model.FLOWS. Each is
# one sentence that reads from the FROM side. The panel shows it under the
# clicked component in both directions: as "gives" when the clicked component
# is FROM, as "gets" when it is TO. Write the sentence so it reads either way.
# ---------------------------------------------------------------------------

RELATIONS: dict[tuple[str, str], str] = {
    (
        "GitHubIssues",
        "GitHubIntake",
    ): "An issue carrying the trigger label is a request to spend money; the reader refuses it if the body was edited after the label was applied.",
    (
        "GitHubIntake",
        "WorkQueue",
    ): "An admitted issue becomes one queue item with the issue text as its spec and a source reference so the verdict can be posted back.",
    (
        "WorkQueue",
        "ServeDaemon",
    ): "The daemon takes exactly one ready item per cycle, under a lease, so two runs can never touch the same repository at once.",
    (
        "SpendLedger",
        "ServeDaemon",
    ): "Before claiming anything the daemon asks the ledger whether today's budget, cost coverage and poison count allow another run.",
    (
        "Inbox",
        "ServeDaemon",
    ): "The daemon admits no new work while more decisions wait for you than the cap allows; unread decisions are a reason to stop, not to continue.",
    (
        "ServeDaemon",
        "Scheduler",
    ): "One admitted item becomes one factory run, launched as a subprocess the daemon can kill on a deadline.",
    (
        "Operator",
        "Architect",
    ): "You hand the planner a spec; it is the only input you must write, and everything below measures against what it says.",
    (
        "Architect",
        "Manifest",
    ): "The planner splits the spec into components with dependencies and allowed paths, and halts instead of guessing when the spec has a blocker.",
    (
        "Architect",
        "PRD",
    ): "For each component the planner writes the stories and acceptance criteria that every later check will measure against: the set point.",
    (
        "Manifest",
        "Scheduler",
    ): "The graph tells the scheduler which components are ready: those whose dependencies have actually merged, not merely finished.",
    (
        "Scheduler",
        "Worktrees",
    ): "Each component gets its own working copy cut from the base branch, so parallel builds cannot see or damage each other.",
    (
        "Feedforward",
        "EngineerLoop",
    ): "Before the agent writes a line, it is told the module map, public interfaces, import graph and conventions, computed from the tree with no model call.",
    (
        "KnowledgeInjector",
        "EngineerLoop",
    ): "Facts distilled from components built earlier are placed in the prompt so the agent inherits what was already learned instead of rediscovering it.",
    (
        "RetryContext",
        "EngineerLoop",
    ): "What failed on the last attempt is handed to the agent as parsed failures with file, line and a fix hint; after R10.2, only what is failing now.",
    (
        "EngineerLoop",
        "AgentAdapter",
    ): "The loop assembles one prompt and sends it to the agent through the adapter, once per iteration, up to the iteration cap.",
    (
        "AgentAdapter",
        "CodingAgent",
    ): "The adapter runs the agent as a subprocess in its own process group, on a deadline, and feeds the prompt on stdin.",
    (
        "CodingAgent",
        "AgentAdapter",
    ): "The agent streams its output back; the adapter watches for the completion marker and scrapes token and cost figures from what the CLI reports.",
    (
        "PRD",
        "EngineerLoop",
    ): "The loop points the agent at the highest-priority story not yet marked done.",
    (
        "EngineerLoop",
        "PRD",
    ): "The agent marks a story done by setting its flag; that flag is a claim, and after R10.3 the reviewer must confirm it before it counts.",
    (
        "Breaker",
        "EngineerLoop",
    ): "If several iterations in a row change nothing (same diff, same test signature), the breaker halts the loop instead of spending the remaining budget.",
    (
        "PathGuard",
        "EngineerLoop",
    ): "Anything the agent changed outside its allowed paths is reverted or failed before the completion marker is honoured, so a claim of done cannot smuggle out-of-scope edits.",
    (
        "EngineerLoop",
        "Pipeline",
    ): "When the loop ends, by completion, cap, breaker or timeout, its result goes to the pipeline, which decides what the measurement says.",
    (
        "Pipeline",
        "MechanicalVerifier",
    ): "The pipeline hands the finished working copy and its PRD to the checks that need no model: tests, typecheck, lint, scope, bad patterns.",
    (
        "Pipeline",
        "Reviewer",
    ): "After the mechanical checks pass, the diff and the acceptance criteria go to an independent reviewer, by default from a different model family than the engineer.",
    (
        "Pipeline",
        "SecurityReviewer",
    ): "The same diff goes to a security reviewer working from a threat taxonomy; off by default, blocking at a severity threshold in hard mode.",
    (
        "PolicyEnvelope",
        "MechanicalVerifier",
    ): "The written merge rules (denied paths, size caps, dependency and licence rules, secret patterns) are checked on the diff and lockfile as one of the mechanical checks.",
    (
        "AdequacyGate",
        "MechanicalVerifier",
    ): "The tests themselves are checked: deleted tests, added skips, lost assertions, and new test files that assert nothing falsifiable.",
    (
        "FixturesOracle",
        "MechanicalVerifier",
    ): "Input and output pairs approved in the PRD are run sandboxed, outside the tree the agent can write, so a gamed test file cannot deselect them.",
    (
        "MechanicalVerifier",
        "Findings",
    ): "Every failed check becomes a typed finding with a file, a line and a fix hint; a check that could not run leaves a finding saying so.",
    (
        "MechanicalVerifier",
        "FailureParsers",
    ): "Each failed check's stdout and stderr go to the parsers, which recover the file, the line and the message and attach a hint.",
    (
        "FailureParsers",
        "Findings",
    ): "A parsed failure becomes a typed finding with a location and a suggestion; unparseable output still becomes a finding, without them.",
    (
        "Reviewer",
        "Findings",
    ): "Each acceptance criterion gets a verdict, and each concern beyond the criteria (scope creep, weak tests, dead code) becomes a finding.",
    (
        "SecurityReviewer",
        "Findings",
    ): "Each vulnerability found becomes a finding mapped to an OWASP category, with severity.",
    (
        "Findings",
        "Pipeline",
    ): "The findings are the error signal: the pipeline reads them, not the agent's summary, to decide whether the component passed.",
    (
        "Pipeline",
        "RetryContext",
    ): "When a gate fails and retries remain, the pipeline writes the findings into the context the next attempt will receive.",
    (
        "Pipeline",
        "Distiller",
    ): "Once every gate has passed and before the merge, the diff goes to the distiller so what was learned is captured while it is still the true delta.",
    (
        "Distiller",
        "KnowledgeInjector",
    ): "Durable facts about the built artifact are written to disk and picked up by later components' prompts; how often they are used is measured.",
    (
        "Pipeline",
        "PullRequests",
    ): "A component that passed every gate is pushed, opened as a pull request and merged; it counts as complete only when the merge is confirmed.",
    (
        "PullRequests",
        "GitHubPRs",
    ): "One pull request per component, with the findings in its body, is the factory's output; a human can read every decision there.",
    (
        "PullRequests",
        "ContractTester",
    ): "After components merge, the integration tests run on the merged tiers of the dependency graph.",
    (
        "ContractTester",
        "Scheduler",
    ): "A failing tier is attributed to the most recently merged component, which the scheduler resets and re-runs against the fresh base.",
    (
        "Pipeline",
        "Inbox",
    ): "A merge you asked to approve, a policy exception, a budget overrun: the pipeline parks the decision for you instead of taking it.",
    (
        "Pipeline",
        "HumanCheckpoint",
    ): "When pause_before_pr_merge is on, the pipeline stops before the merge and hands the checkpoint the diff excerpt, both finding streams and what the attempt cost.",
    (
        "HumanCheckpoint",
        "Operator",
    ): "You approve, reject (which fails the component and skips its dependents) or spend a retry; unattended, the decision waits in the inbox.",
    (
        "HumanCheckpoint",
        "Inbox",
    ): "With no terminal attached, the checkpoint parks the merge as an inbox item instead of proceeding; nothing merges past a parked decision.",
    (
        "Configuration",
        "Scheduler",
    ): "The run reads its parallelism, retry budget, ceilings and timeouts from the resolved configuration at start; issue #192 makes that a single read so mid-run edits cannot diverge from the recorded policy hash.",
    (
        "Configuration",
        "EngineerLoop",
    ): "The inner loop's iteration cap, its pause between iterations and the paths of the prompt, PRD and progress files come from the configuration.",
    (
        "Operator",
        "Configuration",
    ): "You turn the gains between runs: kstrl.toml, KSTRL_* variables and command flags, in that precedence from lowest to highest.",
    (
        "Scheduler",
        "AutonomyLadder",
    ): "After a run, its outcome (decisive or not, merged components, violations) is folded into the evidence the ladder promotes or demotes on.",
    (
        "AutonomyLadder",
        "Pipeline",
    ): "At run start the ladder derives the permission bundle for the current level; the bundle can only withhold, never grant, so a hand-edited flag cannot exceed it.",
    (
        "PolicyEnvelope",
        "AutonomyLadder",
    ): "A policy violation demotes the factory one level automatically; there is no appeal at run time.",
    (
        "Calibration",
        "AutonomyLadder",
    ): "A regression in the reviewers' measured detection rate opens a decision for you and, behind a switch, demotes (R10.11).",
    (
        "AutonomyLadder",
        "StateDir",
    ): "The level and its history live in the control directory outside the repository, so the agent it governs cannot edit its own governor.",
    (
        "SpendLedger",
        "StateDir",
    ): "Daily spend is kept outside the tree too, rewritten whole under a lock, and refused rather than guessed when unreadable.",
    (
        "Inbox",
        "StateDir",
    ): "The decisions waiting for you are stored outside the tree; an inbox the agent could edit would not be yours.",
    (
        "Scheduler",
        "EvolutionJournal",
    ): "Every component's outcome, failure signature, cost and finding summary is appended to the journal after the run.",
    (
        "EvolutionJournal",
        "Proposals",
    ): "Patterns that recur across runs become proposals written for you to read; today nothing reads a proposal back into a run.",
    (
        "EvolutionJournal",
        "Replay",
    ): "The recorded runs are what the replay tool feeds through the ladder's thresholds to say what would have fired, without changing anything.",
    (
        "Pipeline",
        "EventBus",
    ): "Every phase start, verdict, retry and finding is emitted as a typed event; the event is the record, and sinks may not change control flow.",
    (
        "Scheduler",
        "EventBus",
    ): "Run start, component scheduling, budget and completion are emitted as typed events alongside the pipeline's.",
    (
        "EventBus",
        "ProgressLog",
    ): "The older progress log is still written, byte-compatible, for anything that reads it.",
    (
        "EventBus",
        "Reducer",
    ): "The reducer folds the event file into the current state of the run; anything shown anywhere can be rebuilt from that file.",
    (
        "Reducer",
        "Dashboard",
    ): "The terminal dashboard renders the reduced state live, attaches to a run from another terminal, and replays a finished one.",
    (
        "Reducer",
        "CLI",
    ): "ks status prints the same reduced state for scripts and CI, with lower-bound markers wherever a cost figure is unreported.",
    (
        "EventBus",
        "LinearMirror",
    ): "Failures and budget halts are mirrored to the tracker as comments; the mirror is outbound only and can never fail a run.",
    (
        "Dashboard",
        "Operator",
    ): "You watch the board, open a component, read the findings and the transcript, and approve or reject at a checkpoint.",
    (
        "Operator",
        "Inbox",
    ): "You approve, reject, retry or snooze each waiting decision from the command line; nothing proceeds past a parked decision without you.",
    (
        "Operator",
        "AutonomyLadder",
    ): "Only you can promote, and only with the evidence in place and a recorded acknowledgement; demotion never needs you.",
}

# ---------------------------------------------------------------------------
# Journeys. A reader steps through one at a time; each step names the
# components to light and the edge to trace, with one sentence. Steps use
# the same map, so the journey is a reading of the topology, not a second
# drawing. `acts` is who does the step; `measures` is who checks it (empty
# when nothing does, which is itself the lesson).
# ---------------------------------------------------------------------------

JOURNEYS: list[dict[str, object]] = [
    {
        "id": "spec-to-merge",
        "label": "A spec becomes a merged pull request",
        "steps": [
            {
                "acts": ["GitHubIssues", "GitHubIntake"],
                "measures": ["GitHubIntake"],
                "edge": ("GitHubIssues", "GitHubIntake"),
                "say": "You label an issue. The reader checks the label came before the last edit, then admits it as a queue item.",
            },
            {
                "acts": ["ServeDaemon"],
                "measures": ["SpendLedger", "Inbox"],
                "edge": ("WorkQueue", "ServeDaemon"),
                "say": "The daemon runs its admission gates in a fixed order and claims one item only if every gate allows it.",
            },
            {
                "acts": ["Architect"],
                "measures": ["Architect"],
                "edge": ("Architect", "PRD"),
                "say": "The planner attacks the spec for ambiguity and missing failure modes, halts on a blocker, and otherwise writes the components and their acceptance criteria.",
            },
            {
                "acts": ["Scheduler", "Worktrees"],
                "measures": [],
                "edge": ("Scheduler", "Worktrees"),
                "say": "Each ready component gets its own working copy; nothing is measured yet.",
            },
            {
                "acts": ["Feedforward", "KnowledgeInjector"],
                "measures": [],
                "edge": ("Feedforward", "EngineerLoop"),
                "say": "The agent is told what the codebase looks like and what earlier components learned, before it acts.",
            },
            {
                "acts": ["EngineerLoop", "AgentAdapter", "CodingAgent"],
                "measures": ["Breaker", "PathGuard"],
                "edge": ("EngineerLoop", "AgentAdapter"),
                "say": "The agent works one story at a time and says when it is done. Only the stall detector and the scope fence watch this loop; neither reads the code.",
            },
            {
                "acts": ["Pipeline"],
                "measures": [
                    "MechanicalVerifier",
                    "PolicyEnvelope",
                    "AdequacyGate",
                    "FixturesOracle",
                ],
                "edge": ("Pipeline", "MechanicalVerifier"),
                "say": "The checks that need no model run first: tests, types, lint, scope, the merge rules, the tests' own quality, the approved fixtures.",
            },
            {
                "acts": ["Pipeline"],
                "measures": ["Reviewer", "SecurityReviewer"],
                "edge": ("Pipeline", "Reviewer"),
                "say": "A reviewer from another model family judges every acceptance criterion; a security reviewer hunts vulnerabilities.",
            },
            {
                "acts": ["Findings", "Pipeline"],
                "measures": ["Findings"],
                "edge": ("Findings", "Pipeline"),
                "say": "The findings, not the agent's summary, decide: pass, or write them into the next attempt's context and retry.",
            },
            {
                "acts": ["Distiller"],
                "measures": [],
                "edge": ("Pipeline", "Distiller"),
                "say": "What was learned about the artifact is written down while the diff is still the true delta.",
            },
            {
                "acts": ["PullRequests", "GitHubPRs"],
                "measures": ["PullRequests"],
                "edge": ("Pipeline", "PullRequests"),
                "say": "The component is pushed, opened and merged; it is complete only when the merge is confirmed.",
            },
            {
                "acts": ["ContractTester"],
                "measures": ["ContractTester"],
                "edge": ("PullRequests", "ContractTester"),
                "say": "Integration tests run on the merged tiers; a failure is attributed to the last component merged, which re-runs against the fresh base.",
            },
            {
                "acts": ["Scheduler"],
                "measures": ["AutonomyLadder", "EvolutionJournal"],
                "edge": ("Scheduler", "AutonomyLadder"),
                "say": "The run's outcome is folded into the ladder's evidence and appended to the journal.",
            },
        ],
    },
    {
        "id": "failure-to-retry",
        "label": "A failure becomes the next attempt",
        "steps": [
            {
                "acts": ["MechanicalVerifier"],
                "measures": ["MechanicalVerifier"],
                "edge": ("MechanicalVerifier", "Findings"),
                "say": "A check fails. Its output is parsed into a finding with the file, the line, the source context and a hint.",
            },
            {
                "acts": ["Findings", "Pipeline"],
                "measures": [],
                "edge": ("Findings", "Pipeline"),
                "say": "The pipeline reads the finding and, with retries left, decides to try again.",
            },
            {
                "acts": ["Pipeline", "RetryContext"],
                "measures": [],
                "edge": ("Pipeline", "RetryContext"),
                "say": "The finding is written into the retry context. Today that context only grows; after R10.2 it shows what is failing now and omits what was fixed.",
            },
            {
                "acts": ["RetryContext", "EngineerLoop"],
                "measures": [],
                "edge": ("RetryContext", "EngineerLoop"),
                "say": "The next attempt starts with that context in front of the agent, after the codebase context and before your standing instructions.",
            },
            {
                "acts": ["EngineerLoop"],
                "measures": ["Breaker"],
                "edge": ("Breaker", "EngineerLoop"),
                "say": "If the attempt changes nothing, the stall detector halts it rather than spending the rest of the budget.",
            },
        ],
    },
    {
        "id": "operator-steers",
        "label": "You steer the factory",
        "steps": [
            {
                "acts": ["Operator", "Architect"],
                "measures": [],
                "edge": ("Operator", "Architect"),
                "say": "You write the spec and, when the planner halts on a blocker, you fix the spec; there is no override flag.",
            },
            {
                "acts": ["Dashboard", "Operator"],
                "measures": [],
                "edge": ("Dashboard", "Operator"),
                "say": "You watch the board, drill into a component, and read the findings beside the transcript.",
            },
            {
                "acts": ["Pipeline", "HumanCheckpoint"],
                "measures": [],
                "edge": ("HumanCheckpoint", "Operator"),
                "say": "With the checkpoint on, the factory stops before each merge and shows you the diff, the findings and the cost; you approve, reject or spend a retry.",
            },
            {
                "acts": ["Pipeline", "Inbox", "Operator"],
                "measures": [],
                "edge": ("Operator", "Inbox"),
                "say": "Decisions the factory may not take alone wait for you in one place; you approve, reject, retry or snooze.",
            },
            {
                "acts": ["Operator", "AutonomyLadder"],
                "measures": [],
                "edge": ("Operator", "AutonomyLadder"),
                "say": "You promote the factory only with evidence and an acknowledgement; it demotes itself without asking.",
            },
            {
                "acts": ["OperatorContext", "Steering"],
                "measures": [],
                "edge": ("Feedforward", "EngineerLoop"),
                "say": "Planned: your golden patterns and standing corrections are read on every run, and a comment on a pull request appends to them (R10.8 to R10.10).",
            },
        ],
    },
    {
        "id": "trust-earned-lost",
        "label": "The factory earns and loses autonomy",
        "steps": [
            {
                "acts": ["Scheduler", "AutonomyLadder"],
                "measures": [],
                "edge": ("Scheduler", "AutonomyLadder"),
                "say": "Every decisive run adds evidence at the current level: merged components, clean merges, violations.",
            },
            {
                "acts": ["Operator", "AutonomyLadder"],
                "measures": [],
                "edge": ("Operator", "AutonomyLadder"),
                "say": "When the evidence meets the level's entry criteria, you promote with an acknowledgement; the ladder never promotes itself.",
            },
            {
                "acts": ["AutonomyLadder", "Pipeline"],
                "measures": [],
                "edge": ("AutonomyLadder", "Pipeline"),
                "say": "At the next run start the level becomes a permission bundle that can only withhold: a higher level removes a gate, never adds a power the level does not carry.",
            },
            {
                "acts": ["PolicyEnvelope", "AutonomyLadder"],
                "measures": ["PolicyEnvelope"],
                "edge": ("PolicyEnvelope", "AutonomyLadder"),
                "say": "One policy violation demotes one level, immediately, with a cooldown before re-promotion.",
            },
            {
                "acts": ["Calibration", "HealthTrending", "AutonomyLadder"],
                "measures": ["Calibration", "HealthTrending"],
                "edge": ("Calibration", "AutonomyLadder"),
                "say": "Planned: a measured regression in the reviewers, or a health breach in the run metrics, opens a decision for you and can demote (R10.11, R8.4).",
            },
            {
                "acts": ["AutonomyLadder", "StateDir"],
                "measures": ["StateDir"],
                "edge": ("AutonomyLadder", "StateDir"),
                "say": "All of this lives outside the repository. If the control directory is unreadable or inside the tree, the factory falls back to the lowest level.",
            },
        ],
    },
]

# ---------------------------------------------------------------------------
# Verbs. The visual relationship wheel prints one short verb on each spoke,
# read from the clicked component outward, so the reader sees "measures",
# "feeds back", "governs" at a glance and opens the sentence only for
# emphasis. Defaults by layer and direction; overrides where the default
# would mislead.
# ---------------------------------------------------------------------------

VERB_DEFAULT: dict[str, tuple[str, str]] = {
    # layer: (verb when the clicked component is FROM, verb when it is TO)
    "work": ("hands to", "receives from"),
    "measure": ("measures", "is measured by"),
    "feedback": ("feeds back to", "is fed by"),
    "operator": ("asks", "is steered by"),
    "trust": ("governs", "is governed by"),
    "learn": ("teaches", "learns from"),
    "record": ("records to", "is rebuilt from"),
}

VERB_OVERRIDES: dict[tuple[str, str], tuple[str, str]] = {
    ("Pipeline", "MechanicalVerifier"): ("submits to", "measures for"),
    ("Pipeline", "Reviewer"): ("submits to", "measures for"),
    ("Pipeline", "SecurityReviewer"): ("submits to", "measures for"),
    ("Findings", "Pipeline"): ("decides", "reads"),
    ("EngineerLoop", "PRD"): ("claims done in", "is claimed by"),
    ("PRD", "EngineerLoop"): ("points", "targets"),
    ("PathGuard", "EngineerLoop"): ("fences", "is fenced by"),
    ("Breaker", "EngineerLoop"): ("halts", "is halted by"),
    ("AutonomyLadder", "Pipeline"): ("withholds from", "is limited by"),
    ("PolicyEnvelope", "AutonomyLadder"): ("demotes", "is demoted by"),
    ("Calibration", "AutonomyLadder"): ("can demote", "watches"),
    ("Operator", "AutonomyLadder"): ("promotes", "is promoted by"),
    ("Operator", "Inbox"): ("decides in", "waits for"),
    ("Pipeline", "Inbox"): ("parks in", "holds for"),
    ("Dashboard", "Operator"): ("shows", "watches"),
    ("Operator", "Architect"): ("gives the spec to", "plans for"),
    ("ContractTester", "Scheduler"): ("resets via", "re-runs for"),
    ("Distiller", "KnowledgeInjector"): ("writes facts for", "reads facts from"),
    ("AutonomyLadder", "StateDir"): ("is kept in", "keeps"),
    ("SpendLedger", "StateDir"): ("is kept in", "keeps"),
    ("Inbox", "StateDir"): ("is kept in", "keeps"),
    ("EventBus", "Reducer"): ("is folded by", "folds"),
    ("Reducer", "Dashboard"): ("drives", "renders"),
    ("EventBus", "LinearMirror"): ("mirrors to", "mirrors"),
    ("Pipeline", "HumanCheckpoint"): ("pauses for", "holds"),
    ("HumanCheckpoint", "Operator"): ("asks", "decides at"),
    ("HumanCheckpoint", "Inbox"): ("parks in", "holds for"),
    ("MechanicalVerifier", "FailureParsers"): ("hands output to", "parses for"),
    ("FailureParsers", "Findings"): ("shapes into", "is shaped by"),
    ("Configuration", "Scheduler"): ("sets the gains for", "reads gains from"),
    ("Configuration", "EngineerLoop"): ("bounds", "is bounded by"),
    ("Operator", "Configuration"): ("tunes", "is tuned by"),
}


def verb_for(edge: tuple[str, str], layer: str, from_clicked: bool) -> str:
    """The verb to print on a spoke, read from the clicked component."""
    pair = VERB_OVERRIDES.get(edge) or VERB_DEFAULT.get(layer, ("to", "from"))
    return pair[0] if from_clicked else pair[1]


def layer_for(edge: tuple[str, str], kind: str) -> str:
    """The one layer a flow belongs to: the override if there is one, else its kind's."""
    return LAYER_OVERRIDES.get(edge) or LAYER_OF_KIND[kind]
