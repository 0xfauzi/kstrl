"""Mock-ups: failures, inbox, checkpoint."""

# ruff: noqa: E501
from __future__ import annotations

from html import escape

from mock_shell import lamp, mast, page, panel, spend

# ------------------------------------------------------------- failures ----


def build_failures() -> None:
    table = f"""
<table class="data">
<thead><tr><th>Run</th><th>Component</th><th>Cause</th><th class="num">Tries</th><th>Failed</th><th>Recovery</th></tr></thead>
<tbody>
<tr class="sel"><td class="mono">fail01</td><td class="mono">client-commands</td><td class="wrap">verify: Tests failed (exit code 1)</td><td class="num">2</td><td class="nowrap">today 17:14</td><td>{lamp("go", "retry available")}</td></tr>
<tr><td class="mono">7ad3ae</td><td class="mono">client-http</td><td class="wrap">parked awaiting merge approval (the merge gate was on and no UI could answer)</td><td class="num">1</td><td class="nowrap">09-23 22:32</td><td class="muted">superseded by 8d80e8</td></tr>
</tbody></table>"""
    scope = """
<dl class="kv">
<dt>starts at</dt><dd>the beginning: the engineer runs again, then every gate after it</dd>
<dt>resets</dt><dd>client-commands to pending</dd>
<dt>stays out</dt><dd>nothing else is failed or skipped</dd>
<dt>worktree</dt><dd>none recorded; nothing to remove</dd>
<dt>branch</dt><dd>deletes <span class="mono">kstrl/factory/client-commands</span>; the retry recreates it from main</dd>
<dt>keeps</dt><dd>the run records under .kstrl/runs, the debug directory, the learning journal</dd>
<dt>runs under</dt><dd>max cost $45 · 2 in parallel · agent timeout 900s · component timeout 2400s</dd>
<dt>not repeated</dt><dd>the recorded verify command option is no longer used, and the command it named never ran. Verification now runs the test, typecheck and lint commands.</dd>
<dt>based on</dt><dd>the manifest as it is now, and the launch settings the failed run saved</dd>
</dl>
<div class="toolbar" style="margin:14px 0 0"><a class="btn primary" href="retry-confirm.html">Retry client-commands</a><span class="muted">or from a shell: <span class="mono">ks retry client-commands</span></span></div>"""
    evidence = """
<p><b>verify failed on attempt 2</b> · Tests failed (exit code 1) · Mechanical verification failed</p>
<div class="path">.kstrl/debug/factory-20260926-171433.450564-fail01/client-commands/attempt-2/test_suite.log</div>
<div class="toolbar" style="margin:10px 0 0"><a class="btn sm" href="component-detail.html">Component detail</a><a class="btn sm" href="gate-output.html">Full output</a></div>"""
    body = f"""
<div class="page-title"><h1>Failures</h1><span class="sub">what failed, and what a retry would do · 1 current · 1 superseded</span></div>
{panel("Failure queue", table, flush=True)}
<div style="height:20px"></div>
<div class="cols">
  <div class="stack">{panel("Retry scope", scope, right="client-commands · run fail01 · stated before it is offered")}</div>
  <div class="stack">{panel("Evidence", evidence)}</div>
</div>"""
    m = mast(clock="1:03:42", readout=spend("$19.24", "$78.00", 25), compact="$19.24 of $78 · 25%")
    page("failures.html", "Failures", m, "failures", body)


def build_failures_empty() -> None:
    body = f"""
<div class="page-title"><h1>Failures</h1><span class="sub">what failed, and what a retry would do</span></div>
{panel("Failure queue", '<div class="empty">No current failures with an available recovery action. Superseded and past failures stay in <a href="home.html">History</a> with the run that replaced them.</div>', flush=True)}"""
    m = mast(serve="ks serve not running")
    page("failures-empty.html", "Failures (none)", m, "failures", body, needs=0, failures=0)


