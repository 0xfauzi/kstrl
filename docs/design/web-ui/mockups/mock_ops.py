"""Mock-ups: config, learning, launchers, serve."""

# ruff: noqa: E501
from __future__ import annotations

from html import escape

from mock_shell import lamp, mast, page, panel, spend

# --------------------------------------------------------------- config ----


def build_config() -> None:
    rows = [
        ("agent", "type", "Agent", "auto-detect", "default"),
        ("agent", "command", "Agent command", "unset", "default"),
        ("agent", "model", "Model", "agent default", "default"),
        ("agent", "reasoning_effort", "Reasoning effort", "agent default", "default"),
        ("run", "max_iterations", "Max iterations per component", "10", "default"),
        ("run", "sleep_seconds", "Pause between iterations", "2.0 s", "default"),
        ("run", "interactive", "Interactive prompts", "no", "default"),
        ("paths", "prompt", "Engineer prompt", "scripts/kstrl/prompt.md", "default"),
        ("paths", "prd", "PRD", "scripts/kstrl/prd.json", "default"),
        (
            "paths",
            "progress",
            "Progress log",
            "unset: each component writes beside its own PRD",
            "default",
        ),
        ("paths", "allowed", "Allowed paths", "none", "default"),
        ("factory", "max_parallel", "Components in parallel", "2", "kstrl.toml"),
        ("factory", "max_cost_usd", "Cost cap per run", "$45.00", "kstrl.toml"),
        ("factory", "max_retries", "Retries per component", "3", "default"),
        ("factory", "review_mode", "Review mode", "hard", "default"),
        ("factory", "pause_before_pr_merge", "Merge gate", "off", "default"),
        ("security", "mode", "Security review", "hard", "kstrl.toml"),
        ("security", "fail_threshold", "Security fails at", "high", "kstrl.toml"),
        (
            "serve",
            "daily_budget_usd",
            "Daily budget for ks serve",
            "none: the run cap bounds spend",
            "kstrl.toml",
        ),
        ("serve", "factory_timeout_seconds", "Serve run timeout", "10800 s (3h)", "kstrl.toml"),
        (
            "intake_github",
            "repo",
            "Intake repository",
            "0xfauzi/kstrl-dogfood-snippetvault",
            "kstrl.toml",
        ),
    ]
    out = ""
    for sec, key, label, val, src in rows:
        sel = ' class="sel"' if key == "progress" else ""
        s = f"<b>{src}</b>" if src != "default" else f'<span class="muted">{src}</span>'
        out += f'<tr{sel}><td>{escape(label)}</td><td class="mono muted nowrap">{sec}.{key}</td><td class="wrap">{escape(val)}</td><td>{s}</td></tr>'
    table = f'<table class="data"><thead><tr><th>Setting</th><th>Key in kstrl.toml</th><th>Resolved value</th><th>Source</th></tr></thead><tbody>{out}<tr><td colspan="4" class="muted">95 more · scroll or filter</td></tr></tbody></table>'
    detail = """
<p><b>Progress log</b> <span class="mono muted">paths.progress</span></p>
<p>unset</p>
<p class="muted">Each component's engineer writes its progress beside its own PRD, inside that component's allowed paths. Setting this key forces one path on every factory component.</p>
<dl class="kv" style="margin-top:10px">
<dt>source</dt><dd>built-in default</dd>
<dt>precedence</dt><dd>command flag, then environment <span class="mono">KSTRL_PATHS_PROGRESS</span>, then kstrl.toml, then this default</dd>
<dt>applies to</dt><dd>new runs only</dd>
</dl>"""
    body = f"""
<div class="page-title"><h1>Config</h1><span class="sub">resolved values and their sources · 116 values · 10 set in kstrl.toml, 106 defaults</span></div>
<div class="toolbar"><input type="text" placeholder="filter by setting, key, value or source" style="max-width:320px"><span class="spacer"></span><span class="path">/Users/wumpinihussein/Documents/code/kstrl dogfood/snippetvault/kstrl.toml</span></div>
<div class="cols">
  <div class="stack">{panel("Resolved configuration", table, flush=True)}</div>
  <div class="stack">{panel("Selected", detail)}</div>
</div>"""
    m = mast()
    page("config.html", "Config", m, "config", body)


# ------------------------------------------------------------- learning ----


