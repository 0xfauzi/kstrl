"""The LOGICAL view of kstrl: what the system does, not which files exist.

Components, the artifacts that flow between them, the regions they sit in and
the invariants that govern them are transcribed from ARCHITECTURE.md, CLAUDE.md
and docs/control-loop-design.md. Those documents are the authority for what
the system is meant to be; this file is only their machine-readable form, plus
the mapping from each logical component to the modules that implement it.

Build state is DERIVED, never declared: a component is built when the modules
named in `implemented_by` exist and carry the entry point named in `entry`.
A component that carries a `tracker` (a roadmap item) is planned until that
entry appears, so the diagram cannot claim something is finished before the
code lands, and cannot keep calling it planned after it does.

Positions are hand-placed because this is a topology, not a chart: a box's
place carries meaning (which boundary it sits inside, where it is on the path
from an issue to a merged pull request). A fixed layout also means the same
system always draws the same picture, so a change in the diagram is a change
in the system. `layout_problems()` checks the placement mechanically.

Three node kinds are drawn differently on purpose. A `component` does work, a
`store` holds state, an `actor` is outside the system. Drawing a store as if it
were a processing step is the most common lie in architecture diagrams.

Ported from the deckgen repository's atlas tooling; the model here is kstrl's.
"""

from __future__ import annotations

from typing import Any

CANVAS = (2278, 1070)

# The canvas is a grid of card columns 190 apart (150 card, 40 gutter) and
# card rows 92 apart (56 card, 36 gutter). Every card sits on it, so every
# gutter is a straight corridor an orthogonal edge can run down, and a lane
# in one gutter lines up with the same gutter one band lower. The forward
# path reads left to right across the upper band; the bands that govern,
# remember and observe it sit in a lower band; the agent's own process and
# tree sit below the factory, under BUILD, so the adapter's two edges drop
# straight down to them.
COL: dict[str, int] = {
    "c0": 32,  # github, outside the factory
    "c1": 254,
    "c2": 444,
    "c3": 654,
    "c4": 864,
    "c5": 1054,
    "c6": 1264,
    "c7": 1474,
    "c8": 1664,
    "c9": 1854,
    "c10": 2096,  # operator, outside the factory
}
ROW: dict[str, int] = {
    "r1": 110,
    "r2": 202,
    "r3": 294,
    "r4": 386,
    "r5": 478,
    "r6": 570,
    "l1": 742,
    "l2": 834,
}

# Hard boundaries. Nesting is by containment in the coordinates, not a tree.
CONTAINERS: list[dict[str, Any]] = [
    {
        "id": "github",
        "label": "GITHUB",
        "sub": "issues in, pull requests out, polled never pushed",
        "box": (16, 16, 182, 172),
        "tone": "host",
    },
    {
        "id": "factory",
        "label": "FACTORY PROCESS",
        "sub": "one process; every decision is code",
        "box": (216, 16, 1846, 906),
        "tone": "server",
    },
    {
        "id": "operator",
        "label": "OPERATOR",
        "sub": "one person, on the loop",
        "box": (2080, 16, 182, 116),
        "tone": "host",
    },
    {
        "id": "control_dir",
        "label": "CONTROL DIRECTORY",
        "sub": "XDG state outside the tree; the governor the agent cannot edit",
        "box": (2080, 708, 182, 100),
        "tone": "isolated",
    },
    {
        "id": "agent_cli",
        "label": "AGENT CLI",
        "sub": "subprocess in its own process group; killed on deadline",
        "box": (848, 938, 182, 116),
        "tone": "isolated",
    },
    {
        "id": "worktree",
        "label": "WORKTREE",
        "sub": "the only tree the agent may write",
        "box": (1046, 938, 178, 116),
        "tone": "isolated",
    },
]

# Soft bands inside the factory. The upper band reads left to right as the
# path one unit of work takes, DECIDE between BUILD and MEASURE because the
# pipeline is the hub both sides report to. The lower band holds the bands
# that govern, remember and observe that path. Each box is 20 wider than its
# cards on each side and 34 taller above them: room for a lane past a card
# inside its own band.
REGIONS: list[dict[str, Any]] = [
    {"id": "intake", "label": "INTAKE", "box": (234, 76, 380, 380)},
    {"id": "plan", "label": "PLAN", "box": (634, 76, 190, 288)},
    {"id": "build", "label": "BUILD", "box": (844, 76, 380, 380)},
    {"id": "decide", "label": "DECIDE", "box": (1244, 76, 190, 380)},
    {"id": "measure", "label": "MEASURE", "box": (1454, 76, 380, 564)},
    {"id": "ship", "label": "SHIP", "box": (1854, 76, 190, 472)},
    {"id": "observe", "label": "OBSERVE", "box": (234, 708, 570, 196)},
    {"id": "learn", "label": "LEARN", "box": (1034, 708, 380, 196)},
    {"id": "trust", "label": "TRUST", "box": (1454, 708, 570, 196)},
]