def build_retry_confirm() -> None:
    body = f"""
<div class="page-title"><h1>Retry client-commands?</h1><span class="sub">run fail01 · verify: Tests failed (exit code 1)</span></div>
<div class="cols">
<div class="stack">
{
        panel(
            "What this retry does",
            '''
<dl class="kv">
<dt>starts at</dt><dd>the beginning: the engineer runs again, then every gate after it</dd>
<dt>resets</dt><dd>client-commands to pending</dd>
<dt>stays out</dt><dd>nothing else is failed or skipped</dd>
<dt>worktree</dt><dd>none recorded; nothing to remove</dd>
<dt>branch</dt><dd>deletes <span class="mono">kstrl/factory/client-commands</span>; the retry recreates it from main</dd>
<dt>keeps</dt><dd>the run records under .kstrl/runs, the debug directory, the learning journal</dd>
<dt>runs under</dt><dd>max cost $45 · 2 in parallel · agent timeout 900s · component timeout 2400s</dd>
<dt>not repeated</dt><dd>the recorded verify command option is no longer used, and the command it named never ran. Verification now runs the test, typecheck and lint commands.</dd>
<dt>based on</dt><dd>the manifest as it is now, and the launch settings the failed run saved</dd>
<dt>will run as</dt><dd><span class="mono">ks retry client-commands --max-parallel 2</span> · a child process of this page's server; the new run appears under Active</dd>
</dl>
<div class="toolbar" style="margin:16px 0 0"><a class="btn primary" href="run-board.html">Start retry</a><a class="btn" href="failures.html">Cancel</a></div>
''',
            right="read it before you start",
        )
    }
</div>
<div class="stack dimmed">
{
        panel(
            "Evidence",
            '<p><b>verify failed on attempt 2</b> · Tests failed (exit code 1)</p><div class="path">.kstrl/debug/factory-20260926-171433.450564-fail01/client-commands/attempt-2/test_suite.log</div>',
        )
    }
</div>
</div>"""
    m = mast(clock="1:03:42", readout=spend("$19.24", "$78.00", 25), compact="$19.24 of $78 · 25%")
    page("retry-confirm.html", "Retry confirmation", m, "failures", body)


def build_retry_blocked() -> None:
    body = f"""
<div class="page-title"><h1>Failures</h1><span class="sub">e3root · 1 current · 0 superseded</span></div>
{
        panel(
            "Failure queue",
            f'''<table class="data"><thead><tr><th>Run</th><th>Component</th><th>Cause</th><th class="num">Tries</th><th>Failed</th><th>Recovery</th></tr></thead>
<tbody><tr class="sel"><td class="mono">fda682</td><td class="mono">integration-fix-1</td><td>review: Review failed</td><td class="num">2</td><td class="nowrap">09-25 23:21</td><td>{lamp("nogo", "retry blocked")}</td></tr></tbody></table>''',
            flush=True,
        )
    }
<div style="height:20px"></div>
<div class="cols">
<div class="stack">
{
        panel(
            "Retry blocked",
            '''
<div class="note bad" style="margin-bottom:12px"><b>This project moved or was copied after the run.</b> The launch record names a manifest at another path, so the recorded settings cannot be trusted to belong to this project, and <span class="mono">ks retry</span> refuses the record too.</div>
<p><b>What you can do</b></p>
<ol style="margin:0 0 12px 18px;padding:0">
<li>Retry from the original project location, where the launch record matches.</li>
<li>Or delete the launch record and retry without the recorded settings: <span class="mono">rm .kstrl/runs/factory-20260925-214816.991354-fda682/launch.json</span>, then retry. The run limits will come from kstrl.toml instead of the failed run.</li>
</ol>
<details><summary>The three paths in full</summary>
<dl class="kv" style="margin-top:10px">
<dt>launch record</dt><dd class="path">/private/tmp/claude-501/-Users-wumpinihussein-Documents-code-ralph/66842a13-086d-472b-b1a0-f74f7ad69d73/scratchpad/ui/round6-before/_work/retry-moved/e3root/.kstrl/runs/factory-20260925-214816.991354-fda682/launch.json</dd>
<dt>it names</dt><dd class="path">/private/tmp/claude-501/-Users-wumpinihussein-Documents-code-ralph/66842a13-086d-472b-b1a0-f74f7ad69d73/scratchpad/replay/work/e3-slice1-r1/run-2/root/scripts/kstrl/manifest.json</dd>
<dt>this project's</dt><dd class="path">/private/tmp/claude-501/-Users-wumpinihussein-Documents-code-ralph/66842a13-086d-472b-b1a0-f74f7ad69d73/scratchpad/ui/round6-before/_work/retry-moved/e3root/scripts/kstrl/manifest.json</dd>
</dl></details>
''',
            right="cause first, paths on request",
        )
    }
</div>
<div class="stack">
{
        panel(
            "Evidence",
            '<p><b>review failed on attempt 2</b> · Review failed · 1 blocking finding at src/snippetvault/snippets.py:121-124</p><div class="toolbar" style="margin:10px 0 0"><a class="btn sm" href="integration.html">Integration review</a><a class="btn sm" href="#">Review transcript</a></div>',
        )
    }
</div></div>"""
    m = mast(project="e3root", serve="ks serve not running")
    page("retry-blocked.html", "Retry blocked", m, "failures", body, needs=1, failures=1)


