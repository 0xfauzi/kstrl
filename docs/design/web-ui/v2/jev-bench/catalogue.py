"""The closed catalogue the command bar routes into, and the questions Jev answers.

Everything Jev can pick is written here as a closed set. Jev never generates
a value: free values (run ids, component names, durations) are candidates
that code puts into the criteria, and Jev selects among them. Every set has
a no-match or not-named option, so an answer of "nothing here fits" is
always available.

The questions are sent together in one request (speculative fan-out): the
route question and every argument question. Code reads only the arguments
that belong to the chosen route.
"""

from __future__ import annotations

from typing import Any

# The entities the page knows at the moment the operator types. In the
# product these come from the readers (manifest, runs, inbox); here they are
# the snippetvault and e3root fixtures the prototype shows.
KNOWN: dict[str, list[str]] = {
    "components": [
        "snippet-rules",
        "token-crypto",
        "storage",
        "http-app",
        "http-server",
        "cli",
        "client-commands",
        "client-http",
        "comp-c",
        "integration-fix-1",
    ],
    "runs": ["live01", "fail01", "8d80e8", "fda682", "1490e8", "7ad3ae", "4965fb", "db9df0"],
    "waiting_items": [
        "merge gate: client-commands (run 8d80e8)",
        "checkpoint: comp-c (approve PR creation and merge)",
        "halted run fda682 (the integration loop stopped)",
    ],
}

ROUTES: dict[str, str] = {
    "open_run": "go to one run's live board: its components, phases, iterations, liveness and spend",
    "open_component": "go to one component's detail: its attempts, gates, findings and transcript",
    "open_decisions": "go to the list of things waiting on a person: merge gates, checkpoints, halted runs",
    "open_failures": "go to the failures and what a retry of each would do",
    "open_delivery": "go to delivery: the merges to main and the CI state of each merge commit",
    "open_serve": "go to the ks serve queue: the daemon, the queued specs and the admission checks",
    "open_config": "go to the resolved configuration and where each value comes from",
    "open_learning": "go to learning: recurring failure patterns and trends across runs",
    "open_history": "go to the list of past runs",
    "ask_cost": "answer how much was spent and against what cap, as a number rather than a page",
    "ask_why_failed": "answer why a component or run failed: which gate, the cause, the evidence",
    "ask_alive": "answer whether the agent or worker is alive and still producing output",
    "ask_main_green": "answer whether main is green: did CI pass on the merge commits",
    "ask_needs_me": "answer what is waiting on the operator's decision right now",
    "decide": "answer a waiting decision: approve, reject, send back to the engineer, or defer a merge gate or checkpoint",
    "retry": "retry a failed component",
    "snooze": "hide a waiting item for a while so it comes back later",
    "start_factory": "start a new factory run or a decompose from a spec",
    "stop_run": "stop a run that is running",
    "serve_control": "pause or resume ks serve's admission of queued work",
    "ci_poll": "read the CI state from GitHub now, without changing anything else",
    "make_view": "build a new view of the data: a list, board, timeline, graph, meter, sparkline, table, number or diff over runs, components, events, costs, findings, CI, the queue or config, filtered, grouped or sorted",
    "toggle_theme": "switch between the dark and the light theme",
    "no_match": "none of the above: the text is not something this page can do, or it is about something outside kstrl",
}