# Component id mapped to its place on the grid and how it is drawn:
# column, row, then component, store or actor.
_PLACE: dict[str, tuple[str, str, str]] = {
    # intake: the daemon top right so its run leaves by the top corridor;
    # the inbox under it, on the right, where the operator and the pipeline
    # reach it
    "WorkQueue": ("c1", "r1", "store"),
    "ServeDaemon": ("c2", "r1", "component"),
    "GitHubIntake": ("c1", "r2", "component"),
    "Inbox": ("c2", "r2", "store"),
    "SpendLedger": ("c1", "r3", "store"),
    "FlowControl": ("c2", "r3", "component"),
    "Steering": ("c1", "r4", "component"),
    # plan: the manifest top so it reaches the scheduler over BUILD; the PRD
    # level with the engineer loop
    "Manifest": ("c3", "r1", "store"),
    "Architect": ("c3", "r2", "component"),
    "PRD": ("c3", "r3", "store"),
    # build: the loop on the right, level with the pipeline; the adapter
    # bottom left, straight above the agent's process
    "Feedforward": ("c4", "r1", "component"),
    "OperatorContext": ("c5", "r1", "component"),
    "KnowledgeInjector": ("c4", "r2", "component"),
    "RetryContext": ("c5", "r2", "store"),
    "PathGuard": ("c4", "r3", "component"),
    "EngineerLoop": ("c5", "r3", "component"),
    "AgentAdapter": ("c4", "r4", "component"),
    "Breaker": ("c5", "r4", "component"),
    # decide: the configuration on top, read once at run start by the
    # scheduler under it; the checkpoint under the pipeline, the last stop
    # before a merge
    "Configuration": ("c6", "r1", "store"),
    "Scheduler": ("c6", "r2", "component"),
    "Pipeline": ("c6", "r3", "component"),
    "HumanCheckpoint": ("c6", "r4", "component"),
    # measure: the three sensors the pipeline submits to stacked beside it,
    # findings level with the pipeline on the far side of them with the
    # parsers directly under them; the two sensors that report to the ladder
    # on the bottom row, above it
    "SecurityReviewer": ("c7", "r1", "component"),
    "ContractTester": ("c8", "r1", "component"),
    "Reviewer": ("c7", "r2", "component"),
    "Sense": ("c8", "r2", "component"),
    "MechanicalVerifier": ("c7", "r3", "component"),
    "Findings": ("c8", "r3", "store"),
    "AdequacyGate": ("c7", "r4", "component"),
    "FailureParsers": ("c8", "r4", "component"),
    "FixturesOracle": ("c8", "r5", "component"),
    "Calibration": ("c7", "r6", "component"),
    "PolicyEnvelope": ("c8", "r6", "component"),
    # ship
    "PullRequests": ("c9", "r1", "component"),
    "Distiller": ("c9", "r2", "component"),
    "Worktrees": ("c9", "r3", "component"),
    "Dampener": ("c9", "r4", "component"),
    "ReleaseStage": ("c9", "r5", "component"),
    # observe: the bus top right, nearest the decisions that feed it
    "Dashboard": ("c1", "l1", "component"),
    "Reducer": ("c2", "l1", "component"),
    "EventBus": ("c3", "l1", "component"),
    "ProgressLog": ("c1", "l2", "store"),
    "CLI": ("c2", "l2", "component"),
    "LinearMirror": ("c3", "l2", "component"),
    # learn: the journal top right, under the scheduler, beside the ladder
    "Proposals": ("c5", "l1", "component"),
    "EvolutionJournal": ("c6", "l1", "store"),
    "Playbook": ("c5", "l2", "store"),
    "RuntimeSignals": ("c6", "l2", "component"),
    # trust: the ladder top left, under the sensors that demote it; its
    # files directly under it
    "AutonomyLadder": ("c7", "l1", "component"),
    "Replay": ("c8", "l1", "component"),
    "SafeMode": ("c9", "l1", "component"),
    "StateDir": ("c7", "l2", "store"),
    "HealthTrending": ("c8", "l2", "component"),
}

# Actors sit inside their containers at fixed coordinates.
_ACTORS: dict[str, tuple[int, int]] = {
    "GitHubIssues": (32, 74),
    "GitHubPRs": (32, 130),
    "CodingAgent": (864, 996),
    "Operator": (2096, 74),
}

_TUI_MODULES = [
    "kstrl.tui",
    "kstrl.tui.app",
    "kstrl.tui.bridge",
    "kstrl.tui.dispatch",
    "kstrl.tui.embed",
    "kstrl.tui.home",
    "kstrl.tui.home_data",
    "kstrl.tui.messages",
    "kstrl.tui.runcontext",
    "kstrl.tui.runs",
    "kstrl.tui.screens",
    "kstrl.tui.screens.checkpoint",
    "kstrl.tui.screens.component",
    "kstrl.tui.screens.config",
    "kstrl.tui.screens.decompose",
    "kstrl.tui.screens.evolve",
    "kstrl.tui.screens.home",
    "kstrl.tui.screens.inbox",
    "kstrl.tui.screens.init_wizard",
    "kstrl.tui.screens.launch",
    "kstrl.tui.screens.options",
    "kstrl.tui.screens.overview",
    "kstrl.tui.screens.quit",
    "kstrl.tui.screens.retry",
    "kstrl.tui.session",
    "kstrl.tui.state",
    "kstrl.tui.tail",
    "kstrl.tui.theme",
    "kstrl.tui.widgets",
    "kstrl.tui.widgets.activity",
    "kstrl.tui.widgets.component_table",
    "kstrl.tui.widgets.context_bar",
    "kstrl.tui.widgets.cost_meter",
    "kstrl.tui.widgets.dag_table",
    "kstrl.tui.widgets.evidence",
    "kstrl.tui.widgets.findings_table",
    "kstrl.tui.widgets.form",
    "kstrl.tui.widgets.header",
    "kstrl.tui.widgets.phase_timeline",
    "kstrl.tui.widgets.run_table",
    "kstrl.tui.widgets.transcript",
]

_AGENT_MODULES = [
    "kstrl.agents",
    "kstrl.agents.base",
    "kstrl.agents.claude_code",
    "kstrl.agents.claude_sdk",
    "kstrl.agents.codex",
    "kstrl.agents.custom",
    "kstrl.agents.logging",
    "kstrl.agents.proc",
    "kstrl.agents.sdk_runner",
]

# The command tree and everything that only exists to serve it: the package
# root and its entry point, the init and feature subcommands, and the
# terminal output layer (plain and rich) the commands print through.
_CLI_MODULES = [
    "kstrl",
    "kstrl.__main__",
    "kstrl.cli",
    "kstrl.init_cmd",
    "kstrl.init_wizard",
    "kstrl.feature_cmd",
    "kstrl.output",
    "kstrl.render",
    "kstrl.ui",
    "kstrl.ui.base",
    "kstrl.ui.plain",
    "kstrl.ui.rich_ui",
    "kstrl.ui.animated_art",
    "kstrl.ui.bridge",
]