def build_learning() -> None:
    readiness = """
<dl class="kv" style="grid-template-columns:220px minmax(0,1fr)">
<dt>knowledge facts</dt><dd>measured on 9 of 10 components; 0 referenced by later components, in 0 runs</dd>
<dt>review and security concerns</dt><dd>on 9 of 10 components · claim disagreement 6 · copy paste 2 · dead code 2 · denial of service 1 · error handling 8 · information disclosure 4 · injection 1 · other 16 · PRD criterion 6 · scope creep 2 · security concern 5 · test quality 13</dd>
<dt>distill replies that did not parse</dt><dd>0 of 9 in the last 7 runs</dd>
</dl>"""
    trends = """
<table class="data"><thead><tr><th>Run</th><th class="num">Done</th><th class="num">Failed</th><th class="num">Retry rate</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead>
<tbody>
<tr><td class="mono">4965fb</td><td class="num">6</td><td class="num">0</td><td class="num">0.83</td><td class="num">35.60M</td><td class="num">$42.35</td></tr>
<tr><td class="mono">7ad3ae</td><td class="num">0</td><td class="num">1</td><td class="num muted">·</td><td class="num">8.02M</td><td class="num">$10.37</td></tr>
<tr><td class="mono">8d80e8</td><td class="num">2</td><td class="num">0</td><td class="num muted">·</td><td class="num">18.51M</td><td class="num">$19.30</td></tr>
</tbody></table>
<p class="muted" style="padding:10px 14px 0">Retry rate is retries per completed component; a dot means no retry was recorded.</p>"""
    body = f"""
<div class="page-title"><h1>Learning</h1><span class="sub">failure patterns and trends across the last 10 runs</span></div>
<div class="cols">
  <div class="stack">
    {panel("Recurring failure patterns", '<div class="empty">No failure pattern recurred across the last 10 runs. A pattern is the same gate failing for the same reason in 2 or more runs.</div>', flush=True)}
    {panel("Trends", trends, n="3 finished factory runs", flush=True)}
  </div>
  <div class="stack">{panel("Learning readiness", readiness, right="what the next run can learn from")}</div>
</div>"""
    m = mast()
    page("learning.html", "Learning", m, "learning", body)


# ------------------------------------------------------------ launchers ----


def build_start() -> None:
    tabs = '<div class="tabs"><a class="on" href="start.html">Factory</a><a href="#">Decompose</a><a href="#">Feature</a><a href="#">Understand</a><a href="start-init.html">Initialise</a></div>'
    form = """
<div class="field"><label>Manifest</label><div><input type="text" value="scripts/kstrl/manifest.json"><div class="hint">found · 6 components · 6 completed in run 4965fb · a new run rebuilds only pending components</div></div></div>
<div class="field"><label>Spec</label><div><input type="text" placeholder="spec.md" ><div class="hint">optional: decomposes the spec first if the manifest is stale</div></div></div>
<div class="field"><label>Components in parallel</label><div><input type="text" placeholder="2 (from kstrl.toml)"><div class="hint">unset resolves environment, then kstrl.toml, then the default</div></div></div>
<div class="field"><label>Review mode</label><div><select><option>hard (from kstrl.toml)</option><option>advisory</option><option>skip</option></select></div></div>
<div class="field"><label>Cost cap</label><div><input type="text" placeholder="$45.00 (from kstrl.toml)"><div class="hint">the run halts at the cap; a cap kstrl cannot read is refused, never read as no cap</div></div></div>
<div class="field"><label>Merge gate</label><div><label class="toggle"><input type="checkbox"> pause before each PR merge and ask me here</label></div></div>
<div class="toolbar" style="margin:16px 0 0"><a class="btn primary" href="run-board.html">Start factory</a><span class="muted">runs <span class="mono">ks factory --max-parallel 2</span> as a child process; the board opens when the first event lands</span></div>"""
    checks = f"""
<dl class="kv">
<dt>configuration</dt><dd>{lamp("go", "kstrl.toml valid")}</dd>
<dt>agent</dt><dd>{lamp("go", "agent auto-detected on PATH")}</dd>
<dt>gates</dt><dd>test <span class="mono">uv run pytest</span> · typecheck <span class="mono">uv run mypy</span> · lint <span class="mono">uv run ruff check</span></dd>
<dt>factory lock</dt><dd>{lamp("nogo", "held by run live01")} <span class="muted">· a second factory run in this project waits until it finishes</span></dd>
<dt>stale branches</dt><dd>{lamp("go", "none")}</dd>
</dl>"""
    body = f"""
<div class="page-title"><h1>Start a run</h1><span class="sub">everything unset resolves environment, then kstrl.toml, then defaults</span></div>
{tabs}
<div class="cols">
  <div class="stack">{panel("Factory", form)}</div>
  <div class="stack">{panel("Preflight", checks, right="checked before anything is spent")}
  {panel("What a factory run does", "<p>Builds each pending component of the manifest on its own branch: engineer, verify, diff scope, review, security, distill, then a pull request merged to main. Tiers run in dependency order; components in a tier run in parallel up to the limit.</p>")}</div>
</div>"""
    m = mast(clock="1:03:42", readout=spend("$19.24", "$78.00", 25), compact="$19.24 of $78 · 25%")
    page("start.html", "Start a run", m, "home", body)


