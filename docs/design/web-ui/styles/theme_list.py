"""Directions A and B: list rows on a quiet canvas, a thin sidebar, one accent.

The two share one structure and differ in their tokens: A is the dark
canvas, B the light one. Nothing is boxed; sections are a title row and
hairline-separated rows.
"""

# ruff: noqa: E501
from __future__ import annotations

from html import escape
from pathlib import Path

import content as c

HERE = Path(__file__).resolve().parent

FONT = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap"

DARK = """
  --canvas:#111216; --raised:#181a20; --hover:#20232b; --sel:#20232b;
  --line:#2d3038; --line-soft:#23262d; --line-strong:#414550;
  --t1:#f1f2f4; --t2:#b5b9c2; --t3:#8e949f;
  --accent:#a9a0ff; --accent-ink:#111216; --accent-soft:rgba(169,160,255,.16);
  --ok:#7ddca2; --bad:#ff8585; --wait:#e5c26a; --run:#83b7ff; --run-rgb:131,183,255; --park:#d9a7ed; --queued:#5c616c;
  --btn:#181a20; --btn-line:#414550; --btn-hover:#20232b;
  --code:#181a20; --code-line:#23262d; --add:#7ddca2; --del:#ff8585; --add-bg:rgba(125,220,162,.10); --del-bg:rgba(255,133,133,.10);
  --focus:#a9a0ff; --scheme:dark;
"""

LIGHT = """
  --canvas:#f8f9fb; --raised:#f0f2f6; --hover:#ffffff; --sel:#ffffff;
  --line:#dce0e7; --line-soft:#e6e9ef; --line-strong:#c5cbd5;
  --t1:#171a21; --t2:#535c69; --t3:#657080;
  --accent:#594ccb; --accent-ink:#ffffff; --accent-soft:rgba(89,76,203,.12);
  --ok:#176a3a; --bad:#b32631; --wait:#755600; --run:#205bac; --run-rgb:32,91,172; --park:#76408d; --queued:#9aa2ae;
  --btn:#ffffff; --btn-line:#c5cbd5; --btn-hover:#f0f2f6;
  --code:#ffffff; --code-line:#dce0e7; --add:#176a3a; --del:#b32631; --add-bg:rgba(23,106,58,.08); --del-bg:rgba(179,38,49,.08);
  --focus:#594ccb; --scheme:light;
"""