# Each record: id, region (None for an actor), does, interface, implemented_by,
# entry. `tracker` marks a roadmap item whose code has not landed; `note` is a
# caveat the reader should see; `container` places an actor.
COMPONENTS: list[dict[str, Any]] = [
    # ---- INTAKE ----------------------------------------------------------
    {
        "id": "ServeDaemon",
        "region": "intake",
        "does": "Polls every minute, runs the admission gates in a fixed order, "
        "claims exactly one item, launches a run.",
        "interface": "serve_cycle(root, ...) -> CycleResult",
        "implemented_by": ["kstrl.serve"],
        "entry": "serve_cycle",
    },
    {
        "id": "GitHubIntake",
        "region": "intake",
        "does": "Turns a labelled issue into a queue item after checking the label "
        "was applied before the body was last edited.",
        "interface": "sync(queue, config, root) -> SyncResult",
        "implemented_by": ["kstrl.intake_github"],
        "entry": "sync",
    },
    {
        "id": "WorkQueue",
        "region": "intake",
        "does": "Maildir-style queue with leases, a reaper and backoff; one item per cycle.",
        "interface": "Queue.next_ready / lease / start / transition",
        "implemented_by": ["kstrl.workqueue"],
        "entry": "Queue",
    },
    {
        "id": "SpendLedger",
        "region": "intake",
        "does": "Daily spend and consecutive-poison counts, rewritten whole under "
        "the control lock.",
        "interface": "SpendLedger.read_state / record_terminal",
        "implemented_by": ["kstrl.serve"],
        "entry": "SpendLedger",
    },
    {
        "id": "Inbox",
        "region": "intake",
        "does": "Everything awaiting a human decision, with a cap consulted before admission.",
        "interface": "Inbox.add / open_items",
        "implemented_by": ["kstrl.inbox"],
        "entry": "Inbox",
    },
    {
        "id": "FlowControl",
        "region": "intake",
        "does": "R10.7 #228: refuse admission while max_open_prs kstrl PRs are open.",
        "interface": "check_open_pr_bound",
        "implemented_by": ["kstrl.serve"],
        "entry": "check_open_pr_bound",
        "tracker": "R10.7 #228",
    },
    {
        "id": "Steering",
        "region": "intake",
        "does": "R10.10 #231: /memory and /iterate comments on kstrl PRs, polled.",
        "interface": "poll_steering",
        "implemented_by": ["kstrl.intake_github"],
        "entry": "poll_steering",
        "tracker": "R10.10 #231",
    },
    # ---- PLAN ------------------------------------------------------------
    {
        "id": "Architect",
        "region": "plan",
        "does": "Red-teams the spec (halts on blockers) and decomposes it into a "
        "component DAG with per-component PRDs.",
        "interface": "decompose_spec(...) -> Manifest",
        "implemented_by": ["kstrl.decompose"],
        "entry": "decompose_spec",
    },
    {
        "id": "Manifest",
        "region": "plan",
        "does": "The component DAG, per-component status, PR pointers, policy "
        "hash; the resumable source of truth.",
        "interface": "Manifest.load / save / get_ready_components",
        "implemented_by": ["kstrl.manifest"],
        "entry": "Manifest",
    },
    {
        "id": "PRD",
        "region": "plan",
        "does": "Per-component user stories with acceptance criteria and the "
        "passes flag the engineer sets.",
        "interface": "PRD.load / save",
        "implemented_by": ["kstrl.prd"],
        "entry": "PRD",
    },
    # ---- BUILD -----------------------------------------------------------
    {
        "id": "Feedforward",
        "region": "build",
        "does": "Computes module map, public interfaces, import graph and "
        "conventions from the tree, no model call.",
        "interface": "build_feedforward_context(path, config) -> str",
        "implemented_by": ["kstrl.feedforward"],
        "entry": "build_feedforward_context",
    },
    {
        "id": "KnowledgeInjector",
        "region": "build",
        "does": "Selects distilled facts (core, dependency, sibling tiers) for "
        "this component's prompt under token caps.",
        "interface": "build_knowledge_context(...) -> str",
        "implemented_by": ["kstrl.knowledge"],
        "entry": "build_knowledge_context",
    },
    {
        "id": "RetryContext",
        "region": "build",
        "does": "The failures handed to the next attempt; append-only today, "
        "level-triggered after R10.2.",
        "interface": "IterationContext.format_for_prompt",
        "implemented_by": ["kstrl.context"],
        "entry": "IterationContext",
    },
    {
        "id": "EngineerLoop",
        "region": "build",
        "does": "Builds the prompt once, runs the agent up to max_iterations, "
        "watches for the completion marker, enforces allowed paths, trips the "
        "breaker.",
        "interface": "run_loop(config, agent, ...) -> LoopResult",
        "implemented_by": ["kstrl.loop"],
        "entry": "run_loop",
    },
    {
        "id": "AgentAdapter",
        "region": "build",
        "does": "Runs the coding agent's CLI, its SDK or a custom command as a "
        "subprocess through a deadline streamer; scrapes usage.",
        "interface": "Agent.run(prompt, cwd, timeout) -> Iterator[str]",
        "implemented_by": _AGENT_MODULES,
        "entry": "Agent",
    },
    {
        "id": "Breaker",
        "region": "build",
        "does": "Halts a component after N iterations with an unchanged diff hash "
        "and test signature.",
        "interface": "NoProgressBreaker.record_iteration",
        "implemented_by": ["kstrl.breaker"],
        "entry": "NoProgressBreaker",
    },
    {
        "id": "PathGuard",
        "region": "build",
        "does": "Reverts or fails changes outside allowedPaths before the "
        "completion marker is honoured.",
        "interface": "enforce_allowed_paths(...)",
        "implemented_by": ["kstrl.guards"],
        "entry": "enforce_allowed_paths",
    },
    {
        "id": "OperatorContext",
        "region": "build",
        "does": "R10.8 #229 and R10.9 #230: golden patterns and the memory file "
        "loaded into the prefix.",
        "interface": "load_operator_file",
        "implemented_by": ["kstrl.operator_context"],
        "entry": "load_operator_file",
        "tracker": "R10.8 #229, R10.9 #230",
    },
    # ---- MEASURE ---------------------------------------------------------
    {
        "id": "MechanicalVerifier",
        "region": "measure",
        "does": "Tests, typecheck, lint, diff scope, bad patterns, plus opt-in "
        "policy, adequacy, dead code, mutation, fixtures; all checks run even "
        "if earlier ones fail.",
        "interface": "run_mechanical_verification(...) -> VerificationResult",
        "implemented_by": ["kstrl.verify", "kstrl.commandrun"],
        "entry": "run_mechanical_verification",
    },
    {
        "id": "FailureParsers",
        "region": "measure",
        "does": "Turns raw pytest, mypy and ruff output into structured failures "
        "with file, line, source context and a fix hint, so the agent is "
        "handed a target rather than a wall of stderr.",
        "interface": "parse_* functions -> structured failures",
        "implemented_by": ["kstrl.parsers"],
        "entry": "parse_pytest_output",
    },
    {
        "id": "Reviewer",
        "region": "measure",
        "does": "Independent LLM verdict per acceptance criterion plus concerns; "
        "cross-family by default; fails closed on empty or partial output.",
        "interface": "run_review(...) -> ReviewResult",
        "implemented_by": ["kstrl.review"],
        "entry": "run_review",
    },
    {
        "id": "SecurityReviewer",
        "region": "measure",
        "does": "OWASP/CWE-mapped LLM review; off by default; hard mode fails at "
        "the severity threshold.",
        "interface": "run_security_review(...) -> SecurityResult",
        "implemented_by": ["kstrl.security"],
        "entry": "run_security_review",
    },
    {
        "id": "ContractTester",
        "region": "measure",
        "does": "Integration tests on merged tiers; attributes a failure to the "
        "most recently merged component.",
        "interface": "run_contract_testing(...)",
        "implemented_by": ["kstrl.contract"],
        "entry": "run_contract_testing",
    },
    {
        "id": "FixturesOracle",
        "region": "measure",
        "does": "Approved input/output pairs run sandboxed outside the "
        "agent-writable tree; snapshot regression.",
        "interface": "check_fixtures_from_prd",
        "implemented_by": ["kstrl.fixtures", "kstrl.sandbox"],
        "entry": "check_fixtures_from_prd",
    },
    {
        "id": "AdequacyGate",
        "region": "measure",
        "does": "Reads the diff for deleted tests, added skips, lost assertions, "
        "and new tests with no strong oracle.",
        "interface": "lint_test_source / analyze_test_diff",
        "implemented_by": ["kstrl.adequacy"],
        "entry": "analyze_test_diff",
    },
    {
        "id": "PolicyEnvelope",
        "region": "measure",
        "does": "Declarative merge guardrails on the diff and lockfile; "
        "enforcement-machinery paths halt at every level.",
        "interface": "evaluate_policy(...) -> PolicyEvaluation",
        "implemented_by": ["kstrl.policy", "kstrl.licensing"],
        "entry": "evaluate_policy",
    },
    {
        "id": "Findings",
        "region": "measure",
        "does": "The typed error signal every sensor emits; infrastructure_error "
        "and phase_skipped make an empty list a safe success.",
        "interface": "Finding",
        "implemented_by": ["kstrl.findings"],
        "entry": "Finding",
    },
    {
        "id": "Calibration",
        "region": "measure",
        "does": "Runs the adversarial roles against planted bugs and compares "
        "detection rates to a saved baseline; the sensor pointed at the sensors.",
        "interface": "compare_baselines(old, new) -> Comparison",
        "implemented_by": ["kstrl.calibration"],
        "entry": "compare_baselines",
    },
    {
        "id": "Sense",
        "region": "measure",
        "does": "R10.1 #222: every mechanical sensor run by hand against any tree with --json.",
        "interface": "ks sense",
        "implemented_by": ["kstrl.cli"],
        "entry": "sense",
        "tracker": "R10.1 #222",
    },
    # ---- DECIDE ----------------------------------------------------------
    {
        "id": "Pipeline",
        "region": "decide",
        "does": "Per-component phase chain: verify, diff, review, security, "
        "distill, checkpoint, PR; decides retry, fail, or complete; owns the "
        "adversarial budget.",
        "interface": "ComponentPipeline.process_result",
        "implemented_by": ["kstrl.pipeline", "kstrl.retry_plan"],
        "entry": "ComponentPipeline",
    },
    {
        "id": "Scheduler",
        "region": "decide",
        "does": "Schedules ready components into worktrees under max_parallel, "
        "runs contract testing, resets breakers, records autonomy outcomes.",
        "interface": "run_factory(...) -> FactoryResult",
        "implemented_by": ["kstrl.factory", "kstrl.shutdown", "kstrl.launch", "kstrl.runid"],
        "entry": "run_factory",
    },
    {
        "id": "HumanCheckpoint",
        "region": "decide",
        "does": "The optional pause before a merge: shows the diff excerpt, both "
        "finding streams and the attempt's cost, and waits for approve, reject "
        "or retry; unattended, it parks the decision in the inbox instead of "
        "proceeding.",
        "interface": "CheckpointContext / the E6 checkpoint UI",
        "implemented_by": ["kstrl.interaction"],
        "entry": "CheckpointContext",
    },
    {
        "id": "Configuration",
        "region": "decide",
        "does": "Every gain the loops run on: kstrl.toml sections, environment "
        "overrides and CLI flags resolved once per process with env over toml "
        "over defaults; per-phase timeouts; the resolved values reported with "
        "provenance.",
        "interface": "KstrlConfig / TimeoutConfig / <Section>Config.load(root)",
        "implemented_by": ["kstrl.config", "kstrl.timeout", "kstrl.config_report"],
        "entry": "KstrlConfig",
    },
    # ---- SHIP ------------------------------------------------------------
    {
        "id": "Worktrees",
        "region": "ship",
        "does": "Isolated per-component worktrees cut from origin/<base>, "
        "flocked, recreated fresh after timeouts and conflicts.",
        "interface": "fetch_base_branch / resolve_base_ref; factory._setup_worktree drives them",
        "implemented_by": ["kstrl.git"],
        "entry": "fetch_base_branch",
        "note": "The worktree command itself is a private function in "
        "kstrl.factory; kstrl.git holds the public base-branch primitives it "
        "is built on, so this entry tracks those.",
    },
    {
        "id": "PullRequests",
        "region": "ship",
        "does": "Push, create, merge and wait; completion is merge-gated; "
        "MERGE_PENDING parks a component.",
        "interface": "push_create_and_merge_pr(...) -> PrOutcome",
        "implemented_by": ["kstrl.pr"],
        "entry": "push_create_and_merge_pr",
    },
    {
        "id": "Distiller",
        "region": "ship",
        "does": "Writes durable facts about the built artifact to disk before the PR merges.",
        "interface": "distill_facts(...)",
        "implemented_by": ["kstrl.knowledge"],
        "entry": "distill_facts",
    },
    {
        "id": "Dampener",
        "region": "ship",
        "does": "R10.6 #227: baseline in version control plus a per-PR regression report.",
        "interface": "ks sense --compare-baseline",
        "implemented_by": ["kstrl.cli"],
        "entry": "compare_baseline",
        "tracker": "R10.6 #227",
        "note": "Entry name is provisional: R10.6 adds a flag to ks sense, so the "
        "helper that flag calls must be named compare_baseline, or this entry "
        "updated, for the state to derive.",
    },
    {
        "id": "ReleaseStage",
        "region": "ship",
        "does": "R8.7 #154: deploy drivers, verification ladder, rollback doctrine.",
        "interface": "(Phase 4)",
        "implemented_by": ["kstrl.release"],
        "entry": "run_release",
        "tracker": "R8.7 #154",
        "note": "Entry name is provisional until R8.7 names its module.",
    },
    # ---- TRUST -----------------------------------------------------------
    {
        "id": "AutonomyLadder",
        "region": "trust",
        "does": "L1 to L4; promotion needs evidence plus a human ack; demotion is "
        "automatic; the flag bundle is derived at run start and can only "
        "withhold.",
        "interface": "AutonomyState / resolve_runtime_level / flag_bundle_for",
        "implemented_by": ["kstrl.autonomy"],
        "entry": "AutonomyState",
    },
    {
        "id": "Replay",
        "region": "trust",
        "does": "Replays the ladder's thresholds over recorded history and reports, never decides.",
        "interface": "replay_file(...)",
        "implemented_by": ["kstrl.autonomy_replay"],
        "entry": "replay_file",
    },
    {
        "id": "StateDir",
        "region": "trust",
        "does": "Resolves the control directory outside the tree and refuses to "
        "trust it when it is inside, symlinked or unreadable.",
        "interface": "control_file / control_untrusted_reason / control_lock",
        "implemented_by": ["kstrl.statedir"],
        "entry": "control_untrusted_reason",
    },
    {
        "id": "SafeMode",
        "region": "trust",
        "does": "R10.4 #225: one predicate over the four degraded states.",
        "interface": "safe_mode_reasons(root)",
        "implemented_by": ["kstrl.safemode"],
        "entry": "safe_mode_reasons",
        "tracker": "R10.4 #225",
    },
    {
        "id": "HealthTrending",
        "region": "trust",
        "does": "R8.4 #151: control-chart rules over run metrics; the HEALTH_BREACH sensor.",
        "interface": "health_breaches",
        "implemented_by": ["kstrl.health"],
        "entry": "health_breaches",
        "tracker": "R8.4 #151",
    },
    # ---- LEARN -----------------------------------------------------------
    {
        "id": "EvolutionJournal",
        "region": "learn",
        "does": "Append-only record of every component outcome, failure "
        "signature, cost and finding summary.",
        "interface": "EvolutionJournal.record_run",
        "implemented_by": ["kstrl.evolution"],
        "entry": "EvolutionJournal",
    },
    {
        "id": "Proposals",
        "region": "learn",
        "does": "Turns recurring journal patterns into markdown proposals; applies "
        "only convention proposals, behind a prompt.",
        "interface": "list_proposals / apply_proposal",
        "implemented_by": ["kstrl.proposals"],
        "entry": "apply_proposal",
        "note": "Pattern detection and proposal writing live on "
        "EvolutionJournal.propose_improvements; this module reads and applies "
        "the files it writes.",
    },
    {
        "id": "Playbook",
        "region": "learn",
        "does": "R9 #217: global playbook of attributed lessons under the XDG state home.",
        "interface": "(store)",
        "implemented_by": ["kstrl.playbook"],
        "entry": "Playbook",
        "tracker": "R9 #217",
        "note": "Entry name is provisional until R9 names its module.",
    },
    {
        "id": "RuntimeSignals",
        "region": "learn",
        "does": "R8.8 #155: error and health signals polled into the queue with a "
        "reproducing-test rule.",
        "interface": "ks signals poll",
        "implemented_by": ["kstrl.signals"],
        "entry": "poll_signals",
        "tracker": "R8.8 #155",
        "note": "Entry name is provisional until R8.8 names its module.",
    },
    # ---- OBSERVE ---------------------------------------------------------
    {
        "id": "EventBus",
        "region": "observe",
        "does": "Typed, schema-versioned events fanned out to sinks; sinks are "
        "observability, never control flow.",
        "interface": "EventBus.emit",
        "implemented_by": ["kstrl.events"],
        "entry": "EventBus",
    },
    {
        "id": "Reducer",
        "region": "observe",
        "does": "Folds an event stream into the renderable run state every surface shows.",
        "interface": "load_run_state(...) -> RunState",
        "implemented_by": ["kstrl.reducer"],
        "entry": "load_run_state",
    },
    {
        "id": "Dashboard",
        "region": "observe",
        "does": "The Textual TUI: home shell, run board, component detail, "
        "checkpoint modal; a view, never the record.",
        "interface": "KstrlTuiApp",
        "implemented_by": _TUI_MODULES,
        "entry": "KstrlTuiApp",
    },
    {
        "id": "ProgressLog",
        "region": "observe",
        "does": "The v1 append-only JSONL log kept byte-compatible for existing consumers.",
        "interface": "ProgressLog.emit",
        "implemented_by": ["kstrl.observability"],
        "entry": "ProgressLog",
    },
    {
        "id": "LinearMirror",
        "region": "observe",
        "does": "One-way outbound mirror of component status to the issue "
        "tracker; warns and degrades, never fails a run.",
        "interface": "LinearSink",
        "implemented_by": ["kstrl.linear"],
        "entry": "LinearSink",
    },
    {
        "id": "CLI",
        "region": "observe",
        "does": "The ks command tree: run, factory, decompose, serve, status, "
        "dash, autonomy, inbox, queue, evolve.",
        "interface": "cli",
        "implemented_by": _CLI_MODULES,
        "entry": "cli",
    },
    # ---- actors, outside the factory ------------------------------------
    {
        "id": "Operator",
        "region": None,
        "container": "operator",
        "does": "Labels issues, edits specs and prompts, approves checkpoints, "
        "promotes autonomy, reads the inbox and the dashboard.",
        "interface": "external",
        "implemented_by": [],
        "entry": "",
        "external": True,
    },
    {
        "id": "GitHubIssues",
        "region": None,
        "container": "github",
        "does": "The remote inbox: an issue plus a label is a request.",
        "interface": "external",
        "implemented_by": [],
        "entry": "",
        "external": True,
    },
    {
        "id": "GitHubPRs",
        "region": None,
        "container": "github",
        "does": "The output: one pull request per component, merge-gated.",
        "interface": "external",
        "implemented_by": [],
        "entry": "",
        "external": True,
    },
    {
        "id": "CodingAgent",
        "region": None,
        "container": "agent_cli",
        "does": "Whichever coding agent the project configures, or a custom command.",
        "interface": "external",
        "implemented_by": [],
        "entry": "",
        "external": True,
    },
]