# ---------------------------------------------------------------- inbox ----


def inbox_list(selected: str) -> str:
    rows = [
        (
            "merge",
            "park",
            "merge gate",
            "client-commands is waiting for merge approval",
            "run 8d80e8",
            "2s",
        ),
        (
            "halted",
            "hold",
            "halted run",
            "The integration loop stopped without a clean verdict",
            "run fda682",
            "2s",
        ),
    ]
    out = ""
    for key, k, kind, title, run, age in rows:
        sel = ' class="sel"' if key == selected else ""
        href = "inbox.html" if key == "merge" else "inbox-halted.html"
        out += f'<tr{sel}><td>{lamp(k, kind)}</td><td class="wrap"><a href="{href}">{escape(title)}</a></td><td class="muted nowrap">{run}</td><td class="muted num">{age}</td></tr>'
    return f'<table class="data stack-sm"><thead><tr><th>Kind</th><th>Decision</th><th>Run</th><th class="num">Age</th></tr></thead><tbody>{out}</tbody></table>'


def build_inbox() -> None:
    detail = f"""
<p class="muted">merge gate · normal priority · raised 2s ago · component <span class="mono">client-commands</span> · run <a href="run-board.html" class="mono">8d80e8</a></p>
<p>Merge approval is required and no prompt was available to ask for it, so nothing was pushed and no PR was opened. The branch holds the reviewed work.</p>
<div class="poll" style="margin-top:12px;border:1px solid var(--rule-soft);border-radius:4px">
<div class="station"><span class="name">branch</span><span class="mono">kstrl/factory/client-commands</span></div>
<div class="station"><span class="name">branch head</span><span><span class="mono">87c3e2efbe2c</span> <span class="muted">· approval applies to this commit only</span></span></div>
<div class="station"><span class="name">verify</span><span>{lamp("go", "passed")}</span></div>
<div class="station"><span class="name">review</span><span>{lamp("go", "passed")} <span class="muted">· 0 blocking · 2 advisory · <a href="#">read them</a></span></span></div>
<div class="station"><span class="name">security</span><span>{lamp("go", "passed")}</span></div>
<div class="station"><span class="name">pull request</span><span class="muted">none yet: a park happens before any push</span></div>
</div>"""
    choices = """
<div class="choices">
<div class="choice"><div><a class="btn primary" href="#">Approve and run</a></div><div class="does"><b>Records your approval and starts a factory run now.</b> That run pushes <span class="mono">kstrl/factory/client-commands</span> and merges it if its head is still <span class="mono">87c3e2efbe2c</span>; if the branch moved, client-commands fails and nothing is pushed. Same as <span class="mono">ks inbox approve</span> from a shell.</div></div>
<div class="choice"><div><a class="btn" href="#">Approve only</a></div><div class="does">Records your approval and nothing else. Nothing merges until the next factory run, which then behaves as above.</div></div>
<div class="choice"><div><a class="btn danger" href="#">Reject with a reason</a></div><div class="does">Records your rejection and your reason. The next factory run marks client-commands failed as rejected by a person and skips its dependents. Nothing is pushed.</div></div>
<div class="choice"><div><a class="btn quiet" href="#">Snooze 24 hours</a></div><div class="does">Hides this item for 24 hours; it returns to this list after that. client-commands stays parked, and ks serve admits no new work while a merge is parked.</div></div>
</div>"""
    body = f"""
<div class="page-title"><h1>Decisions</h1><span class="sub">2 open · <a href="#">show decided</a></span></div>
{panel("Waiting on you", inbox_list("merge"), n="2", flush=True)}
<div style="height:20px"></div>
<h2 style="font-size:16px;line-height:24px;margin:0 0 12px">client-commands is waiting for merge approval</h2>
<div class="cols-2">
  <div class="stack">{panel("Before you decide", detail, right="what is known about this item")}</div>
  <div class="stack">{panel("Your decision", choices, right="each choice says what it does", flush=True)}</div>
</div>"""
    m = mast(clock="1:03:42", readout=spend("$19.24", "$78.00", 25), compact="$19.24 of $78 · 25%")
    page("inbox.html", "Decisions", m, "decisions", body)