# Argument questions. Each is a Choice with a closed set; the last option of
# most sets means "the command does not say", which is the argument's
# absence. Code applies the default when Jev picks it.
ARGS: dict[str, dict[str, Any]] = {
    "decide_choice": {
        "instructions": "For a waiting decision, which answer does the operator give? Read `command`.",
        "criteria": {
            "approve_run": "approve it and let the run push, open the PR and merge now",
            "approve_only": "record the approval only; nothing runs or merges until the next run",
            "reject": "reject it: the component fails and nothing is pushed",
            "send_back": "send it back to the engineer for another attempt",
            "later": "leave it open and decide later",
            "unspecified": "the command does not say which answer, or gives no decision",
        },
    },
    "target_item": {
        "instructions": "Which waiting item does `command` refer to?",
        "criteria": {item: None for item in KNOWN["waiting_items"]} | {"not named": "the command names no waiting item"},
    },
    "component": {
        "instructions": "Which component does `command` name or clearly refer to?",
        "criteria": {c: None for c in KNOWN["components"]} | {"not named": "no component is named"},
    },
    "run": {
        "instructions": "Which run does `command` name or clearly refer to?",
        "criteria": {r: None for r in KNOWN["runs"]}
        | {
            "the live run": "the run that is running now, without naming an id",
            "not named": "no run is named or implied",
        },
    },
    "snooze_duration": {
        "instructions": "For how long should the item be hidden, according to `command`?",
        "criteria": {
            "1h": "about an hour",
            "24h": "about a day",
            "7d": "about a week",
            "unstated": "the command gives no duration",
        },
    },
    "serve_action": {
        "instructions": "Does `command` ask ks serve to stop admitting work, or to start again?",
        "criteria": {"pause": "stop admitting queued work", "resume": "start admitting queued work again"},
    },
    "cost_scope": {
        "instructions": "Whose spend does `command` ask about?",
        "criteria": {
            "this_run": "the run that is running now",
            "all_runs": "every run together, the project's total",
            "named_run": "one run named in the command",
            "unstated": "the command does not say",
        },
    },
    "period": {
        "instructions": "What time period does `command` ask about?",
        "criteria": {
            "today": "today",
            "this_week": "this week or the last seven days",
            "this_month": "this month or the last thirty days",
            "all_time": "all time, every run ever",
            "unstated": "the command gives no period",
        },
    },
    "view_source": {
        "instructions": "For a new view, what data should it show? Read `command`.",
        "criteria": {
            "runs": "runs: past and present factory and decompose runs",
            "components": "components of a run and their state, phases and cost",
            "events": "the event stream of a run: phases, iterations, findings, merges, over time",
            "costs": "spend and tokens, per run, per component or over time",
            "findings": "review and security findings with severity and disposition",
            "ci": "the CI state of merge commits",
            "queue": "the ks serve queue items",
            "config": "configuration values and their sources",
            "diff": "the code changes of a component",
            "unstated": "the command does not say what data",
        },
    },
    "view_form": {
        "instructions": "For a new view, what visual form does `command` ask for?",
        "criteria": {
            "list": "a list of rows",
            "board": "columns of cards, one column per state or group",
            "timeline": "items placed along a time axis",
            "graph": "nodes and edges, such as components and dependencies",
            "meter": "one amount against a cap or limit",
            "sparkline": "a small line or bar chart of a value over time or per item",
            "table": "a grid of rows and columns",
            "number": "a single number",
            "diff": "a code diff",
            "unstated": "the command does not say what form",
        },
    },
    "view_group": {
        "instructions": "For a new view, how should the items be grouped, according to `command`?",
        "criteria": {
            "state": "by state: failed, completed, running, waiting",
            "component": "by component",
            "run": "by run",
            "day": "by day",
            "severity": "by finding severity",
            "phase": "by phase or gate: review, security, verify",
            "none": "no grouping is asked for",
        },
    },
    "view_sort": {
        "instructions": "For a new view, what should the items be ordered by, according to `command`?",
        "criteria": {
            "cost": "by cost or spend",
            "time": "by time, newest or oldest first",
            "name": "by name",
            "state": "by state",
            "none": "no ordering is asked for",
        },
    },
    "view_filter_state": {
        "instructions": "For a new view, which items does `command` limit it to?",
        "criteria": {
            "failed": "only failed ones",
            "completed": "only completed or passed ones",
            "running": "only running ones",
            "waiting": "only ones waiting on a person",
            "all": "no limit on state",
        },
    },
    "view_placement": {
        "instructions": "For a new view, where does `command` want it placed?",
        "criteria": {
            "float": "floating over the page, as a panel that can be closed",
            "dock": "docked or pinned to the page so it stays",
            "unstated": "the command does not say",
        },
    },
    "theme": {
        "instructions": "Which theme does `command` ask for?",
        "criteria": {
            "dark": "the dark theme",
            "light": "the light theme",
            "toggle": "the other one, whichever is not on now, or it does not say",
        },
    },
}

# Which arguments belong to which route. Code reads only these.
ROUTE_ARGS: dict[str, list[str]] = {
    "open_run": ["run"],
    "open_component": ["component"],
    "ask_cost": ["cost_scope", "run", "period"],
    "ask_why_failed": ["component", "run"],
    "ask_alive": ["component"],
    "decide": ["decide_choice", "target_item"],
    "retry": ["component"],
    "snooze": ["target_item", "snooze_duration"],
    "stop_run": ["run"],
    "serve_control": ["serve_action"],
    "make_view": [
        "view_source",
        "view_form",
        "view_group",
        "view_sort",
        "view_filter_state",
        "period",
        "view_placement",
        "component",
        "run",
    ],
    "toggle_theme": ["theme"],
}

ROUTE_QUESTION = {
    "instructions": (
        "The operator of kstrl, a software factory that builds code with AI agents, "
        "typed `command` into the page's command bar. What do they want the page to do?"
    ),
    "criteria": ROUTES,
}


def build_state(command: str) -> dict[str, Any]:
    return {"command": command, "known": KNOWN}