for _c in COMPONENTS:
    if _c["id"] in _ACTORS:
        _c["x"], _c["y"] = _ACTORS[_c["id"]]
        _c["kind"] = "actor"
    else:
        _col, _row, _kind = _PLACE[_c["id"]]
        _c["x"], _c["y"], _c["kind"] = COL[_col], ROW[_row], _kind

# (from, to, artifact carried, dataflow)
FLOWS: list[tuple[str, str, str, str]] = [
    ("GitHubIssues", "GitHubIntake", "labelled issue", "intake"),
    ("GitHubIntake", "WorkQueue", "queue item", "intake"),
    ("WorkQueue", "ServeDaemon", "next ready item", "intake"),
    ("SpendLedger", "ServeDaemon", "admission verdict", "intake"),
    ("Inbox", "ServeDaemon", "open-item cap", "intake"),
    ("ServeDaemon", "Scheduler", "factory run", "intake"),
    ("Operator", "Architect", "spec", "plan"),
    ("Architect", "Manifest", "component DAG", "plan"),
    ("Architect", "PRD", "stories + criteria", "plan"),
    ("Configuration", "Scheduler", "resolved config + timeouts", "decide"),
    ("Manifest", "Scheduler", "ready components", "decide"),
    ("Scheduler", "Worktrees", "worktree per component", "ship"),
    ("Configuration", "EngineerLoop", "max_iterations, sleep, prompt paths", "build"),
    ("Feedforward", "EngineerLoop", "computed context", "build"),
    ("KnowledgeInjector", "EngineerLoop", "facts", "build"),
    ("RetryContext", "EngineerLoop", "failures from last attempt", "build"),
    ("EngineerLoop", "AgentAdapter", "prompt", "build"),
    ("AgentAdapter", "CodingAgent", "stdin prompt", "build"),
    ("CodingAgent", "AgentAdapter", "stream + COMPLETE marker", "build"),
    ("PRD", "EngineerLoop", "next failing story", "build"),
    ("EngineerLoop", "PRD", "passes flag (a claim)", "build"),
    ("Breaker", "EngineerLoop", "stall verdict", "build"),
    ("PathGuard", "EngineerLoop", "scope verdict", "build"),
    ("EngineerLoop", "Pipeline", "LoopResult", "decide"),
    ("Pipeline", "MechanicalVerifier", "worktree + PRD", "measure"),
    ("Pipeline", "Reviewer", "diff + criteria", "measure"),
    ("Pipeline", "SecurityReviewer", "diff", "measure"),
    ("PolicyEnvelope", "MechanicalVerifier", "envelope verdict", "measure"),
    ("AdequacyGate", "MechanicalVerifier", "adequacy findings", "measure"),
    ("FixturesOracle", "MechanicalVerifier", "fixture results", "measure"),
    ("MechanicalVerifier", "Findings", "check results", "measure"),
    ("MechanicalVerifier", "FailureParsers", "raw tool output", "measure"),
    ("FailureParsers", "Findings", "structured failures", "measure"),
    ("Reviewer", "Findings", "criterion verdicts + concerns", "measure"),
    ("SecurityReviewer", "Findings", "vulnerability findings", "measure"),
    ("Findings", "Pipeline", "the error signal", "decide"),
    ("Pipeline", "RetryContext", "retry", "decide"),
    ("Pipeline", "Distiller", "passed diff", "ship"),
    ("Distiller", "KnowledgeInjector", "durable facts", "learn"),
    ("Pipeline", "PullRequests", "merge", "ship"),
    ("PullRequests", "GitHubPRs", "pull request", "ship"),
    ("PullRequests", "ContractTester", "merged tiers", "measure"),
    ("ContractTester", "Scheduler", "breaker component to reset", "decide"),
    ("Pipeline", "Inbox", "checkpoint / policy exception / budget overrun", "trust"),
    ("Pipeline", "HumanCheckpoint", "diff, findings, cost", "trust"),
    ("HumanCheckpoint", "Operator", "approve / reject / retry", "trust"),
    ("HumanCheckpoint", "Inbox", "parked merge gate", "trust"),
    ("Operator", "Configuration", "kstrl.toml, env, flags", "decide"),
    ("Scheduler", "AutonomyLadder", "run outcome", "trust"),
    ("AutonomyLadder", "Pipeline", "flag bundle (withhold only)", "trust"),
    ("PolicyEnvelope", "AutonomyLadder", "violation -> demotion", "trust"),
    ("Calibration", "AutonomyLadder", "regression (R10.11)", "trust"),
    ("AutonomyLadder", "StateDir", "autonomy.json", "trust"),
    ("SpendLedger", "StateDir", "spend.json", "trust"),
    ("Inbox", "StateDir", "inbox.jsonl", "trust"),
    ("Scheduler", "EvolutionJournal", "component outcomes + signatures", "learn"),
    ("EvolutionJournal", "Proposals", "recurring patterns", "learn"),
    ("EvolutionJournal", "Replay", "experiments.tsv", "trust"),
    ("Pipeline", "EventBus", "typed events", "observe"),
    ("Scheduler", "EventBus", "typed events", "observe"),
    ("EventBus", "ProgressLog", "v1 mirror", "observe"),
    ("EventBus", "Reducer", "events.jsonl", "observe"),
    ("Reducer", "Dashboard", "RunState", "observe"),
    ("Reducer", "CLI", "ks status", "observe"),
    ("EventBus", "LinearMirror", "failures, budget halts", "observe"),
    ("Dashboard", "Operator", "board, checkpoint modal", "observe"),
    ("Operator", "Inbox", "approve / reject / snooze", "trust"),
    ("Operator", "AutonomyLadder", "promote with ack", "trust"),
]

