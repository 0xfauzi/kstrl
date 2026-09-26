"""The two screens' content, shared by every visual direction.

Nothing here is styled. Every string is real fixture content from the
snippetvault build and the e3root integration-loop run; the directions
differ only in how they draw it.
"""

# ruff: noqa: E501
from __future__ import annotations

from dataclasses import dataclass

PROJECT = "snippetvault"

MAST = {
    "branch": "main",
    "config": "kstrl.toml valid",
    "safe": "safe mode off",
    "serve": "ks serve running",
}

HOME_SUBTITLE = "three things need you · one run live · ks serve running"

NAV = [
    ("home", "Home", 0),
    ("runs", "Runs", 0),
    ("decisions", "Decisions", 2),
    ("failures", "Failures", 1),
    ("serve", "Serve queue", 0),
    ("config", "Config", 0),
    ("learning", "Learning", 0),
]
NAV_START = [("start", "Start a run"), ("init", "Initialise a project")]


@dataclass(frozen=True)
class Row:
    state: str  # passed | failed | waiting | running | parked | queued
    title: str
    detail: str
    action: str = ""
    primary: bool = False
    href: str = "#"


NEEDS = [
    Row(
        "parked",
        "client-commands is waiting for merge approval",
        "merge gate · run 8d80e8 · raised 2s ago · the branch holds reviewed work; nothing is pushed until you decide",
        "Decide",
        primary=True,
    ),
    Row(
        "waiting",
        "The integration loop stopped without a clean verdict",
        "halted run · run fda682 · raised 2s ago · integration-fix-1 failed review and the fix budget (1) is spent",
        "Decide",
    ),
    Row(
        "failed",
        "client-commands failed at verify: Tests failed (exit code 1)",
        "run fail01 · 2 attempts · failed 17:14 today · a retry is available and its scope is known",
        "Review retry",
    ),
]

ACTIVE = [
    Row(
        "running",
        "factory live01 · running · 3 of 6 components",
        "http-app · engineer · iteration 3 of 10 · last output 8s ago · worker alive, checked 2s ago · elapsed 1:03:42",
        "Open board",
    ),
    Row(
        "running",
        "ks serve · running · snippetvault slice 3: export and import",
        "runs live01 above · last output 8s ago · daemon alive, checked 2s ago",
        "Queue",
    ),
    Row(
        "queued",
        "ks serve · queued 1st · snippetvault slice 4: sharing",
        "starts when live01 finishes and every admission check passes",
    ),
]

SPEND = ("$19.24", "$78.00", 25)
CLOCK = "1:03:42"

MAIN_AT = (
    "cea97b4",
    "waiting",
    "CI unknown",
    "gh api failed (4): HTTP 401: Bad credentials · read 2s ago",
)

# (group heading, [(pr, component, commit, state, word, reason, link)])
DELIVERY = [
    (
        "run live01 · running · integration: no review recorded for this run",
        [
            (
                "PR #3",
                "storage",
                "cea97b4",
                "waiting",
                "unknown",
                "gh api failed (4): HTTP 401: Bad credentials · read 2s ago",
                "",
            ),
            (
                "PR #2",
                "snippet-rules",
                "23dd9ac",
                "failed",
                "failed",
                'check "test" failed · read 2s ago',
                "",
            ),
            (
                "PR #1",
                "token-crypto",
                "4d74d12",
                "passed",
                "passed",
                "7 checks passed · read 2s ago",
                "",
            ),
        ],
    ),
    (
        "run 8d80e8 · completed 2d ago · release ref 4c4706b · integration: no review recorded for this run",
        [
            (
                "PR #9",
                "client-commands",
                "4c4706b",
                "queued",
                "not read yet",
                "ks serve refreshes CI on its own",
                "read now",
            ),
            (
                "PR #8",
                "client-http",
                "4ab99ae",
                "passed",
                "passed",
                "7 checks passed · read 0s ago",
                "",
            ),
        ],
    ),
]