CSS = r"""
*{box-sizing:border-box}
html{font-size:13px;-webkit-text-size-adjust:100%;color-scheme:var(--scheme)}
body{margin:0;background:var(--canvas);color:var(--t1);font-family:"Inter",system-ui,sans-serif;line-height:20px;letter-spacing:-.006em;font-variant-numeric:tabular-nums;font-feature-settings:"cv11","ss01"}
::selection{background:var(--accent-soft);color:var(--t1)}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
*{scrollbar-color:var(--line) transparent;scrollbar-width:thin;caret-color:var(--accent)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line);border:3px solid var(--canvas);border-radius:6px}
a{color:inherit;text-decoration:none}
a.link{color:var(--t2);text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--line)}
a.link:hover{color:var(--t1)}
code,.mono,kbd{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12px;font-variant-ligatures:none;letter-spacing:0}
b{font-weight:600}
.t2{color:var(--t2)} .t3{color:var(--t3)}

/* shell */
.shell{display:grid;grid-template-columns:200px minmax(0,1fr);min-height:100vh}
.side{border-right:1px solid var(--line);padding:14px 10px;display:flex;flex-direction:column;gap:18px;background:var(--raised)}
.side .brand{display:flex;align-items:center;gap:10px;padding:4px 8px;font-weight:600}
.side .brand .mark{width:18px;height:18px;border-radius:5px;background:var(--accent);display:inline-block}
.side .brand .proj{color:var(--t2);font-weight:500}
.side nav{display:flex;flex-direction:column;gap:1px}
.side nav .group{color:var(--t3);font-size:12px;padding:6px 8px 4px;font-weight:500}
.side nav a{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;color:var(--t2);font-weight:500;line-height:18px}
.side nav a:hover{background:var(--hover);color:var(--t1)}
.side nav a.on{background:var(--hover);color:var(--t1)}
.side nav a .n{margin-left:auto;color:var(--t3);font-size:12px}
.side nav a .n.hot{color:var(--t1);background:var(--accent-soft);border-radius:4px;padding:0 6px;line-height:18px}

.main{min-width:0}
.top{height:44px;display:flex;align-items:center;gap:14px;padding:0 24px;border-bottom:1px solid var(--line);color:var(--t2);white-space:nowrap;overflow:hidden}
.top .crumb b{color:var(--t1);font-weight:500}
.top .crumb .sep{color:var(--t3);margin:0 6px}
.top .status{display:flex;gap:14px;margin-left:auto;align-items:center}
.top .status .dotword{color:var(--t2)}
.top .readout{display:flex;align-items:center;gap:10px;color:var(--t2)}
.top .readout b{color:var(--t1);font-weight:500}
.top .cmd{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 8px;border:1px solid var(--line);border-radius:6px;color:var(--t2);background:var(--canvas)}
.top .cmd kbd{font-size:11px;line-height:16px;color:var(--t3);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.top .clock{font-family:"JetBrains Mono",monospace;font-size:12px;color:var(--t2);border:1px solid var(--line);border-radius:4px;padding:1px 7px}
.page{padding:24px 24px 40px;max-width:1240px}
h1{font-size:18px;line-height:26px;font-weight:600;letter-spacing:-.014em;margin:0}
.sub{color:var(--t2);margin:2px 0 0}
.head{margin-bottom:22px}

/* meter */
.meter{display:inline-block;width:72px;height:4px;background:var(--line);border-radius:2px;vertical-align:middle;overflow:hidden}
.meter i{display:block;height:100%;background:var(--accent)}

/* sections: a title row, then rows */
.cols{display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:40px;align-items:start}
.sec{margin-bottom:28px}
.sec .st{display:flex;align-items:baseline;gap:8px;padding:0 0 6px;border-bottom:1px solid var(--line);font-weight:500}
.sec .st .n{color:var(--t3);font-weight:400}
.sec .st .r{margin-left:auto;color:var(--t3);font-weight:400;font-size:12px}
.sec .st .r a{color:var(--t2)}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line-soft)}
.row .l1{display:flex;align-items:center;gap:8px;font-weight:500}
.row .l2{color:var(--t2);padding-left:16px;margin-top:1px}
.row.sel{background:var(--sel);margin:0 -12px;padding:10px 12px;border-radius:6px;border-bottom-color:transparent;box-shadow:inset 0 0 0 1px var(--line)}
.row .act{white-space:nowrap}

/* state: a dot, always with a word */
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--queued);flex:none;vertical-align:0}
.dot.passed{background:var(--ok)} .dot.failed{background:var(--bad)} .dot.waiting{background:var(--wait)}
.dot.running{background:var(--run);animation:pulse 1.8s cubic-bezier(.2,.8,.2,1) infinite}
.dot.parked{background:var(--park)} .dot.queued{background:var(--queued)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(var(--run-rgb),.45)}70%{box-shadow:0 0 0 6px rgba(var(--run-rgb),0)}100%{box-shadow:0 0 0 0 rgba(var(--run-rgb),0)}}
@media (prefers-reduced-motion:reduce){.dot.running{animation:none}}
.dotword{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;border:1px solid var(--btn-line);border-radius:6px;background:var(--btn);color:var(--t1);font:inherit;font-weight:500;cursor:pointer;line-height:26px}
.btn:hover{background:var(--btn-hover)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.primary:hover{filter:brightness(1.08)}
.btn.danger{color:var(--bad)}
.btn.quiet{background:transparent;border-color:transparent;color:var(--t2)}
.btn.quiet:hover{background:var(--hover);color:var(--t1)}

/* delivery */
.main-at{padding:10px 0;border-bottom:1px solid var(--line-soft)}
.grp{color:var(--t3);font-size:12px;padding:10px 0 4px}
.pr{display:grid;grid-template-columns:150px 64px minmax(0,1fr);gap:12px;padding:7px 0;border-bottom:1px solid var(--line-soft);align-items:start}
.pr .why{color:var(--t2);font-size:12px;line-height:17px}

/* tables */
table.data{width:100%;border-collapse:collapse}
table.data th{text-align:left;font-weight:500;color:var(--t3);font-size:12px;padding:6px 8px 6px 0;border-bottom:1px solid var(--line)}
table.data td{padding:8px 8px 8px 0;border-bottom:1px solid var(--line-soft);vertical-align:top}
table.data th.num,table.data td.num{text-align:right;white-space:nowrap}
table.data tr.sel td{background:var(--sel)}
table.data tr.sel td:first-child{border-radius:6px 0 0 6px;padding-left:8px}
table.data tr.sel td:last-child{border-radius:0 6px 6px 0}
table.data .note{color:var(--t2);padding-left:20px}
.expand{padding:12px 0 4px 24px;color:var(--t2);max-width:76ch;border-bottom:1px solid var(--line-soft)}
.expand b{color:var(--t1)}

/* checkpoint */
.cols-cp{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:40px;align-items:start}
.kv{display:grid;grid-template-columns:132px minmax(0,1fr);gap:0}
.kv>div{padding:8px 0;border-bottom:1px solid var(--line-soft)}
.kv>div:nth-child(odd){color:var(--t2)}
.finding{display:grid;grid-template-columns:64px 72px 200px minmax(0,1fr);gap:12px;padding:8px 0;border-bottom:1px solid var(--line-soft);align-items:start}
.sev{display:inline-block;font-size:11px;line-height:18px;padding:0 6px;border-radius:4px;border:1px solid var(--line);color:var(--t2)}
.sev.advisory{color:var(--wait);border-color:var(--wait)} .sev.low{color:var(--run);border-color:var(--run)}
.diff{border:1px solid var(--line-soft);border-radius:8px;background:var(--code);overflow:auto;margin-top:8px}
.diff pre{margin:0;padding:12px 14px;font-family:"JetBrains Mono",monospace;font-size:12px;line-height:19px;font-variant-ligatures:none}
.diff .file{color:var(--t1);font-weight:500;display:block;margin-top:8px}
.diff .file:first-child{margin-top:0}
.diff .hunk{color:var(--t3);display:block}
.diff .add{color:var(--add);background:var(--add-bg);display:block} .diff .del{color:var(--del);background:var(--del-bg);display:block}
.diff .more{color:var(--t3);display:block;margin-top:6px}
.choices{position:sticky;top:24px}
.choice{padding:12px 0;border-bottom:1px solid var(--line-soft)}
.choice .ctl{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.choice .pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.choice .pair .btn{margin-bottom:8px}
.choice .does{color:var(--t2)}
.choice .does+.does{margin-top:6px}
.choice .does b{color:var(--t1)}
.choice .does .who{color:var(--t1);font-weight:500}

/* phone */
.bottom-bar{display:none}
@media (max-width:600px){
  .shell{grid-template-columns:minmax(0,1fr)}
  .side{display:none}
  .top{height:auto;padding:10px 16px;flex-wrap:wrap;gap:6px 12px;white-space:normal}
  .top .status{margin-left:0;flex-wrap:wrap;gap:6px 12px;width:100%}
  .top .status .hide-sm{display:none}
  .page{padding:16px 16px 84px}
  .cols,.cols-cp{grid-template-columns:minmax(0,1fr);gap:0}
  .row{grid-template-columns:minmax(0,1fr)}
  .row .act{padding-left:16px}
  .pr{grid-template-columns:minmax(0,1fr)}
  .pr .why{margin-top:-2px}
  table.data,table.data tbody,table.data tr,table.data td{display:block}
  table.data thead{display:none}
  table.data tr{padding:8px 0;border-bottom:1px solid var(--line-soft)}
  table.data td{border:0;padding:1px 0;text-align:left}
  table.data td.num{text-align:left}
  table.data td[data-label]::before{content:attr(data-label) " ";color:var(--t3)}
  table.data td:empty{display:none}
  table.data tr.sel td{background:transparent}
  table.data tr.sel{background:var(--sel);margin:0 -8px;padding:8px;border-radius:6px}
  .expand{padding-left:0}
  .choices{position:static}
  .finding{grid-template-columns:64px 72px minmax(0,1fr)}
  .finding>span:last-child{grid-column:1/4}
  .kv{grid-template-columns:minmax(0,1fr)}
  .kv>div:nth-child(odd){padding-bottom:0;border-bottom:0}
  .choice .pair{grid-template-columns:minmax(0,1fr)}
  .bottom-bar{display:flex;position:fixed;left:0;right:0;bottom:0;height:64px;background:var(--raised);border-top:1px solid var(--line);padding:6px 8px calc(6px + env(safe-area-inset-bottom))}
  .bottom-bar a{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:2px;font-size:11px;line-height:14px;color:var(--t2);font-weight:500}
  .bottom-bar a.on{color:var(--t1)}
  .bottom-bar a .count{font-size:11px;line-height:16px;background:var(--accent);color:var(--accent-ink);border-radius:8px;padding:0 6px;min-width:18px;text-align:center}
  .bottom-bar a .count-space{height:16px}
}
"""