# The dataflows a reader can trace one at a time, in reading order.
FLOW_KINDS: list[str] = [
    "intake",
    "plan",
    "build",
    "measure",
    "decide",
    "ship",
    "trust",
    "learn",
    "observe",
]


def build_state(component: dict[str, Any], atlas: dict[str, Any]) -> str:
    """built | partial | planned, derived from what the atlas actually found.

    A component with a `tracker` is a roadmap item: until its entry exists it is
    planned, not "part built", even when the module it will land in already
    exists (a new command on an existing CLI module, for instance).
    """
    modules = component.get("implemented_by") or []
    if not modules:
        return "planned"
    components = atlas.get("components", {})
    present = [m for m in modules if m in components]
    if not present:
        return "planned"
    entry = component.get("entry") or ""
    if not entry:
        # The parts exist but nothing named assembles them.
        return "partial"
    found = any(
        entry in [f["name"] for f in components[m]["functions"]]
        or entry in [c["name"] for c in components[m]["classes"]]
        for m in present
    )
    if not found:
        return "planned" if component.get("tracker") else "partial"
    return "built" if len(present) == len(modules) else "partial"


# Model-call budget per unit of work. Marking which components call a model
# is the most load-bearing distinction in this system: everything unmarked is
# deterministic machinery, and invariant 12 says the two must never be the
# same component.
CALL_BUDGET: dict[str, str] = {
    "Architect": "1 decompose call per spec",
    "EngineerLoop": "up to max_iterations agent calls per attempt",
    "Reviewer": "1 call per component per attempt (chunked: 1 per chunk)",
    "SecurityReviewer": "1 call per component per attempt when enabled",
    "Distiller": "1 call per completed component",
    "Calibration": "3 runs per fixture per role, opt-in",
}

