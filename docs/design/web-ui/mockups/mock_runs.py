"""Mock-ups: run board, decompose, integration."""

# ruff: noqa: E501
from __future__ import annotations

from html import escape
from pathlib import Path

from mock_shell import lamp, mast, page, panel, phases, spend

# ------------------------------------------------------------ run board ----


def build_run_board() -> None:
    comps = [
        ("go", "snippet-rules", "completed", "done", ["go"] * 7, "2", "4", "19m", "4.47M", "$5.71"),
        ("go", "token-crypto", "completed", "done", ["go"] * 7, "3", "1", "8m", "1.90M", "$3.48"),
        ("go", "storage", "completed", "done", ["go"] * 7, "2", "1", "33m", "8.00M", "$10.05"),
        (
            "run",
            "http-app",
            "running",
            "engineer · output 10s ago · worker alive (checked 2s ago)",
            ["run"] + ["off"] * 6,
            "1",
            "3",
            "11m",
            "·",
            "·",
        ),
        (
            "off",
            "http-server",
            "pending",
            "waiting on http-app",
            ["off"] * 7,
            "·",
            "·",
            "·",
            "·",
            "·",
        ),
        ("off", "cli", "pending", "waiting on http-server", ["off"] * 7, "·", "·", "·", "·", "·"),
    ]
    rows = ""
    for k, cid, st, ph, lamps, tries, it, t, tok, cost in comps:
        sel = ' class="sel"' if cid == "http-app" else ""
        rows += (
            f'<tr{sel}><td><a href="component-detail.html" class="mono">{cid}</a></td><td>{lamp(k, st)}</td>'
            f'<td class="full">{phases(lamps)}</td><td class="wrap">{escape(ph)}</td><td class="num" data-label="tries">{tries}</td><td class="num" data-label="iter">{it}</td>'
            f'<td class="num" data-label="time">{t}</td><td class="num" data-label="tokens">{tok}</td><td class="num" data-label="cost">{cost}</td></tr>'
        )
    board = f"""
<table class="data">
<thead><tr><th>Component</th><th>State</th><th title="engineer, verify, diff, review, security, distill, pr">Phases <span style="font-weight:400;letter-spacing:0;text-transform:none">eng · ver · diff · rev · sec · dis · pr</span></th><th>Now</th><th class="num">Tries</th><th class="num">Iter</th><th class="num">Time</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead>
<tbody>{rows}</tbody></table>
<div class="legend" style="padding:10px 14px;border-top:1px solid var(--glass-rule)">
<span>{lamp("go")} passed</span><span>{lamp("run")} running</span><span>{lamp("nogo")} failed</span><span>{lamp("off")} not started</span><span>{lamp("hold")} unknown</span>
<span class="muted">phases in order: engineer, verify, diff, review, security, distill, pr</span></div>"""

    delivery = f"""
<dl class="kv">
<dt>main is at</dt><dd><span class="mono">cea97b4</span> · {lamp("hold", "CI unknown")} <span class="muted">· the newest merge of this run</span></dd>
<dt>integration</dt><dd class="muted">no review recorded for this run</dd>
<dt>PR #1 token-crypto</dt><dd><span class="mono">4d74d12</span> · {lamp("go", "CI passed")} <span class="muted">· 7 checks passed · read 2s ago</span></dd>
<dt>PR #2 snippet-rules</dt><dd><span class="mono">23dd9ac</span> · {lamp("nogo", "CI failed")} <span class="muted">· check "test" failed · read 2s ago</span></dd>
<dt>PR #3 storage</dt><dd><span class="mono">cea97b4</span> · {lamp("hold", "CI unknown")} <span class="muted">· gh api failed (4): HTTP 401: Bad credentials · read 2s ago · <a href="#">full reply</a></span></dd>
</dl>"""

    feed_rows = [
        (
            "17:10:32",
            "storage",
            "w",
            "review finding [advisory] test_quality at tests/test_storage.py:1094-1107",
        ),
        (
            "17:10:32",
            "storage",
            "w",
            "review finding [advisory] other at src/snippetvault/storage.py:532-542",
        ),
        (
            "17:10:32",
            "storage",
            "w",
            "review finding [advisory] other at src/snippetvault/storage.py:488-491; tests/test_storage.py:250-252",
        ),
        (
            "17:10:32",
            "storage",
            "w",
            "review finding [advisory] dead_code at src/snippetvault/storage.py:433-438",
        ),
        (
            "17:10:32",
            "storage",
            "w",
            "review finding [advisory] test_quality at tests/test_storage.py:29,403-418",
        ),
        ("17:10:32", "storage", "g", "review passed in 140s"),
        (
            "17:11:26",
            "storage",
            "w",
            "security finding [low] information_disclosure at src/snippetvault/storage.py:141-167, 449-460",
        ),
        ("17:11:26", "storage", "g", "security passed in 53s"),
        ("17:11:55", "storage", "g", "distill passed in 30s"),
        ("17:12:06", "storage", "", "PR #3 opened"),
        ("17:12:06", "storage", "", "PR #3 merged"),
        ("17:12:06", "storage", "g", "pr passed in 10s"),
        ("17:12:06", "storage", "g", "completed · 1 iteration"),
        ("17:12:06", "http-app", "a", "started"),
        ("17:16:48", "http-app", "", "iteration 1 (281s)"),
        ("17:21:51", "http-app", "", "iteration 2 (301s)"),
    ]
    feed = (
        '<div class="feed">'
        + "".join(
            f'<span class="t">{t}</span><span><span class="who">{c}</span> <span class="{k}">{escape(txt)}</span></span>'
            for t, c, k, txt in feed_rows
        )
        + "</div>"
    )

    body = f"""
<div class="page-title"><h1>factory live01</h1><span class="sub">{lamp("run", "running")} · started 16:08 today · last event 21s ago · 3 of 6 components · <a href="serve.html">run by ks serve q-6990de</a></span></div>
{panel("Components", board, right='<a href="component-detail.html">open http-app</a>', glass=True, flush=True)}
<div style="height:20px"></div>
<div class="cols">
  <div class="stack">{panel("Activity", feed, right="newest last · follows live", glass=True)}</div>
  <div class="stack">{panel("Delivery", delivery, right="per merge commit")}
  {panel("Spend", f'''{spend("$19.24", "$78.00", 25)}<div class="muted" style="margin-top:8px">14.37M tokens · every call reported, so this is the whole amount, not a lower bound · cap from kstrl.toml <span class="mono">factory.max_cost_usd</span></div>''')}
  {panel("Agent", f'''<dl class="kv"><dt>component</dt><dd>http-app · engineer · iteration 3 of 10</dd><dt>last output</dt><dd>{lamp("go", "8s ago")} <span class="muted">stale after 1m without output</span></dd><dt>process</dt><dd>{lamp("go", "worker 46889 alive")} <span class="muted">· checked 2s ago, rechecked every 5s</span></dd></dl>''')}
  </div>
</div>"""
    m = mast(
        title="factory live01",
        clock="1:03:42",
        readout=spend("$19.24", "$78.00", 25),
        compact="$19.24 of $78 · 25%",
    )
    page("run-board.html", "Run board", m, "runs", body)