def dot(state: str, word: str = "") -> str:
    if word:
        return f'<span class="dotword"><span class="dot {state}"></span>{escape(word)}</span>'
    return f'<span class="dot {state}"></span>'


def meter(pct: int) -> str:
    return f'<span class="meter"><i style="width:{pct}%"></i></span>'


def side(active: str) -> str:
    items = []
    for key, name, n in c.NAV:
        on = ' class="on"' if key == active else ""
        count = f'<span class="n hot">{n}</span>' if n else ""
        items.append(f'<a{on} href="#">{escape(name)}{count}</a>')
    start = "".join(f'<a href="#">{escape(name)}</a>' for _, name in c.NAV_START)
    return f"""
<aside class="side">
  <div class="brand"><span class="mark"></span>kstrl <span class="proj">{c.PROJECT}</span></div>
  <nav aria-label="views">{"".join(items)}<div class="group">Start</div>{start}</nav>
</aside>"""


def top(crumb: str, readout: str, clock: str) -> str:
    return f"""
<header class="top">
  <span class="crumb">{crumb}</span>
  <span class="status">
    <span class="dotword hide-sm">{dot("passed")}{c.MAST["config"]}</span>
    <span class="dotword hide-sm">{dot("passed")}{c.MAST["safe"]}</span>
    <span class="dotword hide-sm">{dot("running")}{c.MAST["serve"]}</span>
    <span class="readout">{readout}</span>
    <span class="clock">{clock}</span>
    <a class="cmd hide-sm" href="#">Commands <kbd>⌘K</kbd></a>
  </span>
</header>"""