# Where the documents define each component: an ARCHITECTURE.md heading for a
# shipped component, a docs/control-loop-design.md section for a planned one.
SPEC_ANCHOR: dict[str, str] = {
    "ServeDaemon": "ARCHITECTURE.md: Runtime state layout",
    "GitHubIntake": "docs/continuous-intake.md",
    "WorkQueue": "docs/continuous-intake.md",
    "SpendLedger": "ARCHITECTURE.md: Runtime state layout",
    "Inbox": "ARCHITECTURE.md: Runtime state layout",
    "FlowControl": "control-loop-design 5.6",
    "Steering": "control-loop-design 5.8",
    "Architect": "ARCHITECTURE.md: Factory mode",
    "Manifest": "ARCHITECTURE.md: Factory mode",
    "PRD": "ARCHITECTURE.md: The iteration loop",
    "Feedforward": "ARCHITECTURE.md: The iteration loop",
    "KnowledgeInjector": "ARCHITECTURE.md: The iteration loop",
    "RetryContext": "control-loop-design 5.3",
    "EngineerLoop": "ARCHITECTURE.md: The iteration loop",
    "AgentAdapter": "ARCHITECTURE.md: The iteration loop",
    "Breaker": "ARCHITECTURE.md: The iteration loop",
    "PathGuard": "ARCHITECTURE.md: The iteration loop",
    "OperatorContext": "control-loop-design 5.4, 5.7",
    "MechanicalVerifier": "ARCHITECTURE.md: The pipeline",
    "Reviewer": "ARCHITECTURE.md: The pipeline",
    "SecurityReviewer": "ARCHITECTURE.md: The pipeline",
    "ContractTester": "ARCHITECTURE.md: Factory mode",
    "FixturesOracle": "ARCHITECTURE.md: The fixtures sandbox",
    "AdequacyGate": "ARCHITECTURE.md: The pipeline",
    "PolicyEnvelope": "ARCHITECTURE.md: The pipeline",
    "Findings": "ARCHITECTURE.md: The pipeline",
    "FailureParsers": "ARCHITECTURE.md: The iteration loop",
    "Calibration": "docs/adversarial-design.md",
    "Sense": "control-loop-design 5.1",
    "Pipeline": "ARCHITECTURE.md: The pipeline",
    "HumanCheckpoint": "ARCHITECTURE.md: The pipeline",
    "Configuration": "ARCHITECTURE.md: The iteration loop",
    "Scheduler": "ARCHITECTURE.md: Factory mode",
    "Worktrees": "ARCHITECTURE.md: Factory mode",
    "PullRequests": "ARCHITECTURE.md: The pipeline",
    "Distiller": "ARCHITECTURE.md: The pipeline",
    "Dampener": "control-loop-design 5.5",
    "ReleaseStage": "docs/dark-factory-roadmap.md R8.7",
    "AutonomyLadder": "ARCHITECTURE.md: Runtime state layout",
    "Replay": "ARCHITECTURE.md: Runtime state layout",
    "StateDir": "ARCHITECTURE.md: Runtime state layout",
    "SafeMode": "control-loop-design 5.9",
    "HealthTrending": "control-loop-design 5.11",
    "EvolutionJournal": "ARCHITECTURE.md: The learning loop",
    "Proposals": "ARCHITECTURE.md: The learning loop",
    "Playbook": "docs/continuous-learning-design.md",
    "RuntimeSignals": "docs/dark-factory-roadmap.md R8.8",
    "EventBus": "ARCHITECTURE.md: The event-stream substrate",
    "Reducer": "ARCHITECTURE.md: The event-stream substrate",
    "Dashboard": "ARCHITECTURE.md: The event-stream substrate",
    "ProgressLog": "ARCHITECTURE.md: The event-stream substrate",
    "LinearMirror": "docs/linear-integration.md",
    "CLI": "ARCHITECTURE.md: The event-stream substrate",
}