def build_component_detail() -> None:
    attempts = f"""
<div class="kv" style="grid-template-columns:90px minmax(0,1fr)">
<dt>attempt 1</dt><dd>{lamp("go", "engineer")} <span class="muted">312s</span> &nbsp; {lamp("nogo", "verify")} <span class="muted">1s · Tests failed (exit code 1)</span></dd>
<dt>attempt 2</dt><dd>{lamp("go", "engineer")} <span class="muted">312s</span> &nbsp; {lamp("nogo", "verify")} <span class="muted">1s · Tests failed (exit code 1)</span> &nbsp; <span class="muted">diff, review, security, distill and pr did not run</span></dd>
</div>"""
    output = """<pre><span class="m">collected 3 items

tests/test_tokens.py F.F                                                 [100%]</span>

<span class="h">____________________ test_generate_token_length ____________________</span>
    def test_generate_token_length() -> None:
>       assert len(generate_token()) == 43
<span class="e">E       AssertionError: assert 32 == 43
E        +  where 32 = len('1yTqgojHVxU9NIFBpmv14tT232INPESs')</span>
tests/test_tokens.py:14: AssertionError
<span class="h">_____________________ test_rejects_empty_token _____________________</span>
    def test_rejects_empty_token() -> None:
>       assert hash_token("") != hashlib.sha256(b"").hexdigest()
<span class="e">E       AssertionError: assert 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' != 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'</span>
tests/test_tokens.py:22: AssertionError
<span class="m">=========================== short test summary info ============================</span>
<span class="e">FAILED tests/test_tokens.py::test_generate_token_length - AssertionError
FAILED tests/test_tokens.py::test_rejects_empty_token - AssertionError</span>
<span class="e">========================= 2 failed, 1 passed in 0.02s ==========================</span></pre>"""
    failed = f"""
<div class="note bad" style="margin-bottom:12px">
<b>verify failed on attempt 2 · Tests failed (exit code 1)</b><br>
Mechanical verification failed: the test command exited 1. 2 tests failed, 1 passed, in <span class="mono">tests/test_tokens.py</span>. The retry budget for this component is spent (2 of 2 attempts), so the run marked it failed and skipped <b>storage</b>, which depends on it.
</div>
<div class="toolbar"><a class="btn primary" href="failures.html">Review retry</a><a class="btn" href="gate-output.html">Full output (35 lines)</a><span class="muted">or from a shell: <span class="mono">ks retry token-crypto</span></span></div>
<div class="path" style="margin-bottom:8px">.kstrl/debug/factory-20260926-171935.866868-gate01/token-crypto/attempt-2/test_suite.log · last 20 of 35 lines</div>
<section class="panel glass"><div class="pb">{output}</div></section>"""
    body = f"""
<div class="page-title"><h1>token-crypto</h1><span class="sub">Token generation, hashing and header parsing · {lamp("nogo", "failed")} · attempt 2 of 2 · took 10m · run gate01</span></div>
<div class="toolbar"><a class="btn quiet" href="run-board.html">Back to the board</a><span class="spacer"></span><span class="muted">skipped because of this failure: <span class="mono">storage</span></span></div>
{panel("Attempts", attempts)}
<div style="height:20px"></div>
{panel("Failed gate", failed, right="what failed, and the evidence")}
<div style="height:20px"></div>
<div class="cols-2">
{panel("Findings", '<div class="empty">No review or security findings: the component never reached those gates.</div>', flush=True)}
{panel("Engineer transcript", '<div class="empty">This component wrote no engineer transcript in this run.</div>', flush=True)}
</div>"""
    m = mast(
        title="factory gate01", readout=spend("$0.00", "$45.00", 0), compact="$0.00 of $45 · 0%"
    )
    page("component-detail.html", "Component: token-crypto", m, "runs", body)