def bottom(active: str) -> str:
    tabs = [
        ("home", "Home"),
        ("runs", "Runs"),
        ("decisions", "Decide"),
        ("failures", "Failures"),
        ("more", "More"),
    ]
    out = []
    for key, name in tabs:
        on = ' class="on"' if key == active else ""
        count = (
            '<span class="count">2</span>'
            if key == "decisions"
            else '<span class="count-space"></span>'
        )
        out.append(f'<a{on} href="#">{count}{name}</a>')
    return f'<nav class="bottom-bar" aria-label="views">{"".join(out)}</nav>'


def page(out: Path, title: str, tokens: str, active: str, header: str, body: str) -> None:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" href="{FONT}">
<style>:root{{{tokens}}}{CSS}</style></head>
<body><div class="shell">{side(active)}<div class="main">{header}<main class="page">{body}</main></div></div>{bottom(active)}</body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(line.rstrip() for line in html.splitlines()) + "\n", encoding="utf-8")


def sec(title: str, body: str, n: str = "", right: str = "") -> str:
    nn = f'<span class="n">{escape(n)}</span>' if n else ""
    rr = f'<span class="r">{right}</span>' if right else ""
    return f'<section class="sec"><div class="st">{escape(title)}{nn}{rr}</div>{body}</section>'


def row(r: c.Row, extra: str = "", sel: bool = False) -> str:
    act = ""
    if r.action:
        cls = "btn primary" if r.primary else ("btn quiet" if r.action == "Queue" else "btn")
        act = f'<a class="{cls}" href="{r.href}">{escape(r.action)}</a>'
    return f"""<div class="row{" sel" if sel else ""}"><div><div class="l1">{dot(r.state)}{escape(r.title)}</div><div class="l2">{escape(r.detail)}</div>{extra}</div><div class="act">{act}</div></div>"""