# The load-bearing rules of this system, from CLAUDE.md (H1 to H4), the
# roadmap doctrine, and docs/control-loop-design.md. They say WHY a part is
# shaped the way it is, which is the one thing the code cannot tell you.
INVARIANTS: dict[int, str] = {
    1: "H1: AI-generated code is never gated by AI self-review.",
    2: "H2: any adversarial prompt-body change re-runs calibration and records the delta.",
    3: "H3: prompt version and snapshot hash move together in one diff.",
    4: "H4: every done claim states what was tested versus assumed.",
    5: "Doctrine: integrate at the edges, build only thin middles.",
    6: "Doctrine: enforcement reads artifacts, never agent self-report.",
    7: "Doctrine: autonomy is earned, bounded, revocable; the bundle may only withhold.",
    8: "Doctrine: no assumed numbers; advisory first, graduate on the operator's judgement.",
    9: "Doctrine: the human is a role, not a bottleneck; boundary conditions route to the inbox.",
    10: "Doctrine: the first-class phase count is frozen; new evaluators land inside a phase.",
    11: "Doctrine: new outcome surfaces reuse the shared disposition; existing "
    "enums are not retrofitted.",
    12: "Loop rule: what acts never measures its own result (a measurement "
    "written by the actuator is not a measurement).",
    13: "Loop rule: every component runs by hand first; a sensor reachable only "
    "through a factory run cannot be tuned.",
    14: "Record rule: the filesystem is the event bus; sinks are observability, "
    "never control flow; the dashboard is a view.",
    15: "Trust boundary: control state lives outside the agent-reachable tree; "
    "unreadable control state fails closed.",
}