def build_gate_output() -> None:
    text = Path(__file__).resolve().parent / "_pytest_fail.txt"
    raw = text.read_text(encoding="utf-8") if text.exists() else ""
    if not raw:
        raw = PYTEST_FAIL
    lines = []
    for ln in raw.splitlines():
        e = escape(ln)
        if ln.startswith("E ") or ln.startswith("FAILED") or "failed," in ln:
            lines.append(f'<span class="e">{e}</span>')
        elif ln.startswith("=") or ln.startswith("_"):
            lines.append(f'<span class="h">{e}</span>')
        elif (
            ln.startswith("platform")
            or ln.startswith("rootdir")
            or ln.startswith("plugins")
            or ln.startswith("asyncio")
            or ln.startswith("collected")
        ):
            lines.append(f'<span class="m">{e}</span>')
        else:
            lines.append(e)
    pre = "<pre>" + "\n".join(lines) + "</pre>"
    body = f"""
<div class="page-title"><h1>Gate output</h1><span class="sub">token-crypto · verify · attempt 2 · test command · 35 lines</span></div>
<div class="toolbar"><a class="btn quiet" href="component-detail.html">Back to token-crypto</a><a class="btn sm" href="#">Download</a><a class="btn sm" href="#">Copy path</a><span class="spacer"></span><label class="toggle"><input type="checkbox" checked> wrap long lines</label></div>
<div class="path" style="margin-bottom:10px">.kstrl/debug/factory-20260926-171935.866868-gate01/token-crypto/attempt-2/test_suite.log</div>
<section class="panel glass"><div class="pb">{pre}</div></section>"""
    m = mast(title="factory gate01")
    page("gate-output.html", "Gate output", m, "runs", body)