HISTORY_HEAD = ["Run", "Kind", "State", "Last event", "Components", "Tokens", "Cost", "Note"]
HISTORY = [
    (
        "running",
        "live01",
        "factory",
        "running",
        "2s",
        "3 of 6",
        "14.37M",
        "$19.24 of $78.00 · 25%",
        "",
    ),
    (
        "failed",
        "fail01",
        "factory",
        "failed",
        "2s",
        "0 of 1, 1 failed",
        "2.41M",
        "$3.74 of $20.00 · 19%",
        "current: see needs you",
    ),
    (
        "passed",
        "8d80e8",
        "factory",
        "completed",
        "2d",
        "2 of 2",
        "18.51M",
        "$19.30 of $35.00 · 56%",
        "",
    ),
    (
        "waiting",
        "1490e8",
        "factory",
        "unknown",
        "2d",
        "0 of 2",
        "·",
        "·",
        "Refusing to run: stale component branches found",
    ),
    (
        "failed",
        "7ad3ae",
        "factory",
        "failed",
        "2d",
        "0 of 2, 1 failed",
        "8.02M",
        "$10.37 of $45.00 · 24%",
        "superseded by 8d80e8",
    ),
    (
        "passed",
        "4965fb",
        "factory",
        "completed",
        "3d",
        "6 of 6",
        "35.60M",
        "$42.35 of $78.00 · 55%",
        "",
    ),
    (
        "waiting",
        "e3e393",
        "factory",
        "unknown",
        "3d",
        "0 of 6",
        "·",
        "·",
        "Refusing to run: stale component branches found",
    ),
    (
        "waiting",
        "22f40e",
        "factory",
        "unknown",
        "3d",
        "0 of 6",
        "1.22M",
        "$1.64 of $78.00 · 3%",
        "Disallowed changes detected",
    ),
    (
        "passed",
        "db9df0",
        "decompose",
        "completed",
        "3d",
        "6 planned",
        "342.4k",
        "$1.47 · no cap",
        "",
    ),
]
HISTORY_SELECTED = "1490e8"
HISTORY_NOTE_HEAD = "1490e8 · unknown"
HISTORY_NOTE_SUB = "no finish record; last event 2d ago"
HISTORY_NOTE = (
    "Refusing to run: stale component branches found: branch <code>kstrl/factory/client-http</code> "
    "(component client-http) already exists with commits not merged into <code>main</code>; refusing to "
    "silently reuse it. Merge it or delete it (<code>git branch -D kstrl/factory/client-http</code>) and re-run."
)

# ---------------------------------------------------------------- checkpoint

CP_TITLE = "Approve PR creation and merge for comp-c?"
CP_SUBTITLE = "factory checkpoint · the run is waiting on this answer · asked 2s ago"
CP_SPEND = "at least $4.50"
CP_CLOCK = "0:02"

# (label, state or "", word, rest as html)
CHECKS = [
    ("branch", "", "", "<code>kstrl/factory/comp-c</code>"),
    ("verify", "passed", "passed", "tests, typecheck, lint, diff scope"),
    ("review", "passed", "passed", "0 blocking · 2 advisory, listed below"),
    ("security", "passed", "passed", "1 low, listed below"),
    (
        "changed files",
        "",
        "",
        "3 · <code>src/snippetvault/cli.py</code> +201, <code>src/snippetvault/__init__.py</code> +8 -2, <code>src/snippetvault/__main__.py</code> +5",
    ),
    (
        "spend so far",
        "",
        "",
        "<b>at least $4.50</b> · no cost cap · some calls did not report a cost, so this is a lower bound",
    ),
]

# (label, kind: primary | secondary | danger | plain, key hint, consequence html)
CHOICES = [
    (
        "Approve and run",
        "primary",
        "1",
        "<b>Pushes <code>kstrl/factory/comp-c</code>, opens its PR and merges it.</b> comp-c completes once the merge is confirmed; without gh it stays unpushed and the run says so.",
    ),
    (
        "Approve only",
        "secondary",
        "2",
        "Records the approval and nothing else. Nothing merges until the next factory run, which then behaves as above.",
    ),
    (
        "Reject",
        "danger",
        "3",
        "comp-c fails and its dependents are skipped. Nothing is pushed. The branch is kept for you to read.",
    ),
    (
        "Send back to the engineer",
        "plain",
        "4",
        "The engineer runs comp-c again with a note that a human reviewer asked for changes; no reason is passed on. Uses one retry; with none left, comp-c fails as on Reject.",
    ),
    (
        "Decide later",
        "plain",
        "5",
        "Leaves the question open. The run waits at this point; the item stays under Needs you and the elapsed clock keeps running.",
    ),
]

# (phase, severity, where, finding)
FINDINGS = [
    (
        "review",
        "advisory",
        "cli.py:621-634",
        "A connection refused error prints a traceback instead of the exit-2 message the PRD names.",
    ),
    (
        "review",
        "advisory",
        "test_cli_client.py:1923-1929",
        "The test asserts the exit code only; the stderr text the criterion pins is never checked.",
    ),
    (
        "security",
        "low",
        "cli.py:419-429",
        "The --host value reaches the URL unescaped; a crafted host can add a path segment.",
    ),
]

DIFF_SUMMARY = "3 files · +214 -2"
# (kind: file | hunk | del | add | ctx | more, text)
DIFF = [
    ("file", "src/snippetvault/__init__.py"),
    ("hunk", "@@ -1,2 +1,8 @@"),
    ("del", "-def main() -> None:"),
    ("del", '-    print("snippetvault")'),
    ("add", '+"""snippetvault: a private snippet server and its client."""'),
    ("add", "+"),
    ("add", "+from __future__ import annotations"),
    ("add", "+"),
    ("add", "+from snippetvault.cli import main"),
    ("add", "+"),
    ("add", '+__all__ = ["main"]'),
    ("file", "src/snippetvault/__main__.py"),
    ("hunk", "@@ -0,0 +1,5 @@"),
    ("add", "+from __future__ import annotations"),
    ("add", "+"),
    ("add", "+from snippetvault.cli import main"),
    ("add", "+"),
    ("add", "+raise SystemExit(main())"),
    ("file", "src/snippetvault/cli.py"),
    ("hunk", "@@ -0,0 +1,201 @@"),
    ("more", "... 201 added lines · open the whole diff"),
]
