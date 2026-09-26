"""Direction C: command-first. A command field is the primary way to act.

No sidebar. A command field sits under a thin top strip; every list is
compact rows with a state icon, a word and a keyboard hint at the row's
end; the focused row's actions appear in an action rail. On the
checkpoint the five choices are an action list with chorded shortcuts,
each consequence kept under its control.
"""

# ruff: noqa: E501
from __future__ import annotations

from html import escape
from pathlib import Path

import content as c

HERE = Path(__file__).resolve().parent

FONT = "https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap"

TOKENS = """
  --canvas:#13141c; --raised:#20212c; --raised-2:#292b38;
  --line:#383a49; --line-strong:#505366; --line-soft:#2a2c39;
  --t1:#f7f7fb; --t2:#bfc2d2; --t3:#969aaa;
  --accent:#d7a8ff; --accent-ink:#13141c; --accent-soft:rgba(215,168,255,.14);
  --ok:#8dddab; --bad:#ff9097; --wait:#eac879; --run:#9dc8ff; --park:#e0b1e8; --queued:#6c7080;
  --add-bg:rgba(141,221,171,.10); --del-bg:rgba(255,144,151,.10);
"""

CSS = r"""
*{box-sizing:border-box}
html{font-size:13px;-webkit-text-size-adjust:100%;color-scheme:dark}
body{margin:0;background:var(--canvas);color:var(--t1);font-family:"Geist",system-ui,sans-serif;line-height:19px;font-variant-numeric:tabular-nums}
::selection{background:var(--accent-soft);color:var(--t1)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
*{scrollbar-color:var(--line) transparent;scrollbar-width:thin;caret-color:var(--accent)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line);border:3px solid var(--canvas);border-radius:6px}
a{color:inherit;text-decoration:none}
a.link{color:var(--t2);text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--line-strong)}
code,.mono{font-family:"Geist Mono",ui-monospace,monospace;font-size:12px;font-variant-ligatures:none}
kbd{font-family:"Geist Mono",ui-monospace,monospace;font-size:11px;line-height:18px;color:var(--t2);background:var(--raised-2);border-radius:4px;padding:0 5px;white-space:nowrap}
b{font-weight:600}
.t2{color:var(--t2)} .t3{color:var(--t3)}

/* top strip */
.strip{height:44px;display:flex;align-items:center;gap:20px;padding:0 24px;border-bottom:1px solid var(--line-soft);white-space:nowrap}
.strip .brand{display:flex;align-items:center;gap:8px;font-weight:600}
.strip .brand .mark{width:16px;height:16px;border-radius:4px;background:var(--accent)}
.strip .tabs{display:flex;gap:2px}
.strip .tabs a{padding:3px 8px;border-radius:6px;color:var(--t2);font-weight:500}
.strip .tabs a.on{background:var(--raised);color:var(--t1)}
.strip .tabs a .n{color:var(--t3);margin-left:5px}
.strip .tabs a .n.hot{color:var(--accent)}
.strip .right{margin-left:auto;display:flex;align-items:center;gap:16px;color:var(--t2)}
.strip .right b{color:var(--t1);font-weight:500}
.strip .clock{font-family:"Geist Mono",monospace;font-size:12px;color:var(--t2)}

/* the command field */
.wrap{max-width:1320px;padding:20px 24px 48px;margin:0 auto}
.cmd{display:flex;align-items:center;gap:12px;height:46px;padding:0 16px;border:1px solid var(--line);border-radius:10px;background:var(--raised);font-size:15px;color:var(--t3)}
.cmd .caret{width:2px;height:20px;background:var(--accent);border-radius:1px;animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
@media (prefers-reduced-motion:reduce){.cmd .caret{animation:none}}
.cmd .right{margin-left:auto;display:flex;gap:10px;align-items:center}
.hints{display:flex;gap:14px;padding:8px 4px 0;color:var(--t3);font-size:12px}
.hints span{display:inline-flex;gap:6px;align-items:center}
h1{font-size:21px;line-height:28px;font-weight:600;letter-spacing:-.012em;margin:0}
.sub{color:var(--t2);margin:2px 0 0}
.head{margin:22px 0 14px}

/* layout: lists left, action rail right */
.grid{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:28px;align-items:start;margin-top:20px}
.rail{position:sticky;top:20px;border:1px solid var(--line);border-radius:12px;background:var(--raised);padding:6px;box-shadow:0 20px 48px rgba(0,0,0,.33)}
.rail .rt{padding:8px 10px 6px;color:var(--t3);font-size:12px}
.rail .rt b{color:var(--t1);font-weight:500;display:block;font-size:13px;margin-bottom:2px;white-space:normal}
.rail a{display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:8px;color:var(--t1)}
.rail a:hover,.rail a.on{background:var(--raised-2)}
.rail a kbd{margin-left:auto}
.rail .does{padding:2px 10px 8px;color:var(--t2);font-size:12px;line-height:17px}
.rail .does b{color:var(--t1)}
.rail hr{border:0;border-top:1px solid var(--line-soft);margin:4px 0}

/* sections and rows */
.sec{margin-bottom:22px}
.sec .st{display:flex;align-items:baseline;gap:8px;padding:0 8px 6px;color:var(--t3);font-size:12px;font-weight:500}
.sec .st .r{margin-left:auto;font-weight:400}
.sec .st .r a{color:var(--t2)}
.row{display:grid;grid-template-columns:22px minmax(0,1fr) auto;gap:10px;align-items:start;padding:8px 8px;border-radius:8px;border-bottom:1px solid var(--line-soft)}
.row:hover{background:var(--raised)}
.row.on{background:var(--raised-2);border-bottom-color:transparent}
.row .l1{font-weight:500}
.row .l1 .kind{color:var(--t3);font-weight:400;margin-left:6px}
.row .l2{color:var(--t2)}
.row .end{display:flex;align-items:center;gap:8px;white-space:nowrap;color:var(--t3);padding-top:1px}
.row .end .w{font-size:12px}

/* state icons: a 16px tile with a drawn glyph, always beside a word */
.ico{width:16px;height:16px;border-radius:4px;display:inline-flex;align-items:center;justify-content:center;flex:none;margin-top:1px}
.ico svg{width:12px;height:12px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.ico.passed{color:var(--ok);background:rgba(141,221,171,.14)} .ico.failed{color:var(--bad);background:rgba(255,144,151,.14)}
.ico.waiting{color:var(--wait);background:rgba(234,200,121,.14)} .ico.running{color:var(--run);background:rgba(157,200,255,.14)}
.ico.parked{color:var(--park);background:rgba(224,177,232,.14)} .ico.queued{color:var(--t3);background:rgba(150,154,170,.14)}
.state{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}

/* delivery and history */
.main-at{padding:8px 8px;border-bottom:1px solid var(--line-soft)}
.grp{color:var(--t3);font-size:12px;padding:10px 8px 4px}
.pr{display:grid;grid-template-columns:22px 150px 70px minmax(0,1fr) auto;gap:10px;padding:7px 8px;border-bottom:1px solid var(--line-soft);align-items:start;border-radius:8px}
.pr:hover{background:var(--raised)}
.pr .why{color:var(--t2);font-size:12px;line-height:17px}
.pr kbd{justify-self:end}
table.data{width:100%;border-collapse:collapse}
table.data th{text-align:left;font-weight:500;color:var(--t3);font-size:12px;padding:4px 8px;border-bottom:1px solid var(--line-soft)}
table.data td{padding:7px 8px;border-bottom:1px solid var(--line-soft);vertical-align:top}
table.data th.num,table.data td.num{text-align:right;white-space:nowrap}
table.data tr.on td{background:var(--raised-2)}
table.data tr.on td:first-child{border-radius:8px 0 0 8px} table.data tr.on td:last-child{border-radius:0 8px 8px 0}
table.data .note{color:var(--t2)}
.expand{padding:10px 8px 4px 38px;color:var(--t2);max-width:78ch;border-bottom:1px solid var(--line-soft)}
.expand b{color:var(--t1)}
.meter{display:inline-block;width:80px;height:4px;background:var(--line);border-radius:2px;vertical-align:middle;overflow:hidden}
.meter i{display:block;height:100%;background:var(--accent)}

/* checkpoint */
.cp{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:28px;align-items:start;margin-top:8px}
.cp .side{position:sticky;top:20px}
.kv{display:grid;grid-template-columns:120px minmax(0,1fr)}
.kv>div{padding:7px 8px;border-bottom:1px solid var(--line-soft)}
.kv>div:nth-child(odd){color:var(--t3)}
.acts{border:1px solid var(--line);border-radius:12px;background:var(--raised);padding:6px;box-shadow:0 20px 48px rgba(0,0,0,.33)}
.act{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;padding:8px 10px;border-radius:8px}
.act:hover,.act.on{background:var(--raised-2)}
.act .lab{font-weight:500}
.act .lab.primary{color:var(--accent)} .act .lab.danger{color:var(--bad)}
.act .does{grid-column:1/3;color:var(--t2);font-size:12px;line-height:17px}
.act .does b{color:var(--t1)}
.finding{display:grid;grid-template-columns:22px 58px 66px minmax(0,1fr);gap:10px;padding:8px;border-bottom:1px solid var(--line-soft);align-items:start;border-radius:8px}
.finding:hover{background:var(--raised)}
.finding .where{display:block;color:var(--t3);margin-top:2px}
.sev{display:inline-block;font-size:11px;line-height:18px;padding:0 6px;border-radius:4px;background:var(--raised-2);color:var(--t2)}
.sev.advisory{color:var(--wait)} .sev.low{color:var(--run)}
.diff{border:1px solid var(--line);border-radius:10px;background:var(--raised);overflow:auto;margin:0 8px}
.diff pre{margin:0;padding:12px 14px;font-family:"Geist Mono",monospace;font-size:12px;line-height:19px;font-variant-ligatures:none}
.diff .file{color:var(--t1);font-weight:500;display:block;margin-top:8px}
.diff .file:first-child{margin-top:0}
.diff .hunk{color:var(--t3);display:block}
.diff .add{color:var(--ok);background:var(--add-bg);display:block} .diff .del{color:var(--bad);background:var(--del-bg);display:block}
.diff .more{color:var(--t3);display:block;margin-top:6px}

/* the action bar at the foot of the page */
.foot{position:fixed;left:0;right:0;bottom:0;height:44px;display:flex;align-items:center;gap:16px;padding:0 24px;background:var(--raised);border-top:1px solid var(--line);color:var(--t2)}
.foot .cur{display:flex;align-items:center;gap:14px;min-width:0;overflow:hidden;white-space:nowrap}
.foot .cur span{display:inline-flex;gap:6px;align-items:center}
.foot .r{margin-left:auto;display:flex;gap:18px;align-items:center;white-space:nowrap}
.foot .r span{display:inline-flex;gap:8px;align-items:center}
.foot .r .primary{color:var(--t1);font-weight:500}
.foot .sep{width:1px;height:18px;background:var(--line-strong)}
"""