PYTEST_FAIL = """============================= test session starts ==============================
platform darwin -- Python 3.12.8, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/wumpinihussein/Documents/code/kstrl dogfood/snippetvault
plugins: syrupy-5.5.3, cov-7.1.0, asyncio-1.4.0, anyio-4.14.2
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 3 items

tests/test_tokens.py F.F                                                 [100%]

=================================== FAILURES ===================================
__________________________ test_generate_token_length __________________________

    def test_generate_token_length() -> None:
>       assert len(generate_token()) == 43
E       AssertionError: assert 32 == 43
E        +  where 32 = len('1yTqgojHVxU9NIFBpmv14tT232INPESs')
E        +    where '1yTqgojHVxU9NIFBpmv14tT232INPESs' = generate_token()

tests/test_tokens.py:14: AssertionError
___________________________ test_rejects_empty_token ___________________________

    def test_rejects_empty_token() -> None:
>       assert hash_token("") != hashlib.sha256(b"").hexdigest()
E       AssertionError: assert 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' != 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
E        +  where 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' = hash_token('')
E        +  and   'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' = <built-in method hexdigest of _hashlib.HASH object at 0x10a07fc90>()
E        +    where <built-in method hexdigest of _hashlib.HASH object at 0x10a07fc90> = <sha256 _hashlib.HASH object @ 0x10a07fc90>.hexdigest
E        +      where <sha256 _hashlib.HASH object @ 0x10a07fc90> = <built-in function openssl_sha256>(b'')
E        +        where <built-in function openssl_sha256> = hashlib.sha256

tests/test_tokens.py:22: AssertionError
=========================== short test summary info ============================
FAILED tests/test_tokens.py::test_generate_token_length - AssertionError: ass...
FAILED tests/test_tokens.py::test_rejects_empty_token - AssertionError: asser...  <!-- codespell:ignore asser -->
========================= 2 failed, 1 passed in 0.02s =========================="""

# ------------------------------------------------------------ decompose ----


def build_decompose() -> None:
    plan_rows = [
        ("snippet-rules", "Snippet domain rules", 0, "·"),
        ("token-crypto", "Token generation, hashing and header parsing", 0, "·"),
        ("storage", "SQLite storage for tokens and snippets", 1, "snippet-rules, token-crypto"),
        ("http-app", "HTTP routing and endpoint logic", 2, "snippet-rules, token-crypto, storage"),
        ("http-server", "Socket server, body limits and lifecycle", 3, "http-app"),
        ("cli", "Command-line entry point", 4, "storage, token-crypto, http-server"),
    ]
    rows = "".join(
        f'<tr><td class="mono">{c}</td><td class="wrap">{escape(t)}</td><td class="num">{tier}</td><td class="wrap">{escape(d)}</td><td>{lamp("go", "written")}</td></tr>'
        for c, t, tier, d in plan_rows
    )
    plan = f"""
<table class="data">
<thead><tr><th>Component</th><th>What it is</th><th class="num">Tier</th><th>Depends on</th><th>PRD</th></tr></thead>
<tbody>{rows}</tbody></table>"""
    issues = """
<p><b>19 major</b> and <b>19 minor</b> issues, no blockers, so the architect closed each one itself and recorded the choice in <span class="mono">scripts/kstrl/decisions.json</span>.</p>
<p class="muted">A blocker would have halted the run and appeared under Needs you.</p>
<a class="btn" href="spec-issues.html">Read the 38 issues</a>"""
    transcript = """<pre><span class="m">[Bash]</span> git ls-files &amp;&amp; echo --- &amp;&amp; find . -path ./.git -prune -o -type f -print
<span class="m">[Bash]</span> cat src/snippetvault/__init__.py; echo ---; cat pyproject.toml
<span class="m">[Bash]</span> cat spec.md | sed -n '1,51p' | grep -n "H8\\|S5\\|expiry"
<span class="m">[Bash]</span> uv run python -c 'import json; ...'
<span class="m">[Bash]</span> cd "/Users/wumpinihussein/Documents/code/kstrl dogfood/snippetvault"</pre>"""
    body = f"""
<div class="page-title"><h1>decompose db9df0</h1><span class="sub">{lamp("go", "completed")} · architect · attempt 1 · took 7:27 · 3d ago</span></div>
<div class="cols">
  <div class="stack">
    {panel("Plan", plan, n="6 components in 5 tiers", right='manifest <span class="mono">scripts/kstrl/manifest.json</span>', flush=True)}
    {panel("Architect transcript", transcript, right="saved · 6 lines", glass=True)}
  </div>
  <div class="stack">
    {panel("Spec issues", issues, right="the red-team pass over spec.md")}
    {panel("Spend", spend("$1.47", None, None) + '<div class="muted" style="margin-top:8px">342.4k tokens · one architect call · no cost cap was set for this run</div>')}
    {panel("Next", '<p>The manifest is ready. A factory run builds these six components tier by tier.</p><a class="btn primary" href="start.html">Start a factory run</a>')}
  </div>
</div>"""
    m = mast(title="decompose db9df0", readout=spend("$1.47", None, None), compact="$1.47 · no cap")
    page("decompose.html", "Decompose board", m, "runs", body)