def build_start_init() -> None:
    tabs = '<div class="tabs"><a href="start.html">Factory</a><a href="#">Decompose</a><a href="#">Feature</a><a href="#">Understand</a><a class="on" href="start-init.html">Initialise</a></div>'
    form = """
<div class="field"><label>Project</label><div><span class="path">/Users/wumpinihussein/Documents/code/newproject</span></div></div>
<div class="field"><label>Detected</label><div>Python · uv · git repository on <span class="mono">main</span></div></div>
<div class="field"><label>Gates that will run</label><div>test <span class="mono">uv run pytest</span> · typecheck <span class="mono">uv run mypy</span> · lint <span class="mono">uv run ruff check</span><div class="hint">resolved by the gate itself, so this is what will actually run</div></div></div>
<div class="field"><label>Agent</label><div><select><option>auto-detect</option><option>claude-code</option><option>claude-sdk</option><option>codex</option></select></div></div>
<div class="field"><label>Model</label><div><input type="text" placeholder="agent default"></div></div>
<div class="field"><label>Reasoning effort</label><div><select><option>agent default</option><option>low</option><option>medium</option><option>high</option><option>max</option></select></div></div>"""
    preview = """
<table class="data"><thead><tr><th>File</th><th>Action</th></tr></thead><tbody>
<tr><td class="mono">kstrl.toml</td><td>create · every key commented out with its default</td></tr>
<tr><td class="mono">scripts/kstrl/prompt.md</td><td>create · the engineer prompt</td></tr>
<tr><td class="mono">scripts/kstrl/prd.json</td><td>create · empty PRD</td></tr>
<tr><td class="mono">CLAUDE.md</td><td>keep · exists, not touched</td></tr>
<tr><td class="mono">AGENTS.md</td><td>create</td></tr>
<tr><td class="mono">.gitignore</td><td>append · <span class="mono">.kstrl/</span></td></tr>
</tbody></table>
<div class="toolbar" style="margin:14px 14px 14px"><a class="btn primary" href="#">Write these files</a><span class="muted">an existing file is never overwritten</span></div>"""
    body = f"""
<div class="page-title"><h1>Initialise a project</h1><span class="sub">detect, preview, then scaffold</span></div>
{tabs}
<div class="cols">
  <div class="stack">{panel("Project", form)}</div>
  <div class="stack">{panel("Files it will write", preview, flush=True)}</div>
</div>"""
    m = mast(
        project="newproject",
        config="no kstrl.toml yet",
        safe="safe mode not applicable",
        serve="ks serve not running",
    )
    page("start-init.html", "Initialise a project", m, "home", body, needs=0, failures=0)


# ---------------------------------------------------------------- serve ----