GLYPH = {
    "passed": "<polyline points='2.5,6.5 5,9 9.5,3.5'/>",
    "failed": "<path d='M3 3l6 6M9 3l-6 6'/>",
    "waiting": "<path d='M6 1.8v2.4M6 9.8h0'/><circle cx='6' cy='6' r='4.6'/>",
    "running": "<path d='M4 2.5l5 3.5-5 3.5z'/>",
    "parked": "<circle cx='6' cy='4' r='2.2'/><path d='M2 10.5c.6-2.2 2-3.2 4-3.2s3.4 1 4 3.2'/>",
    "queued": "<path d='M2.5 4h7M2.5 6.5h7M2.5 9h4'/>",
}


def ico(state: str, word: str = "") -> str:
    tile = f'<span class="ico {state}" aria-hidden="true"><svg viewBox="0 0 12 12">{GLYPH[state]}</svg></span>'
    if word:
        return f'<span class="state">{tile}{escape(word)}</span>'
    return tile


def strip(active: str, readout: str, clock: str) -> str:
    tabs = []
    for key, name, n in c.NAV:
        on = ' class="on"' if key == active else ""
        count = f'<span class="n hot">{n}</span>' if n else ""
        tabs.append(f'<a{on} href="#">{escape(name)}{count}</a>')
    return f"""
<header class="strip">
  <span class="brand"><span class="mark"></span>kstrl <span class="t2">{c.PROJECT}</span></span>
  <nav class="tabs" aria-label="views">{"".join(tabs)}</nav>
  <span class="right">
    <span class="state">{ico("passed")}{c.MAST["config"]}</span>
    <span class="state">{ico("passed")}{c.MAST["safe"]}</span>
    <span class="state">{ico("running")}{c.MAST["serve"]}</span>
    <span>{readout}</span>
    <span class="clock">{clock}</span>
  </span>
</header>"""