def home_body() -> str:
    spent, cap, pct = c.SPEND
    needs = "".join(row(r) for r in c.NEEDS)
    active = ""
    for i, r in enumerate(c.ACTIVE):
        extra = ""
        if i == 0:
            extra = f'<div class="l2" style="margin-top:4px"><b>{spent}</b> of {cap} cap {meter(pct)} {pct}%</div>'
        active += row(r, extra)
    sha, st, word, why = c.MAIN_AT
    delivery = f'<div class="main-at"><b>main is at <code>{sha}</code></b> <span class="t3">·</span> {dot(st, word)} <span class="t2">· {escape(why)}</span></div>'
    for heading, prs in c.DELIVERY:
        delivery += f'<div class="grp">{escape(heading)}</div>'
        for pr, comp, sha, st, word, why, link in prs:
            more = f' · <a class="link" href="#">{escape(link)}</a>' if link else ""
            delivery += f'<div class="pr"><span>{pr} <span class="t2">{escape(comp)}</span></span><code>{sha}</code><span>{dot(st, word)}<div class="why">{escape(why)}{more}</div></span></div>'
    hist = ""
    for st, rid, kind, word, age, comps, tok, cost, note in c.HISTORY:
        sel = rid == c.HISTORY_SELECTED
        hist += (
            f'<tr{" class=sel" if sel else ""}><td data-label="run"><code>{rid}</code></td><td data-label="kind">{kind}</td><td data-label="state">{dot(st, word)}</td>'
            f'<td class="num t2" data-label="last event">{age}</td><td class="num" data-label="components">{escape(comps)}</td><td class="num" data-label="tokens">{tok}</td>'
            f'<td class="num" data-label="cost">{escape(cost)}</td><td class="note">{escape(note)}</td></tr>'
        )
    heads = "".join(
        f"<th{' class=num' if h in ('Last event', 'Components', 'Tokens', 'Cost') else ''}>{h}</th>"
        for h in c.HISTORY_HEAD
    )
    history = f'<table class="data"><thead><tr>{heads}</tr></thead><tbody>{hist}</tbody></table><div class="expand"><b>{c.HISTORY_NOTE_HEAD}</b> <span class="t3">({c.HISTORY_NOTE_SUB})</span><br>{c.HISTORY_NOTE}</div>'
    return f"""
<div class="head"><h1>{c.PROJECT}</h1><p class="sub">{c.HOME_SUBTITLE}</p></div>
<div class="cols">
  <div>
    {sec("Needs you", needs, "3", '<a href="#">2 decisions</a> · <a href="#">1 failure</a>')}
    {sec("Active", active, "3")}
  </div>
  <div>{sec("Delivery", delivery, right="is main green after the merges")}</div>
</div>
{sec("History", history, "9 runs", "percentages round up · select a row to read its whole note")}"""


def checkpoint_body() -> str:
    kv = ""
    for label, st, word, rest in c.CHECKS:
        val = f"{dot(st, word)} <span class='t2'>· {rest}</span>" if st else rest
        kv += f"<div>{escape(label)}</div><div>{val}</div>"
    findings = "".join(
        f'<div class="finding"><span class="t2">{p}</span><span><span class="sev {s}">{s}</span></span><code>{escape(w)}</code><span>{escape(f)}</span></div>'
        for p, s, w, f in c.FINDINGS
    )
    diff = "".join(f'<span class="{k}">{escape(t)}</span>' for k, t in c.DIFF)
    diff = f'<div class="diff"><pre>{diff}</pre></div>'
    first, second, *rest = c.CHOICES
    choices = f"""<div class="choice"><div class="pair"><div><a class="btn primary" href="#">{first[0]}</a><div class="does">{first[3]}</div></div>
<div><a class="btn" href="#">{second[0]}</a><div class="does">{second[3]}</div></div></div></div>"""
    for label, kind, _key, does in rest:
        cls = {"danger": "btn danger"}.get(kind, "btn")
        choices += f'<div class="choice"><div class="ctl"><a class="{cls}" href="#">{label}</a></div><div class="does">{does}</div></div>'
    return f"""
<div class="head"><h1>{c.CP_TITLE}</h1><p class="sub">{c.CP_SUBTITLE}</p></div>
<div class="cols-cp">
  <div>
    {sec("Checks before this point", f'<div class="kv">{kv}</div>', right="every gate that ran on this component")}
    {sec("Findings", findings, "3")}
    {sec("Diff", diff, right=c.DIFF_SUMMARY)}
  </div>
  <div class="choices">{sec("Your decision", choices, right="each choice says what it does")}</div>
</div>"""


def build(name: str, tokens: str) -> None:
    d = HERE / name
    spent, cap, pct = c.SPEND
    readout = f"<b>{spent}</b> of {cap} {meter(pct)} {pct}%"
    page(
        d / "home.html",
        "Home",
        tokens,
        "home",
        top(f"<b>{c.PROJECT}</b><span class='sep'>/</span>Home", readout, c.CLOCK),
        home_body(),
    )
    page(
        d / "checkpoint.html",
        "Checkpoint decision",
        tokens,
        "runs",
        top(
            f"<b>{c.PROJECT}</b><span class='sep'>/</span>Runs<span class='sep'>/</span>live01<span class='sep'>/</span>comp-c checkpoint",
            f"<b>{c.CP_SPEND}</b> · no cost cap",
            c.CP_CLOCK,
        ),
        checkpoint_body(),
    )


def build_all() -> None:
    build("a-list-dark", DARK)
    build("b-list-light", LIGHT)