# Component -> the invariants it is directly governed by.
GOVERNED_BY: dict[str, list[int]] = {
    "MechanicalVerifier": [6, 12, 13],
    "Reviewer": [1, 2, 3, 6, 12],
    "SecurityReviewer": [2, 3, 6, 12],
    "Calibration": [2, 8],
    "PolicyEnvelope": [6, 7],
    "AdequacyGate": [6, 8],
    "FixturesOracle": [6],
    "AutonomyLadder": [7, 8, 15],
    "StateDir": [15],
    "Inbox": [9],
    "Pipeline": [10, 11, 12],
    "Scheduler": [10, 11],
    "EngineerLoop": [12, 13],
    "RetryContext": [12],
    "PRD": [12],
    "EventBus": [14],
    "Reducer": [14],
    "Dashboard": [14],
    "ProgressLog": [14],
    "ServeDaemon": [8, 9, 15],
    "SpendLedger": [15],
    "Distiller": [6],
    "Proposals": [8],
    "Sense": [13],
    "SafeMode": [9, 15],
    "FlowControl": [8, 9],
    "Dampener": [8, 12],
    "OperatorContext": [9],
    "Steering": [9],
    "HealthTrending": [8],
    "HumanCheckpoint": [4, 7, 9],
    "FailureParsers": [6, 12],
    "Configuration": [8, 15],
}

# Card geometry the layout check shares with the schematic.
CARD_W = 150
CARD_H = {"component": 56, "store": 52, "actor": 44}


def _box(c: dict[str, Any]) -> tuple[int, int, int, int]:
    return c["x"], c["y"], CARD_W, CARD_H[c["kind"]]


def _inside(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def layout_problems() -> list[str]:
    """Ways the hand-placed topology contradicts itself. Empty means clean.

    Every card must sit inside the region (or container) the model assigns
    it, no two cards may overlap, and every region must sit inside the
    factory. A card outside its band would draw a topology the model does
    not claim, which is the one lie a hand-placed layout can tell.
    """
    out: list[str] = []
    regions = {r["id"]: r["box"] for r in REGIONS}
    containers = {c["id"]: c["box"] for c in CONTAINERS}
    ids = {c["id"] for c in COMPONENTS}
    for region in REGIONS:
        if not _inside(region["box"], containers["factory"]):
            out.append(f"region {region['id']} is not inside the factory")
    for c in COMPONENTS:
        box = _box(c)
        home = c.get("region") or c.get("container")
        outer = regions.get(home) or containers.get(home)
        if outer is None:
            out.append(f"{c['id']} names no region or container")
        elif not _inside(box, outer):
            out.append(f"{c['id']} is drawn outside {home}")
    for i, a in enumerate(COMPONENTS):
        for b in COMPONENTS[i + 1 :]:
            if _overlap(_box(a), _box(b)):
                out.append(f"{a['id']} overlaps {b['id']}")
    for src, dst, _art, kind in FLOWS:
        if src not in ids or dst not in ids:
            out.append(f"flow {src} -> {dst} names an unknown component")
        if kind not in FLOW_KINDS:
            out.append(f"flow {src} -> {dst} has unknown kind {kind}")
    for cid in GOVERNED_BY:
        if cid not in ids:
            out.append(f"GOVERNED_BY names unknown component {cid}")
    return out