def command(placeholder: str) -> str:
    return f"""
<div class="cmd" role="search"><span class="caret"></span>{escape(placeholder)}<span class="right"><kbd>⌘K</kbd></span></div>
<div class="hints"><span><kbd>↑</kbd><kbd>↓</kbd> select</span><span><kbd>⏎</kbd> open</span><span><kbd>Esc</kbd> close</span><span class="t3">the lists below stay whole while the field is empty</span></div>"""


def page(
    out: Path, title: str, active: str, readout: str, clock: str, body: str, foot: str
) -> None:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" href="{FONT}">
<style>:root{{{TOKENS}}}{CSS}</style></head>
<body>{strip(active, readout, clock)}<main class="wrap">{body}</main>{foot}</body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(line.rstrip() for line in html.splitlines()) + "\n", encoding="utf-8")


def sec(title: str, body: str, n: str = "", right: str = "") -> str:
    nn = f" <span>{escape(n)}</span>" if n else ""
    rr = f'<span class="r">{right}</span>' if right else ""
    return f'<section class="sec"><div class="st">{escape(title)}{nn}{rr}</div>{body}</section>'


def row(r: c.Row, kind: str, hint: str, on: bool = False, extra: str = "") -> str:
    end = f'<span class="w">{escape(r.action)}</span><kbd>{hint}</kbd>' if r.action else ""
    return f"""<div class="row{" on" if on else ""}">{ico(r.state)}<div><div class="l1">{escape(r.title)}<span class="kind">{escape(kind)}</span></div><div class="l2">{escape(r.detail)}</div>{extra}</div><span class="end">{end}</span></div>"""


