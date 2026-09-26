"""Mock-ups: home."""

# ruff: noqa: E501
from __future__ import annotations

from html import escape

from mock_shell import lamp, mast, page, panel, spend

# ---------------------------------------------------------------- home ----


def build_home() -> None:
    needs = f"""
<div class="strips">
  <div class="strip sel">
    {lamp("park")}
    <div><div class="l1">client-commands is waiting for merge approval</div>
    <div class="l2">merge gate · run 8d80e8 · raised 2s ago · the branch holds reviewed work; nothing is pushed until you decide</div></div>
    <div class="act"><a class="btn primary" href="inbox.html">Decide</a></div>
  </div>
  <div class="strip">
    {lamp("hold")}
    <div><div class="l1">The integration loop stopped without a clean verdict</div>
    <div class="l2">halted run · run fda682 · raised 2s ago · integration-fix-1 failed review and the fix budget (1) is spent</div></div>
    <div class="act"><a class="btn" href="inbox-halted.html">Decide</a></div>
  </div>
  <div class="strip">
    {lamp("nogo")}
    <div><div class="l1">client-commands failed at verify: Tests failed (exit code 1)</div>
    <div class="l2">run fail01 · 2 attempts · failed 17:14 today · a retry is available and its scope is known</div></div>
    <div class="act"><a class="btn" href="failures.html">Review retry</a></div>
  </div>
</div>"""

    active = f"""
<div class="strips">
  <div class="strip">
    {lamp("run")}
    <div><div class="l1"><a href="run-board.html">factory live01</a> · running · 3 of 6 components</div>
    <div class="l2">http-app · engineer · iteration 3 of 10 · last output 8s ago · worker alive, checked 2s ago · elapsed 1:03:42</div>
    <div class="l2" style="margin-top:4px">{spend("$19.24", "$78.00", 25)}</div></div>
    <div class="act"><a class="btn" href="run-board.html">Open board</a></div>
  </div>
  <div class="strip">
    {lamp("run")}
    <div><div class="l1"><a href="serve.html">ks serve</a> · running · snippetvault slice 3: export and import</div>
    <div class="l2">runs live01 above · last output 8s ago · daemon alive, checked 2s ago</div></div>
    <div class="act"><a class="btn quiet" href="serve.html">Queue</a></div>
  </div>
  <div class="strip">
    {lamp("off")}
    <div><div class="l1">ks serve · queued 1st · snippetvault slice 4: sharing</div>
    <div class="l2">starts when live01 finishes and every admission check passes</div></div>
    <div class="act"></div>
  </div>
</div>"""

    delivery = f"""
<table class="data stack-sm">
<thead><tr><th>Merged to main</th><th>Commit</th><th>CI on that commit</th></tr></thead>
<tbody>
<tr><td colspan="3" class="full"><b>main is at cea97b4</b> · {lamp("hold", "CI unknown")} <span class="muted">· gh api failed (4): HTTP 401: Bad credentials · read 2s ago</span></td></tr>
<tr><td colspan="3" class="muted full">run live01 · running · integration: no review recorded for this run</td></tr>
<tr><td class="nowrap">PR #3 storage</td><td class="mono">cea97b4</td><td class="wrap full">{lamp("hold", "unknown")}<div class="muted">gh api failed (4): HTTP 401: Bad credentials · read 2s ago</div></td></tr>
<tr><td class="nowrap">PR #2 snippet-rules</td><td class="mono">23dd9ac</td><td class="wrap full">{lamp("nogo", "failed")}<div class="muted">check "test" failed · read 2s ago</div></td></tr>
<tr><td class="nowrap">PR #1 token-crypto</td><td class="mono">4d74d12</td><td class="wrap full">{lamp("go", "passed")}<div class="muted">7 checks passed · read 2s ago</div></td></tr>
<tr><td colspan="3" class="muted full">run 8d80e8 · completed 2d ago · release ref 4c4706b · integration: no review recorded for this run</td></tr>
<tr><td class="nowrap">PR #9 client-commands</td><td class="mono">4c4706b</td><td class="wrap full">{lamp("off", "not read yet")}<div class="muted">ks serve refreshes CI on its own · <a href="#">read now</a></div></td></tr>
<tr><td class="nowrap">PR #8 client-http</td><td class="mono">4ab99ae</td><td class="wrap full">{lamp("go", "passed")}<div class="muted">7 checks passed · read 0s ago</div></td></tr>
</tbody></table>"""

    hist_rows = [
        (
            "run",
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
            "nogo",
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
            "go",
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
            "hold",
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
            "nogo",
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
            "go",
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
            "hold",
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
            "hold",
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
            "go",
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
    rows = ""
    for k, rid, kind, st, age, comps, tok, cost, note in hist_rows:
        sel = ' class="sel"' if rid == "1490e8" else ""
        n = f'<span class="muted">{escape(note)}</span>' if note else ""
        rows += (
            f'<tr{sel}><td class="nowrap"><a href="run-board.html" class="mono">{rid}</a></td><td>{kind}</td>'
            f"<td>{lamp(k, st)}</td><td class='muted num'>{age}</td><td class='num'>{escape(comps)}</td>"
            f"<td class='num'>{tok}</td><td class='num'>{escape(cost)}</td><td class='wrap'>{n}</td></tr>"
        )
    history = f"""
<table class="data stack-sm">
<thead><tr><th>Run</th><th>Kind</th><th>State</th><th class="num">Last event</th><th class="num">Components</th><th class="num">Tokens</th><th class="num">Cost</th><th>Note</th></tr></thead>
<tbody>{rows}</tbody></table>
<div class="note" style="margin:12px 14px 14px">
<b>1490e8 · unknown</b> <span class="muted">(no finish record; last event 2d ago)</span><br>
Refusing to run: stale component branches found: branch <span class="mono">kstrl/factory/client-http</span> (component client-http) already exists with commits not merged into <span class="mono">main</span>; refusing to silently reuse it. Merge it or delete it (<span class="mono">git branch -D kstrl/factory/client-http</span>) and re-run.
</div>"""

    body = f"""
<div class="page-title"><h1>snippetvault</h1><span class="sub">three things need you · one run live · ks serve running</span></div>
<div class="cols">
  <div class="stack">
    {panel("Needs you", needs, n="3", right='<a href="inbox.html">2 decisions</a> · <a href="failures.html">1 failure</a>', flush=True)}
    {panel("Active", active, n="3", flush=True)}
  </div>
  <div class="stack">
    {panel("Delivery", delivery, right="is main green after the merges", flush=True)}
  </div>
</div>
<div style="height:20px"></div>
{panel("History", history, n="9 runs", right="percentages round up · select a row to read its whole note", flush=True)}
"""
    m = mast(clock="1:03:42", readout=spend("$19.24", "$78.00", 25), compact="$19.24 of $78 · 25%")
    page("home.html", "Home", m, "home", body)


def build_home_empty() -> None:
    body = f"""
<div class="page-title"><h1>snippetvault</h1><span class="sub">no runs yet</span></div>
<div class="cols">
  <div class="stack">
    {panel("Needs you", '<div class="empty">Nothing is waiting on you.</div>', n="0", flush=True)}
    {
        panel(
            "Active",
            '<div class="empty">Nothing is running. <b>ks serve</b> is not running either.</div>',
            n="0",
            flush=True,
        )
    }
    {
        panel(
            "History",
            '<div class="empty">No runs recorded under <span class="mono">.kstrl/runs</span>. The first run appears here as soon as it writes its first event.</div>',
            flush=True,
        )
    }
  </div>
  <div class="stack">
    {
        panel(
            "Start here",
            '''
<p>The project is initialised: <a href="config.html">kstrl.toml is valid</a>, the test, typecheck and lint commands resolve, and the agent is auto-detected.</p>
<ol style="margin:8px 0 12px 18px;padding:0">
<li><b>Decompose the spec.</b> The architect reads <span class="mono">spec.md</span>, red-teams it, and writes a manifest of components.</li>
<li><b>Run the factory</b> over that manifest. Each component is built, verified, reviewed and merged on its own branch.</li>
<li>Or <b>hand it to ks serve</b> and let the queue drain unattended.</li>
</ol>
<a class="btn primary" href="start.html">Decompose spec.md</a> <a class="btn" href="start.html">Start a factory run</a>
''',
        )
    }
    {
        panel(
            "Delivery",
            '<div class="empty">No merges yet, so nothing to check on main.</div>',
            flush=True,
        )
    }
  </div>
</div>"""
    m = mast(serve="ks serve not running")
    page("home-empty.html", "Home (first run)", m, "home", body, needs=0, failures=0)


def build_error_config() -> None:
    body = f"""
<div class="page-title"><h1>snippetvault</h1><span class="sub">configuration could not be read</span></div>
<div class="note bad" style="margin-bottom:20px">
<b>kstrl.toml could not be read, so nothing here can run.</b><br>
<span class="mono">kstrl.toml:41: Invalid value (at line 41, column 16)</span><br>
Every command that reads the configuration refuses before it spends anything, and this page shows only what it can read from the run records. Fix the line, then <a href="#">check again</a>.
</div>
<div class="cols">
  <div class="stack">
    {
        panel(
            "Needs you",
            '<div class="empty">The inbox cannot be read while the configuration is broken: its location comes from the configuration.</div>',
            flush=True,
        )
    }
    {
        panel(
            "History",
            '<div class="empty">9 runs recorded. Their records are readable and open from <a href="home.html">Home</a> once the configuration is repaired.</div>',
            flush=True,
        )
    }
  </div>
  <div class="stack">
    {
        panel(
            "What kstrl read",
            '''
<dl class="kv">
<dt>file</dt><dd><span class="path">/Users/wumpinihussein/Documents/code/kstrl dogfood/snippetvault/kstrl.toml</span></dd>
<dt>line 41</dt><dd><span class="mono">max_cost_usd = 45.0.0</span></dd>
<dt>parser said</dt><dd><span class="mono">Invalid value (at line 41, column 16)</span></dd>
<dt>what refuses</dt><dd>every command except <span class="mono">ks init</span>, <span class="mono">ks doctor</span> and <span class="mono">ks --version</span></dd>
</dl>''',
        )
    }
  </div>
</div>"""
    m = mast(config="kstrl.toml unreadable", safe="safe mode not checked", serve="ks serve unknown")
    page("error-config.html", "Configuration unreadable", m, "home", body, needs=0, failures=0)