def build_serve() -> None:
    items = f"""
<table class="data">
<thead><tr><th>State</th><th>Item</th><th>Title</th><th>Source</th><th>Run</th><th>Output</th></tr></thead>
<tbody>
<tr><td>{lamp("run", "running")}</td><td class="mono nowrap">q-6990de</td><td class="wrap">snippetvault slice 3: export and import</td><td class="muted">queue add</td><td><a href="run-board.html" class="mono">live01</a></td><td class="muted">8s ago</td></tr>
<tr><td>{lamp("off", "queued 1st")}</td><td class="mono nowrap">q-9f8809</td><td class="wrap">snippetvault slice 4: sharing</td><td class="muted">queue add</td><td class="muted">·</td><td class="muted">·</td></tr>
<tr><td>{lamp("nogo", "poisoned")}</td><td class="mono nowrap">q-793181</td><td class="wrap">issue #7 from the intake repository</td><td class="muted">github · 0xfauzi/kstrl-dogfood-snippetvault#7</td><td class="mono">7ad3ae</td><td class="muted">failed 09-23 21:32 after 1 attempt</td></tr>
</tbody></table>"""
    admission = f"""
<dl class="kv" style="grid-template-columns:190px minmax(0,1fr)">
<dt>daemon</dt><dd>{lamp("go", "running")} <span class="muted">· pid 46889 · holds the serve lock · started 16:07 today</span></dd>
<dt>daily budget</dt><dd>none set · each run is bounded by its own cost cap ($45.00)</dd>
<dt>parked merges</dt><dd>{lamp("park", "1 parked")} <span class="muted">· client-commands · no new work is admitted until it is <a href="inbox.html">decided</a></span></dd>
<dt>inbox cap</dt><dd>2 open of 50 · intake continues</dd>
<dt>poison breaker</dt><dd>1 consecutive poisoned item · trips at 3</dd>
<dt>CI refresh</dt><dd>reads the merge commits' CI on its own · last read 2s ago</dd>
<dt>intake</dt><dd>GitHub issues labelled <span class="mono">kstrl:queued</span> in <span class="mono">0xfauzi/kstrl-dogfood-snippetvault</span> · allowed actors: 0xfauzi</dd>
</dl>"""
    journal = """<div class="feed">
<span class="t">20:50:05</span><span><span class="who">q-793181</span> <span class="m">added by intake-github · github #7</span></span>
<span class="t">20:50:16</span><span><span class="who">q-793181</span> queued to leased</span>
<span class="t">20:50:16</span><span><span class="who">q-793181</span> leased to running · attempt 1</span>
<span class="t">20:50:19</span><span><span class="who">q-793181</span> lease adopted by the run process · pid 54443</span>
<span class="t">21:32:30</span><span><span class="who">q-793181</span> <span class="r">running to failed</span></span>
<span class="t">16:08:11</span><span><span class="who">q-6990de</span> queued to leased</span>
<span class="t">16:08:11</span><span><span class="who">q-6990de</span> leased to running · attempt 1 · run live01</span>
</div>"""
    body = f"""
<div class="page-title"><h1>Serve queue</h1><span class="sub">what ks serve is doing, read from the files it writes</span></div>
<div class="cols">
  <div class="stack">
    {panel("Queue", items, n="1 running · 1 queued · 1 poisoned", flush=True)}
    {panel("Journal", journal, right="newest last", glass=True)}
  </div>
  <div class="stack">{panel("Daemon and admission", admission, right="why the next item will or will not start")}</div>
</div>"""
    m = mast(clock="1:03:42", readout=spend("$19.24", "$78.00", 25), compact="$19.24 of $78 · 25%")
    page("serve.html", "Serve queue", m, "serve", body)


def build_safe_mode() -> None:
    body = f"""
<div class="page-title"><h1>Safe mode</h1><span class="sub">{
        lamp("go", "off")
    } · every signal is clear · checked 3s ago, rechecked every 5s</span></div>
<div class="cols">
  <div class="stack">{
        panel(
            "Signals",
            f'''
<div class="strips">
<div class="strip">{lamp("go")}<div><div class="l1">Control directory trusted</div><div class="l2">the state directory kstrl reads decisions from is the one it wrote</div></div><div class="act muted">runbook: control directory untrusted</div></div>
<div class="strip">{lamp("go")}<div><div class="l1">Autonomy at its earned level</div><div class="l2">no fallback or clamp is in force</div></div><div class="act muted">runbook: autonomy fell back or was clamped</div></div>
<div class="strip">{lamp("go")}<div><div class="l1">Queue running</div><div class="l2">ks serve is not paused</div></div><div class="act muted">runbook: queue paused</div></div>
<div class="strip">{lamp("go")}<div><div class="l1">Last finished factory run skipped no adversarial phase</div><div class="l2">review, security and distill all ran in 8d80e8</div></div><div class="act muted">runbook: an adversarial phase did not run</div></div>
</div>''',
            flush=True,
        )
    }</div>
  <div class="stack">{
        panel(
            "When a signal is not clear",
            "<p>The chip in the masthead turns to <b>safe mode: N reasons</b>, and this page lists each reason in the signal's own words with the runbook section that recovers it. The factory keeps running in the degraded state it reports; nothing here changes it.</p>",
        )
    }</div>
</div>"""
    m = mast()
    page("safe-mode.html", "Safe mode", m, "home", body)