def home_body() -> str:
    spent, cap, pct = c.SPEND
    kinds = ["merge gate", "halted run", "failed run"]
    needs = "".join(row(r, kinds[i], "⏎", on=i == 0) for i, r in enumerate(c.NEEDS))
    active = ""
    for i, r in enumerate(c.ACTIVE):
        extra = (
            f'<div class="l2" style="margin-top:3px"><b>{spent}</b> of {cap} cap <span class="meter"><i style="width:{pct}%"></i></span> {pct}%</div>'
            if i == 0
            else ""
        )
        active += row(r, ["factory run", "daemon", "queued"][i], "⏎", extra=extra)
    sha, st, word, why = c.MAIN_AT
    delivery = f'<div class="main-at"><b>main is at <code>{sha}</code></b> <span class="t3">·</span> {ico(st, word)} <span class="t2">· {escape(why)}</span></div>'
    for heading, prs in c.DELIVERY:
        delivery += f'<div class="grp">{escape(heading)}</div>'
        for pr, comp, sha, st, word, why, link in prs:
            more = f' · <a class="link" href="#">{escape(link)}</a>' if link else ""
            delivery += f'<div class="pr">{ico(st)}<span>{pr} <span class="t2">{escape(comp)}</span></span><code>{sha}</code><span>{escape(word)}<div class="why">{escape(why)}{more}</div></span><kbd>⏎</kbd></div>'
    hist = ""
    for st, rid, kind, word, age, comps, tok, cost, note in c.HISTORY:
        on = rid == c.HISTORY_SELECTED
        hist += (
            f"<tr{' class=on' if on else ''}><td><code>{rid}</code></td><td>{kind}</td><td>{ico(st, word)}</td>"
            f'<td class="num t2">{age}</td><td class="num">{escape(comps)}</td><td class="num">{tok}</td>'
            f'<td class="num">{escape(cost)}</td><td class="note">{escape(note)}</td></tr>'
        )
    heads = "".join(
        f"<th{' class=num' if h in ('Last event', 'Components', 'Tokens', 'Cost') else ''}>{h}</th>"
        for h in c.HISTORY_HEAD
    )
    history = f'<table class="data"><thead><tr>{heads}</tr></thead><tbody>{hist}</tbody></table><div class="expand"><b>{c.HISTORY_NOTE_HEAD}</b> <span class="t3">({c.HISTORY_NOTE_SUB})</span><br>{c.HISTORY_NOTE}</div>'
    rail = """
<aside class="rail" aria-label="actions for the selected row">
  <div class="rt"><b>client-commands is waiting for merge approval</b>merge gate · run 8d80e8</div>
  <a class="on" href="#">Decide<kbd>⏎</kbd></a>
  <div class="does">Opens the decision with what is known and the four choices. Nothing happens until you choose.</div>
  <a href="#">Open run 8d80e8<kbd>⌘O</kbd></a>
  <a href="#">Open branch<kbd>⌘B</kbd></a>
  <hr>
  <a href="#">Snooze 24 hours<kbd>⌘S</kbd></a>
  <div class="does">Hides this item for 24 hours; it returns to this list after that. ks serve admits no new work while a merge is parked.</div>
</aside>"""
    return f"""
{command("Find a run, decision or action")}
<div class="head"><h1>{c.PROJECT}</h1><p class="sub">{c.HOME_SUBTITLE}</p></div>
<div class="grid">
  <div>
    {sec("Needs you", needs, "3", '<a href="#">2 decisions</a> · <a href="#">1 failure</a>')}
    {sec("Active", active, "3")}
    {sec("Delivery", delivery, right="is main green after the merges")}
    {sec("History", history, "9 runs", "percentages round up · select a row to read its whole note")}
  </div>
  {rail}
</div>"""