def build_spec_issues() -> None:
    issues = [
        (
            "major",
            "contradiction",
            "S5 vs H8",
            "S5 sets the default expiry to 24 hours, but H8 says snippets expire after 7 days unless the client asks otherwise.",
        ),
        (
            "major",
            "ambiguity",
            "S5",
            "S5 says 'between 60 and 30 days' but does not say whether the bounds are inclusive, what unit the upper bound is in over the wire, or which JSON types are accepted.",
        ),
        (
            "major",
            "contradiction",
            "S4 vs H9",
            "H9 caps the whole request at 64 KiB, but S4 allows a snippet body of up to 64 KiB. A JSON envelope with a title and escaping can never carry a 64 KiB body, so the S4/H1 413 path cannot be reached over HTTP.",
        ),
        (
            "major",
            "missing detail",
            "H1",
            "The spec gives no response for a POST body that is not valid UTF-8, not valid JSON, not a JSON object, or has duplicate keys.",
        ),
        (
            "major",
            "undefined failure mode",
            "HTTP API preamble",
            "The spec defines no response for internal failures such as a database error or id-generation exhaustion, and it lists no error code for them.",
        ),
        (
            "major",
            "ambiguity",
            "T1",
            "T1 leaves the fate of a revoked token's snippets to the deployment owner but defines no way for the owner to express that choice.",
        ),
        (
            "major",
            "unstated assumption",
            "S1, K3",
            "Snippets record the creating token's name, and H3/H4 scope ownership by it. If a revoked token's name can be reused, the new token inherits the old token's snippets.",
        ),
        (
            "major",
            "undefined failure mode",
            "C4",
            "C4 defines no behavior for 'token create' with a name that already exists.",
        ),
        (
            "major",
            "missing detail",
            "K2",
            "K2 requires tokens to be stored hashed but names no algorithm or lookup method.",
        ),
        (
            "minor",
            "contradiction",
            "HTTP API preamble vs H1, H5",
            'The API preamble says every error body has the shape {"error": "<code>"}, but H1 adds a \'field\' key and H5 answers 404 with plain text.',
        ),
        (
            "minor",
            "missing detail",
            "C4, K3",
            "No length or character set is given for token names.",
        ),
        (
            "minor",
            "undefined failure mode",
            "C4",
            "C4 covers revoking an existing token and a missing one, but not a token that is already revoked.",
        ),
    ]
    rows = ""
    for i, (sev, kind, loc, summ) in enumerate(issues):
        sel = ' class="sel"' if i == 2 else ""
        rows += f'<tr{sel}><td><span class="sev {sev}">{sev}</span></td><td class="wrap">{escape(kind)}</td><td class="mono wrap">{escape(loc)}</td><td class="wrap">{escape(summ)}</td></tr>'
    table = f"""
<table class="data">
<thead><tr><th style="width:84px">Severity</th><th style="width:130px">Kind</th><th style="width:150px">Where</th><th>Summary</th></tr></thead>
<tbody>{rows}
<tr><td colspan="4" class="muted">26 more · <a href="#">show all 38</a></td></tr></tbody></table>"""
    detail = """
<p><span class="sev major">major</span> <b>contradiction</b> · <span class="mono">request-cap-vs-body-limit</span></p>
<p>H9 caps the whole request at 64 KiB, but S4 allows a snippet body of up to 64 KiB. A JSON envelope with a title and escaping can never carry a 64 KiB body, so the S4/H1 413 path cannot be reached over HTTP.</p>
<dl class="kv" style="margin-top:10px">
<dt>in the spec</dt><dd>S4 'body may not exceed 64 KiB (65536 bytes)' vs H9 'A request body larger than 64 KiB is refused with 413'</dd>
<dt>suggestion</dt><dd>Either raise the request cap above the snippet body limit or state that the usable body limit is less than 64 KiB.</dd>
<dt>what happened</dt><dd>Closed by the architect: recorded in <span class="mono">scripts/kstrl/decisions.json</span> as <span class="mono">request-cap-vs-body-limit</span>. It did not halt the run.</dd>
</dl>"""
    body = f"""
<div class="page-title"><h1>Spec issues</h1><span class="sub">decompose db9df0 · the architect's red-team findings on spec.md · 0 blocker · 19 major · 19 minor</span></div>
<div class="toolbar"><a class="btn quiet" href="decompose.html">Back to the plan</a><span class="spacer"></span><input type="text" placeholder="filter by kind, section or text" style="max-width:280px"></div>
<div class="cols">
  <div class="stack">{panel("Issues", table, n="38", flush=True)}</div>
  <div class="stack">{panel("Selected", detail)}</div>
</div>"""
    m = mast(title="decompose db9df0")
    page("spec-issues.html", "Spec issues", m, "runs", body)