def build_inbox_halted() -> None:
    detail = f"""
<p class="muted">halted run · normal priority · raised 2s ago · run <a href="integration.html" class="mono">fda682</a> · e3root</p>
<p>integration-fix-1 failed review; the fix budget (1) is spent. The run stopped rather than merging with open findings.</p>
<div class="poll" style="margin-top:12px;border:1px solid var(--rule-soft);border-radius:4px">
<div class="station"><span class="name">open findings</span><span><span class="mono">IF-1</span>, <span class="mono">IF-2</span>, <span class="mono">IF-4</span> · <a href="integration.html">integration review</a></span></div>
<div class="station"><span class="name">last fix</span><span>{lamp("nogo", "integration-fix-1 failed review")} <span class="muted">· 2 attempts</span></span></div>
<div class="station"><span class="name">evidence</span><span class="mono">.kstrl/integration/state.json</span></div>
<div class="station"><span class="name">merged</span><span class="muted">nothing was merged in this run</span></div>
</div>"""
    choices = """
<div class="choices">
<div class="choice"><div><a class="btn primary" href="#">Close as noted</a></div><div class="does"><b>Closes this item.</b> No factory step reads a halted-run decision, so nothing else changes. To fix the findings, start a new run; it carries the open findings forward.</div></div>
<div class="choice"><div><a class="btn danger" href="#">Close with a reason</a></div><div class="does">Closes this item and records your reason with it. No factory step reads it; the reason is for the record.</div></div>
<div class="choice"><div><a class="btn quiet" href="#">Snooze 24 hours</a></div><div class="does">Hides this item for 24 hours; it returns after that. Nothing else changes.</div></div>
</div>"""
    body = f"""
<div class="page-title"><h1>Decisions</h1><span class="sub">2 open · <a href="#">show decided</a></span></div>
{panel("Waiting on you", inbox_list("halted"), n="2", flush=True)}
<div style="height:20px"></div>
<h2 style="font-size:16px;line-height:24px;margin:0 0 12px">The integration loop stopped without a clean verdict</h2>
<div class="cols-2">
  <div class="stack">{panel("Before you decide", detail, right="what is known about this item")}</div>
  <div class="stack">{panel("Your decision", choices, right="each choice says what it does", flush=True)}</div>
</div>"""
    m = mast(clock="1:03:42", readout=spend("$19.24", "$78.00", 25), compact="$19.24 of $78 · 25%")
    page("inbox-halted.html", "Decisions: halted run", m, "decisions", body)


def build_inbox_empty() -> None:
    body = f"""
<div class="page-title"><h1>Decisions</h1><span class="sub">0 open · <a href="#">show decided</a></span></div>
{panel("Waiting on you", '<div class="empty">Nothing is waiting on you. Merge gates, halted runs, budget stops and autonomy demotions appear here when they need a person.</div>', n="0", flush=True)}"""
    m = mast(serve="ks serve not running")
    page("inbox-empty.html", "Decisions (none)", m, "decisions", body, needs=0, failures=0)


# ----------------------------------------------------------- checkpoint ----