def checkpoint_body() -> str:
    kv = ""
    for label, st, word, rest in c.CHECKS:
        val = f"{ico(st, word)} <span class='t2'>· {rest}</span>" if st else rest
        kv += f"<div>{escape(label)}</div><div>{val}</div>"
    keys = ["⌘⏎", "⌘⇧⏎", "⌘⌫", "⌘R", "Esc"]
    acts = ""
    for i, (label, kind, _n, does) in enumerate(c.CHOICES):
        acts += f'<div class="act{" on" if i == 0 else ""}"><span class="lab {kind}">{label}</span><kbd>{keys[i]}</kbd><div class="does">{does}</div></div>'
    findings = "".join(
        f'<div class="finding">{ico("waiting" if s == "advisory" else "running")}<span class="t2">{p}</span><span><span class="sev {s}">{s}</span></span><span>{escape(f)}<code class="where">{escape(w)}</code></span></div>'
        for p, s, w, f in c.FINDINGS
    )
    diff = "".join(f'<span class="{k}">{escape(t)}</span>' for k, t in c.DIFF)
    return f"""
{command("Type a choice, or search this checkpoint")}
<div class="head"><h1>{c.CP_TITLE}</h1><p class="sub">{c.CP_SUBTITLE}</p></div>
<div class="cp">
  <div>
    {sec("Checks before this point", f'<div class="kv">{kv}</div>', right="every gate that ran on this component")}
    {sec("Findings", findings, "3", "what review and security listed")}
    {sec("Diff", f'<div class="diff"><pre>{diff}</pre></div>', right=c.DIFF_SUMMARY)}
  </div>
  <div class="side">
    {sec("Your decision", f'<div class="acts">{acts}</div>', right="each choice says what it does")}
  </div>
</div>"""


def build_all() -> None:
    d = HERE / "c-command"
    spent, cap, pct = c.SPEND
    page(
        d / "home.html",
        "Home",
        "home",
        f"<b>{spent}</b> of {cap} · {pct}%",
        c.CLOCK,
        home_body(),
        """<footer class="foot"><span class="cur"><span><kbd>↑</kbd><kbd>↓</kbd> move between rows</span><span><kbd>⏎</kbd> the row's first action</span><span><kbd>Tab</kbd> next section</span></span>
<span class="r"><span>All actions for the selected row <kbd>⌘K</kbd></span></span></footer>""",
    )
    page(
        d / "checkpoint.html",
        "Checkpoint decision",
        "runs",
        f"<b>{c.CP_SPEND}</b> · no cost cap",
        c.CP_CLOCK,
        checkpoint_body(),
        """<footer class="foot"><span class="cur"><span><kbd>↑</kbd><kbd>↓</kbd> move between choices</span><span><kbd>⏎</kbd> the focused choice, after a confirm</span><span>every direct shortcut is chorded</span></span>
<span class="r"><span>All choices <kbd>⌘K</kbd></span></span></footer>""",
    )