# ---------------------------------------------------------- integration ----


def build_integration() -> None:
    rounds = f"""
<div class="strips">
<div class="strip">{lamp("nogo")}<div><div class="l1">Round 1 · open findings · blocking</div><div class="l2">5 findings opened, 1 handed off, 0 carried closed, 0 carried still open · reviewed 87c3e2e · 21:54</div></div><div class="act muted">review-1.json</div></div>
<div class="strip">{lamp("hold")}<div><div class="l1">Round 2 · not run</div><div class="l2">the last fix integration-fix-1 ended failed, not merged, so no tree holds it · 22:22</div></div><div class="act muted">review-2.json</div></div>
<div class="strip">{lamp("nogo")}<div><div class="l1">Round 3 · open findings · blocking</div><div class="l2">0 findings opened, 0 handed off, 1 carried closed, 3 carried still open · reviewed 5c1d0e7</div></div><div class="act muted">review-3.json</div></div>
</div>"""
    crit = [
        (
            "IC1",
            "nogo",
            "fail",
            "Calls across component boundaries",
            'An authenticated POST containing {"":0,"title":"t","body":"x"} produces InvalidField("") under src/snippetvault/snippets.py:219-222 and the server returns 500 at src/snippetvault/server.py:394-403 instead of the required validation 400.',
        ),
        (
            "IC2",
            "go",
            "pass",
            "Stored data read back",
            "Fixed by integration-fix-1: persisted-record validation is separated from new-input validation (round 3).",
        ),
        (
            "IC3",
            "nogo",
            "fail",
            "One definition per shared rule",
            "Accepted sort fields and orders are independently enumerated in src/snippetvault/api.py:51-56 and src/snippetvault/storage.py:40-51; REQUEST_FIELD is repeated literally in src/snippetvault/server.py:419-438.",
        ),
        (
            "IC4",
            "go",
            "pass",
            "Calls into code that predates the feature",
            "The base revision contains only the placeholder main at src/snippetvault/__init__.py:1-2 and no tests. That function is replaced, not called.",
        ),
        (
            "IC5",
            "nogo",
            "fail",
            "Decisions agree with criteria",
            "scripts/kstrl/decisions.json:284-290 binds http-server to finishing in-flight requests bounded by the ten-second socket timeout; the http-server PRD requires serve to return within two seconds.",
        ),
    ]
    crows = "".join(
        f'<tr><td class="mono">{i}</td><td class="wrap"><b>{escape(t)}</b></td><td>{lamp(k, v)}</td><td class="wrap muted">{escape(w)}</td></tr>'
        for i, k, v, t, w in crit
    )
    criteria = f'<table class="data"><thead><tr><th>Criterion</th><th>What it asks</th><th>Verdict</th><th>Why</th></tr></thead><tbody>{crows}</tbody></table>'
    finds = [
        (
            "IF-1",
            "IC1",
            "nogo",
            "open",
            "integration-fix-1 carries it, and that fix failed review",
            True,
        ),
        ("IF-2", "IC2", "go", "fixed", "by integration-fix-1, confirmed in round 3", False),
        (
            "IF-3",
            "IC3",
            "nogo",
            "open",
            "integration-fix-1 carries it, and that fix failed review",
            False,
        ),
        (
            "IF-4",
            "IC5",
            "park",
            "handed off",
            "outside what a fix component can change: a decision record, not code",
            False,
        ),
        (
            "IF-5",
            "concern",
            "nogo",
            "open",
            "integration-fix-1 carries it, and that fix failed review",
            False,
        ),
    ]
    frows = "".join(
        f'<tr{" class=sel" if s else ""}><td class="mono">{i}</td><td class="mono">{f}</td><td>{lamp(k, d)}</td><td class="wrap">{escape(w)}</td></tr>'
        for i, f, k, d, w, s in finds
    )
    findings = f'<table class="data"><thead><tr><th>Finding</th><th>From</th><th>Disposition</th><th>What happened to it</th></tr></thead><tbody>{frows}</tbody></table>'
    detail = """
<p><span class="mono">IF-1</span> · from IC1 · {open}</p>
<p>IC1 failed: An authenticated POST containing {"":0,"title":"t","body":"x"} produces InvalidField("") under src/snippetvault/snippets.py:219-222, consistent with its documented verbatim unknown-key contract at lines 43-48. src/snippetvault/api.py:320-323 forwards that field to error_response, which rejects it at lines 147-149. The server consequently returns 500 at src/snippetvault/server.py:394-403 instead of the required validation 400.</p>
<dl class="kv" style="margin-top:10px">
<dt>suggestion</dt><dd>Allow an empty string as an error field while retaining None for an absent field. Add an authenticated POST regression test covering an empty unknown key.</dd>
<dt>locations</dt><dd class="mono">src/snippetvault/snippets.py · src/snippetvault/api.py · src/snippetvault/server.py</dd>
<dt>history</dt><dd>opened in run fda682 at 87c3e2e · carried open in round 3</dd>
</dl>""".replace("{open}", lamp("nogo", "open"))
    body = f"""
<div class="page-title"><h1>Integration review</h1><span class="sub">run fda682 · e3root · the merged feature checked as one tree · {lamp("nogo", "blocking")} · 3 findings open · 1 handed off · 1 fixed</span></div>
<div class="note warn" style="margin-bottom:20px"><b>Why the run halted:</b> integration-fix-1 failed review and the fix budget (1) is spent. The halted-run item in <a href="inbox-halted.html">Decisions</a> is yours to close; a new fix needs a new run.</div>
<div class="cols">
  <div class="stack">
    {panel("Rounds", rounds, n="3", flush=True)}
    {panel("Criteria", criteria, n="5 · 2 pass · 3 fail", flush=True)}
    {panel("Findings", findings, n="5 · 3 open · 1 handed off · 1 fixed", right="agrees with round 3: 1 carried closed, 3 carried still open", flush=True)}
  </div>
  <div class="stack">{panel("Selected finding", detail)}</div>
</div>"""
    m = mast(
        project="e3root",
        title="factory fda682",
        readout=spend("$4.50", "$45.00", 11, lower_bound=True),
        compact="at least $4.50 of $45 · 11%",
    )
    page("integration.html", "Integration review", m, "runs", body, needs=1, failures=1)