def build_checkpoint() -> None:
    stations = f"""
<div class="poll">
<div class="station"><span class="name">branch</span><span class="mono">kstrl/factory/comp-c</span></div>
<div class="station"><span class="name">verify</span><span>{lamp("go", "passed")} <span class="muted">· tests, typecheck, lint, diff scope</span></span></div>
<div class="station"><span class="name">review</span><span>{lamp("go", "passed")} <span class="muted">· 0 blocking · 2 advisory, listed below</span></span></div>
<div class="station"><span class="name">security</span><span>{lamp("go", "passed")} <span class="muted">· 1 low, listed below</span></span></div>
<div class="station"><span class="name">changed files</span><span>3 · <span class="mono">src/snippetvault/cli.py</span> +201, <span class="mono">src/snippetvault/__init__.py</span> +8 -2, <span class="mono">src/snippetvault/__main__.py</span> +5</span></div>
<div class="station"><span class="name">spend so far</span><span>{spend("$4.50", None, None, lower_bound=True)} <span class="muted">· some calls did not report a cost, so this is a lower bound</span></span></div>
</div>"""
    findings = """
<table class="data">
<thead><tr><th>Phase</th><th>Severity</th><th>Where</th><th>Finding</th></tr></thead>
<tbody>
<tr><td>review</td><td><span class="sev advisory">advisory</span></td><td class="mono nowrap">cli.py:621-634</td><td class="wrap">A connection refused error prints a traceback instead of the exit-2 message the PRD names.</td></tr>
<tr><td>review</td><td><span class="sev advisory">advisory</span></td><td class="mono nowrap">test_cli_client.py:1923-1929</td><td class="wrap">The test asserts the exit code only; the stderr text the criterion pins is never checked.</td></tr>
<tr><td>security</td><td><span class="sev low">low</span></td><td class="mono nowrap">cli.py:419-429</td><td class="wrap">The --host value reaches the URL unescaped; a crafted host can add a path segment.</td></tr>
</tbody></table>"""
    diff = """<pre><span class="h">src/snippetvault/__init__.py</span>
<span class="hunk">@@ -1,2 +1,8 @@</span>
<span class="del">-def main() -> None:
-    print("snippetvault")</span>
<span class="add">+&quot;&quot;&quot;snippetvault: a private snippet server and its client.&quot;&quot;&quot;
+
+from __future__ import annotations
+
+from snippetvault.cli import main
+
+__all__ = ["main"]</span>
<span class="h">src/snippetvault/__main__.py</span>
<span class="hunk">@@ -0,0 +1,5 @@</span>
<span class="add">+from __future__ import annotations
+
+from snippetvault.cli import main
+
+raise SystemExit(main())</span>
<span class="h">src/snippetvault/cli.py</span>
<span class="hunk">@@ -0,0 +1,201 @@</span>
<span class="m">... 201 added lines · <a href="#">open the whole diff</a></span></pre>"""
    choices = """
<div class="choices">
<div class="choice"><div><a class="btn primary" href="#">Approve</a></div><div class="does"><b>Pushes <span class="mono">kstrl/factory/comp-c</span>, opens its PR and merges it.</b> comp-c completes once the merge is confirmed; without gh it stays unpushed and the run says so.</div></div>
<div class="choice"><div><a class="btn danger" href="#">Reject</a></div><div class="does">comp-c fails and its dependents are skipped. Nothing is pushed. The branch is kept for you to read.</div></div>
<div class="choice"><div><a class="btn" href="#">Send back to the engineer</a></div><div class="does">The engineer runs comp-c again with a note that a human reviewer asked for changes; no reason is passed on. Uses one retry; with none left, comp-c fails as on Reject.</div></div>
<div class="choice"><div><a class="btn quiet" href="#">Decide later</a></div><div class="does">Leaves the question open. The run waits at this point; the item stays under Needs you and the elapsed clock keeps running.</div></div>
</div>"""
    body = f"""
<div class="page-title"><h1>Approve PR creation and merge for comp-c?</h1><span class="sub">factory checkpoint · the run is waiting on this answer · asked 2s ago</span></div>
<div class="cols-2">
  <div class="stack">{panel("Checks before this point", stations, right="every gate that ran on this component", flush=True)}</div>
  <div class="stack">{panel("Your decision", choices, right="each choice says what it does", flush=True)}</div>
</div>
<div style="height:20px"></div>
<div class="cols-2">
  <div class="stack">{panel("Findings", findings, n="3", flush=True)}</div>
  <div class="stack">{panel("Diff", diff, right="3 files · +214 -2", glass=True)}</div>
</div>"""
    m = mast(
        title="factory checkpoint",
        clock="0:02",
        readout=spend("$4.50", None, None, lower_bound=True),
        compact="at least $4.50 · no cap",
    )
    page("checkpoint.html", "Checkpoint decision", m, "runs", body, needs=1, failures=0)
