/* kstrl web UI, round 2 prototype.
   Command first: everything is reachable from the field at the top. The
   routing behind the field is a replay of real Jev answers (replay.js); the
   page never invents a route. Details float (sheet, HUD, made views);
   decisions always pass through a confirmation that states the consequence. */
(function () {
  "use strict";
  const D = window.DATA;
  const REPLAY = window.JEV_REPLAY || [];
  const COVERAGE = window.JEV_COVERAGE || [];
  const SUMMARY = window.JEV_SUMMARY || {};
  const THRESHOLD = 0.8; // measured: lowest route confidence with no wrong routing kept (jev-bench/README.md)
  const COVER_HIGH = 0.5, COVER_LOW = 0.15; // measured in coverage_bench.py

  // ------------------------------------------------------------ helpers
  const $ = (s, r) => (r || document).querySelector(s);
  function h(tag, attrs, ...kids) {
    const el = tag.startsWith("svg:") ? document.createElementNS("http://www.w3.org/2000/svg", tag.slice(4)) : document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.setAttribute("class", v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const set = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
  const money = (n) => (n == null ? "·" : "$" + n.toFixed(2));
  const ICON = {
    check: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3 3 7-7"/></svg>',
    cross: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
    clock: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/></svg>',
    person: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="5.5" r="2.5"/><path d="M3 13.5c0-2.5 2.2-4 5-4s5 1.5 5 4"/></svg>',
    arrow: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h10M9 4l4 4-4 4"/></svg>',
    ask: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M6.2 6.3a1.9 1.9 0 1 1 2.6 1.8c-.6.3-.8.6-.8 1.2"/><circle cx="8" cy="11.5" r=".5" fill="currentColor"/></svg>',
    act: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2L3.5 9H8l-1 5L12.5 7H8z"/></svg>',
    view: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><rect x="2" y="3" width="12" height="10" rx="2"/><path d="M2 7h12M6 7v6"/></svg>',
    pin: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2l4.5 4.5-2 .5-2.5 2.5.5 3-2-1.5L5 14l-3-3 3-3-1.5-2 3 .5L9 4z"/></svg>',
    close: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
    sun: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/></svg>',
    moon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z"/></svg>',
    none: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M4 12l8-8"/></svg>',
    plus: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M8 3v10M3 8h10"/></svg>',
  };
  const icon = (n, cls) => h("span", { class: cls || "glyph", html: ICON[n] });

  // ------------------------------------------------------------- state
  const S = { theme: "dark", sheet: null, hud: null, sel: null, confirm: null, views: [], choiceSel: 0, cmdSel: 0, focus: "overview" };
  const params = new URLSearchParams(location.hash.slice(1));
  S.theme = params.get("theme") || "dark";
  document.documentElement.dataset.theme = S.theme;

  // ---------------------------------------------------------- telemetry
  function renderTelemetry() {
    const t = $("#telemetry");
    t.replaceChildren(
      h("div", { class: "tele serve" }, h("span", { class: "lamp running" }), h("span", {}, "ks serve")),
      h("div", { class: "tele" }, h("b", { class: "num" }, money(D.live.spend.amount)), h("span", { class: "num" }, "of " + money(D.live.spend.cap)),
        h("span", { class: "meter" }, h("i", { style: `width:${D.live.spend.pct}%` })), h("span", { class: "num" }, D.live.spend.pct + "%")),
      h("div", { class: "tele clock-wrap" }, h("span", { class: "clock num" }, D.live.elapsed)),
      h("button", { class: "icon-btn", title: "Switch theme", onclick: () => setTheme(S.theme === "dark" ? "light" : "dark"), html: S.theme === "dark" ? ICON.sun : ICON.moon })
    );
  }
  function setTheme(t) { S.theme = t; document.documentElement.dataset.theme = t; renderTelemetry(); }

  // ------------------------------------------------------------- needs
  function renderNeeds() {
    const r = $("#needs");
    r.replaceChildren(
      h("div", { class: "region-head" }, h("h2", {}, "Needs you", h("span", { class: "count num" }, D.needs.length)), h("span", { class: "q" }, "3 decisions · 1 retry")),
      ...D.needs.map((n, i) => h("button", { class: "item" + (S.sel === n.id ? " sel" : ""), onclick: () => openItem(n.id) },
        h("span", { class: "glyph-state " + n.state, html: n.state === "failed" ? ICON.cross : n.state === "waiting" ? ICON.clock : ICON.person }),
        h("span", {}, h("div", { class: "t" }, n.title), h("div", { class: "s" }, n.sub)),
        h("span", { class: "k" }, h("span", { class: "num" }, n.age), h("kbd", {}, i + 1))
      ))
    );
  }
  function openItem(id) {
    S.sel = id;
    if (id === "checkpoint-comp-c") openSheet(sheetCheckpoint());
    else if (id === "gate-client-commands") openSheet(sheetGate());
    else if (id === "halt-fda682") openSheet(sheetHalt());
    else if (id === "fail-client-commands") openSheet(sheetFailure());
    renderNeeds();
  }

  // --------------------------------------------------------------- run
  const PH = D.live.phases;
  function ringSVG(done, total, running) {
    const r = 24, c = 2 * Math.PI * r, f = done / total;
    return h("div", { class: "ring" },
      h("svg:svg", { viewBox: "0 0 56 56" },
        h("svg:circle", { class: "track", cx: 28, cy: 28, r }),
        h("svg:circle", { class: "arc", cx: 28, cy: 28, r, "stroke-dasharray": `${c * f} ${c}` }),
        running ? h("svg:circle", { class: "arc run", cx: 28, cy: 28, r, "stroke-dasharray": `${c / total * 0.55} ${c}`, "stroke-dashoffset": -c * f }) : null),
      h("div", { class: "lbl" }, `${done}/${total}`));
  }
  function nodeRing(comp, cx, cy) {
    // seven segments, one per phase, drawn as arcs around a small circle
    const g = h("svg:g", {});
    const r = 13, gap = 0.06, per = (2 * Math.PI) / PH.length;
    comp.phases.forEach((st, i) => {
      const a0 = -Math.PI / 2 + i * per + gap, a1 = -Math.PI / 2 + (i + 1) * per - gap;
      const p = `M${cx + r * Math.cos(a0)},${cy + r * Math.sin(a0)} A${r},${r} 0 0 1 ${cx + r * Math.cos(a1)},${cy + r * Math.sin(a1)}`;
      g.append(h("svg:path", { class: "seg" + (st === 1 ? " on" : st === 2 ? " run" : st === 3 ? " fail" : ""), d: p }));
    });
    if (comp.state === "running") g.append(h("svg:circle", { class: "ring-run", cx, cy, r: 7, fill: "none", stroke: "var(--accent)", "stroke-width": 2, "stroke-dasharray": "6 38" }));
    if (comp.state === "completed") g.append(h("svg:path", { d: `M${cx - 4},${cy} l3 3 5-6`, fill: "none", stroke: "var(--pass)", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }));
    if (comp.state === "failed") g.append(h("svg:path", { d: `M${cx - 3.5},${cy - 3.5} l7 7 M${cx + 3.5},${cy - 3.5} l-7 7`, fill: "none", stroke: "var(--fail)", "stroke-width": 1.8, "stroke-linecap": "round" }));
    return g;
  }
  function schematic() {
    const comps = D.live.components;
    const W = 160, H = 44, colW = 188, rowH = 66;
    const tiers = {};
    comps.forEach((c) => (tiers[c.tier] = tiers[c.tier] || []).push(c));
    const nt = Object.keys(tiers).length;
    const maxRows = Math.max(...Object.values(tiers).map((a) => a.length));
    const width = nt * colW - (colW - W) + 24, height = maxRows * rowH + 8;
    const pos = {};
    comps.forEach((c) => {
      const col = tiers[c.tier], idx = col.indexOf(c);
      const x = 12 + c.tier * colW, y = 8 + (maxRows - col.length) * rowH / 2 + idx * rowH;
      pos[c.id] = { x, y };
    });
    const svg = h("svg:svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "components of run live01 by tier, with a seven-segment ring of phases on each" });
    comps.forEach((c) => c.deps.forEach((d) => {
      const a = pos[d], b = pos[c.id];
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
      svg.append(h("svg:path", { class: "edge" + (c.state === "running" ? " active" : ""), d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` }));
    }));
    comps.forEach((c) => {
      const p = pos[c.id];
      const g = h("svg:g", { class: "node" + (S.sel === "comp-" + c.id ? " sel" : ""), transform: `translate(${p.x},${p.y})`, tabindex: 0, role: "button", onclick: () => openComponent(c.id), onkeydown: (e) => { if (e.key === "Enter") openComponent(c.id); } });
      g.append(h("svg:rect", { class: "body", width: W, height: H, rx: 9 }));
      g.append(nodeRing(c, 24, H / 2));
      g.append(h("svg:text", { x: 46, y: 18 }, c.id));
      const sub = c.state === "running" ? `${D.live.now.phase} · ${c.iter} of ${D.live.now.iterations}` : c.state === "completed" ? `${money(c.cost)} · ${c.time}` : c.state === "pending" ? "waiting" : c.state;
      g.append(h("svg:text", { class: "sub", x: 46, y: 33 }, sub));
      svg.append(g);
    });
    return svg;
  }
  function compList() {
    return h("div", { class: "comp-list" }, ...D.live.components.map((c) => {
      const svg = h("svg:svg", { viewBox: "0 0 34 34" }); svg.append(nodeRing(c, 17, 17));
      const sub = c.state === "running" ? `${D.live.now.phase} · ${c.iter} of ${D.live.now.iterations}` : c.state === "completed" ? `${money(c.cost)} · ${c.time}` : "waiting on " + c.deps.join(", ");
      return h("button", { class: "crow", onclick: () => openComponent(c.id) }, svg, h("span", {}, h("div", { class: "n" }, c.id), h("div", { class: "s" }, sub)), h("span", { class: "dim", style: "font-size:12px" }, "tier " + c.tier));
    }));
  }
  function phaseTimeline() {
    const total = 64; // minutes of run so far
    const tl = h("div", { class: "timeline" });
    D.live.components.forEach((c) => {
      const spans = D.live.spans[c.id] || [];
      const track = h("div", { class: "tl-track" });
      spans.forEach(([ph, a, b]) => track.append(h("i", { class: ph + (c.state === "running" && ph === "engineer" ? " live" : ""), style: `left:${(a / total) * 100}%;width:${((b - a) / total) * 100}%`, title: `${ph} ${a}–${b} min` })));
      tl.append(h("div", { class: "tl-row" }, h("div", { class: "n" + (spans.length ? "" : " dim") }, c.id), track));
    });
    tl.append(h("div", { class: "tl-axis" }, h("span", {}, "16:08"), h("span", {}, "+20m"), h("span", {}, "+40m"), h("span", {}, "now 17:12")));
    tl.append(h("div", { class: "tl-legend" }, h("span", {}, h("i", { class: "engineer", style: "background:color-mix(in srgb,var(--ink3) 55%,transparent)" }), "engineer"), h("span", {}, h("i", { style: "background:color-mix(in srgb,var(--park) 60%,transparent)" }), "review, security"), h("span", {}, h("i", { style: "background:var(--pass)" }), "PR merged"), h("span", {}, h("i", { style: "background:var(--accent)" }), "running now")));
    return tl;
  }
  function renderRun() {
    const r = $("#run"), L = D.live, n = L.now;
    set(r,
      h("div", { class: "region-head" }, h("h2", {}, "factory ", h("span", { class: "mono" }, L.id), h("span", { class: "count" }, "running · started " + L.started)),
        h("button", { class: "q", onclick: () => setFocus(S.focus === "run" ? "overview" : "run") }, S.focus === "run" ? "back to overview" : "open the run")),
      h("div", { class: "run-head" },
        ringSVG(L.done, L.total, true),
        h("div", { class: "run-facts" },
          h("div", { class: "fact" }, h("div", { class: "v" }, h("span", { class: "lamp running" }), n.component), h("div", { class: "l" }, `${n.phase} · iteration ${n.iteration} of ${n.iterations}`)),
          h("div", { class: "fact" }, h("div", { class: "v" }, h("span", { class: "lamp passed" }), h("span", { class: "num" }, n.output + " ago")), h("div", { class: "l" }, "last output · stale after " + n.staleAfter)),
          h("div", { class: "fact" }, h("div", { class: "v" }, h("span", { class: "lamp passed" }), "worker alive"), h("div", { class: "l" }, `pid ${n.pid} · checked ${n.checked} ago`)),
          h("div", { class: "fact" }, h("div", { class: "v" }, h("span", { class: "num" }, money(L.spend.amount)), h("span", { class: "meter" }, h("i", { style: `width:${L.spend.pct}%` })), h("span", { class: "num muted" }, L.spend.pct + "%")), h("div", { class: "l" }, `of ${money(L.spend.cap)} cap · ${L.spend.tokens} tokens · whole amount`)),
          h("div", { class: "fact" }, h("div", { class: "v num" }, L.elapsed), h("div", { class: "l" }, "elapsed · last event " + L.lastEvent + " ago")))),
      h("div", { class: "schematic" }, window.innerWidth <= 720 ? compList() : schematic(),
        h("div", { class: "legend" }, h("span", {}, h("span", { class: "lamp passed" }), "completed"), h("span", {}, h("span", { class: "lamp running" }), "running"), h("span", {}, h("span", { class: "lamp pending" }), "waiting on a dependency"), h("span", { class: "phases-note" }, "ring: " + PH.join(" · ")))),
      phaseTimeline()
    );
  }
  function setFocus(f) { S.focus = f; renderRun(); renderHist(); }
  function openComponent(id) {
    S.sel = "comp-" + id;
    const c = D.live.components.find((x) => x.id === id);
    if (id === "client-commands") return openSheet(sheetFailure());
    if (!c) return toast("No component " + id + " in the live run");
    openSheet(sheetComponent(c));
    renderRun();
  }

  // -------------------------------------------------------------- main
  function stateWord(s) { return { passed: "passed", failed: "failed", unknown: "unknown", unread: "not read yet" }[s] || s; }
  function renderMain() {
    const r = $("#main"), d = D.delivery;
    r.replaceChildren(
      h("div", { class: "region-head" }, h("h2", {}, "Main"), h("button", { class: "q", onclick: () => openSheet(sheetDelivery()) }, "delivery")),
      h("div", { class: "main-at" }, h("span", { class: "sq " + d.mainState }), h("span", { class: "big" }, "at ", h("code", {}, d.mainAt)), h("span", { class: "word " + d.mainState }, "CI " + d.mainState)),
      h("div", { class: "dim", style: "font-size:12px;margin:-6px 0 8px" }, d.mainReason + " · read " + d.read + " ago"),
      h("div", { class: "ladder" }, ...d.merges.map((m) => h("button", { class: "rung", onclick: () => openSheet(sheetDelivery(m.pr)) },
        h("span", { class: "sq " + m.state }),
        h("span", { class: "t" }, h("span", { class: "c" }, m.component), h("code", {}, m.commit)),
        h("span", { class: "w word " + m.state }, stateWord(m.state))))),
      h("div", { class: "sub-region" },
        h("div", { class: "region-head" }, h("h2", {}, "Serve"), h("button", { class: "q", onclick: () => openSheet(sheetServe()) }, "queue")),
        h("div", { class: "serve-line" }, h("span", { class: "lamp running" }), h("span", {}, "daemon alive"), h("span", { class: "dim num" }, "checked " + D.serve.checked + " ago")),
        h("div", { class: "serve-line" }, h("span", { class: "pill" }, h("span", { class: "lamp running" }), "slice 3"), h("span", { class: "pill" }, h("span", { class: "lamp queued" }), "slice 4 queued")))
    );
  }

  // ----------------------------------------------------------- history
  function renderHist() {
    const r = $("#hist");
    const runs = D.history;
    const max = Math.max(...runs.map((x) => x.cost || 0));
    const W = 720, Hh = 120, bw = 44, gap = (W - runs.length * bw) / (runs.length - 1);
    const svg = h("svg:svg", { viewBox: `0 0 ${W} ${Hh}`, role: "img", "aria-label": "cost of each of the last nine runs, coloured by state" });
    runs.slice().reverse().forEach((x, i) => {
      const hgt = x.cost ? Math.max(6, (x.cost / max) * 76) : 6;
      const xx = i * (bw + gap);
      const g = h("svg:g", { class: "bar " + (x.cost ? x.state : "none") + (S.sel === "run-" + x.id ? " sel" : ""), onclick: () => { S.sel = "run-" + x.id; renderHist(); openSheet(sheetRun(x)); } });
      g.append(h("svg:rect", { x: xx, y: 84 - hgt, width: bw, height: hgt }));
      g.append(h("svg:text", { x: xx + bw / 2, y: 100, "text-anchor": "middle" }, x.id));
      g.append(h("svg:text", { x: xx + bw / 2, y: 76 - hgt, "text-anchor": "middle" }, x.cost ? "$" + x.cost.toFixed(0) : "·"));
      svg.append(g);
    });
    const sel = runs.find((x) => "run-" + x.id === S.sel);
    r.replaceChildren(
      h("div", { class: "region-head" }, h("h2", {}, "History", h("span", { class: "count num" }, runs.length + " runs")), h("span", { class: "q" }, "cost per run, oldest left · live feed")),
      h("div", { class: "hist-wrap" },
        h("div", { class: "bars" }, svg, h("div", { class: "bar-note" }, sel ? h("span", {}, h("b", { class: "mono" }, sel.id), " · ", h("span", { class: "word " + sel.state }, sel.state), " · ", sel.comps, sel.note ? " · " + sel.note : "") : "select a bar for the run's state and note")),
        h("div", { class: "ticker" }, ...D.live.feed.slice(S.focus === "run" ? -9 : -6).map(([t, c, k, m]) => h("div", { class: "row " + k }, h("span", { class: "t" }, t), h("span", { class: "c" }, c), h("span", { class: "m" }, m))))));
  }

  // ------------------------------------------------------------- sheet
  function openSheet(node) {
    closeHud();
    S.confirm = null; S.choiceSel = 0;
    const root = $("#sheet-root");
    root.replaceChildren(node);
    S.sheet = node;
    const first = node.querySelector(".choice, .btn");
    if (first) first.focus({ preventScroll: true });
  }
  function closeSheet() { $("#sheet-root").replaceChildren(); S.sheet = null; S.confirm = null; S.sel = null; renderNeeds(); renderRun(); }
  function sheetShell(title, sub, body, foot) {
    return h("aside", { class: "sheet", role: "dialog", "aria-label": title },
      h("div", { class: "sheet-head" }, h("div", { style: "flex:1;min-width:0" }, h("h1", {}, title), sub ? h("div", { class: "sub" }, ...[].concat(sub)) : null), h("button", { class: "icon-btn", "aria-label": "Close", onclick: closeSheet, html: ICON.close })),
      h("div", { class: "sheet-body" }, ...body),
      foot ? h("div", { class: "sheet-foot" }, ...[].concat(foot)) : null);
  }
  function gates(gs) { return h("div", { class: "gates" }, ...gs.map((g) => h("span", { class: "gate " + g.state }, h("span", { html: g.state === "passed" ? ICON.check : ICON.cross }), h("span", { class: "n" }, g.name), h("span", { class: "d" }, g.note)))); }
  function findings(fs) { return h("div", {}, ...fs.map((f) => h("div", { class: "finding" }, h("span", { class: "sev " + f.severity }, f.severity[0].toUpperCase()), h("div", {}, h("div", { class: "ft" }, f.text), h("div", { class: "fm" }, h("span", {}, f.phase + " · " + f.severity), h("code", {}, f.where)))))); }
  function choiceList(choices, onPick) {
    return h("div", { class: "choices", role: "radiogroup" }, ...choices.map((c, i) => h("button", { class: "choice " + c.kind + (S.choiceSel === i ? " sel" : ""), onclick: () => onPick(c), onfocus: () => { S.choiceSel = i; } },
      h("span", { class: "l" }, c.label), h("kbd", {}, c.key),
      h("span", { class: "does" }, h("b", {}, c.does), " ", c.then))));
  }
  function confirmCard(title, does, then, extra, onYes) {
    const card = h("div", { class: "confirm", role: "alertdialog" }, h("div", { class: "confirm-card" },
      h("h2", {}, title), h("p", {}, h("b", {}, does), " ", then), extra ? h("div", { class: "cmdline" }, extra) : null,
      h("div", { class: "btn-row" }, h("button", { class: "btn primary", onclick: onYes }, "Confirm", h("kbd", {}, "↵")), h("button", { class: "btn", onclick: cancelConfirm }, "Cancel", h("kbd", {}, "esc"))), h("div", { class: "note" }, "Nothing runs before Confirm. Esc returns to the evidence.")));
    S.confirm = card;
    S.sheet.append(card);
    card.querySelector(".btn").focus();
    return card;
  }
  function cancelConfirm() { if (S.confirm) { S.confirm.remove(); S.confirm = null; } }

  function sheetCheckpoint(pre) {
    const c = D.checkpoint;
    const adds = c.files.reduce((a, f) => a + f.add, 0), dels = c.files.reduce((a, f) => a + f.del, 0);
    const maxLines = Math.max(...c.files.map((f) => f.add + f.del));
    const pick = (ch) => confirmCard(ch.label, ch.does, ch.then, null, () => { closeSheet(); toast(ch.label + " recorded for comp-c", "passed"); });
    const sh = sheetShell(c.title, [h("span", { class: "lamp parked" }), "checkpoint · the run waits on this answer · asked " + c.asked + " ago"], [
      h("h3", {}, "Gates before this point", h("span", {}, "branch " + c.branch)), gates(c.gates),
      h("h3", {}, "Changed files", h("span", { class: "num" }, `${c.files.length} · +${adds} −${dels}`)),
      h("div", { class: "files" }, ...c.files.map((f) => h("div", { class: "file" }, h("code", {}, f.path), h("span", { class: "mm" }, h("i", { class: "a", style: `width:${(f.add / maxLines) * 100}%` }), f.del ? h("i", { class: "d", style: `width:${(f.del / maxLines) * 100}%` }) : null), h("span", { class: "pm num" }, h("span", { class: "a" }, "+" + f.add), f.del ? h("span", { class: "d" }, "−" + f.del) : null)))),
      h("h3", {}, "Findings", h("span", {}, "2 advisory · 1 low · none blocking")), findings(c.findings),
      h("h3", {}, "Spend so far"), h("div", { class: "gates" }, h("span", { class: "gate" }, h("span", { class: "n num" }, c.spend), h("span", { class: "d" }, "no cost cap · lower bound: some calls did not report a cost"))),
      h("h3", {}, "Diff", h("span", {}, "first two files whole; the third is 201 added lines")),
      h("div", { class: "diff" }, ...c.diff.map(([k, t]) => h("div", { class: k }, t)))
    ], [h("h3", {}, "Your decision", h("span", {}, "each choice says what it does · press its number")), choiceList(c.choices, pick)]);
    if (pre) setTimeout(() => { const ch = c.choices.find((x) => x.id === pre); if (ch) pick(ch); }, 0);
    return sh;
  }
  function sheetGate(pre) {
    const g = D.mergeGate;
    const pick = (ch) => confirmCard(ch.label, ch.does, ch.then, "same as: ks inbox " + (ch.id.startsWith("approve") ? "approve" : ch.id === "reject" ? "reject" : "snooze") + " " + g.id, () => { closeSheet(); toast(ch.label + " recorded for client-commands", "passed"); });
    const sh = sheetShell(g.title, [h("span", { class: "lamp parked" }), "merge gate · run " + g.run + " · branch " + g.branch + " at " + g.head], [
      h("h3", {}, "What is known"), gates(g.gates), h("div", { class: "muted", style: "margin-top:8px;font-size:12.5px" }, g.pr + " · the branch holds reviewed work; nothing is pushed until you decide"),
      h("h3", {}, "Your decision", h("span", {}, "each choice says what it does")), choiceList(g.choices, pick)
    ]);
    if (pre) setTimeout(() => { const ch = g.choices.find((x) => x.id === pre); if (ch) pick(ch); }, 0);
    return sh;
  }
  function sheetHalt() {
    const x = D.halt;
    return sheetShell(x.title, [h("span", { class: "lamp waiting" }), "halted run " + x.run + " · " + x.project], [
      h("h3", {}, "Rounds", h("span", {}, "the merged feature checked as one tree")),
      h("div", { class: "rounds" }, ...x.rounds.map((r) => h("div", { class: "round" }, h("span", { class: "lamp " + r.state }), h("span", {}, "round " + r.n), h("span", {}, r.text), h("span", { class: "at num" }, r.at)))),
      h("h3", {}, "Findings", h("span", {}, "counts agree with round 3")),
      h("div", { class: "counts" }, h("span", { class: "chip" }, h("b", {}, x.findings.open), "open"), h("span", { class: "chip" }, h("b", {}, x.findings.handedOff), "handed off"), h("span", { class: "chip" }, h("b", {}, x.findings.fixed), "fixed")),
      h("div", { class: "muted", style: "margin-top:8px;font-size:12.5px" }, "integration-fix-1 failed review and the fix budget (1) is spent. The run stopped rather than merge with open findings.")
    ], [h("h3", {}, "Your decision"), choiceList(x.choices, (ch) => confirmCard(ch.label, ch.does, ch.then, null, () => { closeSheet(); toast(ch.label, "passed"); }))]);
  }
  function sheetFailure(pre) {
    const f = D.failure, s = f.scope;
    const retry = () => confirmCard("Retry client-commands", "Resets client-commands to pending and runs the engineer again, then every gate after it.", "Runs under " + s.limits + ".", "will run as: " + s.command, () => { closeSheet(); toast("Retry started · appears under the live run", "running"); });
    const sh = sheetShell("client-commands failed at verify", [h("span", { class: "lamp failed" }), "run " + f.run + " · 2 attempts · " + f.cause], [
      h("h3", {}, "Attempts"),
      h("div", { class: "attempts" }, ...f.attempts.map((a, i) => h("div", { class: "attempt" }, h("span", { class: "dim" }, "attempt " + (i + 1)), h("span", { class: "steps" }, ...a.map(([ph, st, d]) => h("span", { class: "step" }, h("span", { class: "lamp " + st }), ph, h("span", { class: "d num" }, d))), h("span", { class: "step skipped", title: "diff, review, security, distill and pr did not run" }, "5 gates did not run"))))),
      h("h3", {}, "Evidence", h("span", {}, "last 6 of 35 lines")), h("div", { class: "out" }, ...f.output.map((l) => h("div", { class: /^(E |FAILED|=)/.test(l) ? "e" : "" }, l))), h("div", { class: "path", style: "margin-top:6px" }, f.evidence),
      h("h3", {}, "What a retry does", h("span", {}, "stated before it is offered")),
      h("div", { class: "scope" }, h("div", {}, h("h4", {}, "Resets"), h("ul", {}, ...s.resets.map((t) => h("li", {}, t)))), h("div", {}, h("h4", {}, "Keeps"), h("ul", {}, ...s.keeps.map((t) => h("li", {}, t))))),
      h("div", { class: "muted", style: "font-size:12.5px;margin-top:8px" }, "Runs under " + s.limits + ". Not repeated: " + s.notRepeated + "."),
      h("div", { class: "btn-row" }, h("button", { class: "btn primary", onclick: retry }, "Retry", h("kbd", {}, "R")), h("code", { class: "dim" }, s.command))
    ]);
    if (pre) setTimeout(retry, 0);
    return sh;
  }
  function sheetComponent(c) {
    const running = c.state === "running";
    return sheetShell(c.id, [h("span", { class: "lamp " + c.state }), c.state + " · tier " + c.tier + " · " + c.what], [
      h("h3", {}, "Phases"), h("div", { class: "gates" }, ...PH.map((p, i) => h("span", { class: "gate " + (c.phases[i] === 1 ? "passed" : "") }, h("span", { class: "lamp " + (c.phases[i] === 1 ? "passed" : c.phases[i] === 2 ? "running" : "pending") }), h("span", { class: "n" }, p)))),
      h("dl", { class: "kv", style: "margin-top:14px" }, h("dt", {}, "depends on"), h("dd", {}, c.deps.length ? c.deps.join(", ") : "nothing"), h("dt", {}, "attempts"), h("dd", { class: "num" }, c.tries ?? "·"), h("dt", {}, "iterations"), h("dd", { class: "num" }, running ? `${c.iter} of ${D.live.now.iterations}` : c.iter ?? "·"), h("dt", {}, "time"), h("dd", { class: "num" }, c.time ?? "·"), h("dt", {}, "tokens"), h("dd", { class: "num" }, c.tokens ?? "not yet reported"), h("dt", {}, "cost"), h("dd", { class: "num" }, c.cost != null ? money(c.cost) : "not yet reported")),
      running ? h("div", { style: "margin-top:14px" }, h("h3", {}, "Liveness"), h("div", { class: "gates" }, h("span", { class: "gate passed" }, h("span", { html: ICON.check }), h("span", { class: "n" }, "output " + D.live.now.output + " ago"), h("span", { class: "d" }, "stale after " + D.live.now.staleAfter)), h("span", { class: "gate passed" }, h("span", { html: ICON.check }), h("span", { class: "n" }, "worker " + D.live.now.pid), h("span", { class: "d" }, "checked " + D.live.now.checked + " ago")))) : null,
      h("h3", {}, "Findings"), D.findings.filter((f) => f.component === c.id).length ? findings(D.findings.filter((f) => f.component === c.id).map((f) => ({ ...f, text: f.kind.replace(/_/g, " ") + " (" + f.disposition + ")" }))) : h("div", { class: "dim" }, "none recorded yet")
    ]);
  }
  function sheetRun(x) {
    return sheetShell((x.kind + " ") , [h("span", { class: "mono" }, x.id), h("span", { class: "lamp " + x.state }), x.state + " · last event " + x.when + " ago"], [
      h("dl", { class: "kv", style: "margin-top:6px" }, h("dt", {}, "components"), h("dd", {}, x.comps), h("dt", {}, "tokens"), h("dd", { class: "num" }, x.tokens ?? "·"), h("dt", {}, "cost"), h("dd", { class: "num" }, x.cost != null ? money(x.cost) + (x.cap ? ` of ${money(x.cap)} · ${Math.ceil((x.cost / x.cap) * 100)}%` : " · no cap") : "·")),
      x.note ? h("div", {}, h("h3", {}, "Note"), h("div", { style: "font-size:13px;line-height:19px" }, x.note)) : null
    ]);
  }
  function sheetDelivery(prSel) {
    const d = D.delivery;
    return sheetShell("Is main green", [h("span", { class: "lamp unknown" }), "main at " + d.mainAt + " · CI unknown · " + d.mainReason], [
      h("h3", {}, "Merges", h("span", {}, "CI state per merge commit · read " + d.read + " ago")),
      h("div", {}, ...d.merges.map((m) => h("div", { class: "finding", style: prSel === m.pr ? "background:var(--accent-soft);margin:0 -8px;padding:7px 8px;border-radius:8px" : "" }, h("span", { class: "sq " + m.state, style: "margin:4px 5px" }), h("div", {}, h("div", { class: "ft" }, h("b", {}, "PR #" + m.pr + " " + m.component), " ", h("code", { class: "dim" }, m.commit), " · ", h("span", { class: "word " + m.state }, stateWord(m.state))), h("div", { class: "fm" }, m.reason, m.read != null ? " · read " + m.read + " ago" : ""), m.state === "unread" ? h("div", { class: "btn-row", style: "margin-top:6px" }, h("button", { class: "btn", style: "height:28px", onclick: () => toast("ks ci poll started", "running") }, "Read now"), h("span", { class: "demo-note" }, "runs ks ci poll")) : null)))),
      h("div", { class: "muted", style: "font-size:12.5px;margin-top:12px" }, "A state kstrl could not read is recorded as unknown, never as passed.")
    ]);
  }
  function sheetServe() {
    return sheetShell("ks serve", [h("span", { class: "lamp running" }), "daemon alive · pid " + D.serve.pid + " · checked " + D.serve.checked + " ago"], [
      h("h3", {}, "Queue"),
      h("div", { class: "v-list" }, h("div", { class: "r" }, h("span", { class: "lamp running" }), h("span", {}, D.serve.running, h("span", { class: "m" }, " · runs live01")), h("span", { class: "m" }, "running")), ...D.serve.queued.map((q) => h("div", { class: "r" }, h("span", { class: "lamp queued" }), h("span", {}, q), h("span", { class: "m" }, "queued · starts when live01 finishes and every admission check passes")))),
      h("h3", {}, "Admission checks"), gates([{ name: "factory lock", state: "passed", note: "held by live01; next waits" }, { name: "budget", state: "passed", note: "$19.24 of $78.00" }, { name: "parked merges", state: "failed", note: "1 parked · admits no new work" }]),
      h("div", { class: "btn-row" }, h("button", { class: "btn", onclick: () => confirmCard("Pause ks serve", "Stops admitting queued work.", "The running item finishes; nothing new starts until resumed.", "same as: ks queue pause", () => { closeSheet(); toast("ks serve paused", "waiting"); }) }, "Pause"))
    ]);
  }
  function sheetConfig() {
    return sheetShell("Configuration", ["resolved values and where each comes from"], [
      h("div", { class: "v-table", style: "margin-top:10px" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "setting"), h("th", {}, "value"), h("th", {}, "source"))), h("tbody", {}, ...D.config.map((c) => h("tr", {}, h("td", {}, c.label, h("div", { class: "dim" }, h("code", {}, c.key))), h("td", { class: "num" }, c.value), h("td", { class: "dim" }, c.source))))))
    ]);
  }
  function sheetLearning() {
    return sheetShell("Learning", ["recurring failure patterns across runs"], [
      h("h3", {}, "Patterns"), h("div", { class: "v-list" }, h("div", { class: "r" }, h("span", { class: "lamp failed" }), h("span", {}, "verify: tests fail on a token length assertion", h("span", { class: "m" }, " · token-crypto, client-commands")), h("span", { class: "m" }, "2 runs")), h("div", { class: "r" }, h("span", { class: "lamp unknown" }), h("span", {}, "stale component branches refuse the run", h("span", { class: "m" }, " · 1490e8, e3e393")), h("span", { class: "m" }, "2 runs"))),
      h("h3", {}, "Readiness"), h("div", { class: "muted", style: "font-size:12.5px" }, "9 runs recorded; 5 finished. Lessons need 3 finished runs with the same pattern before they are proposed.")
    ]);
  }
  function sheetDecisions() {
    return sheetShell("Needs you", [D.needs.length + " items"], [h("div", { style: "margin-top:8px" }, ...D.needs.map((n) => h("button", { class: "item", onclick: () => openItem(n.id) }, h("span", { class: "glyph-state " + n.state, html: n.state === "failed" ? ICON.cross : n.state === "waiting" ? ICON.clock : ICON.person }), h("span", {}, h("div", { class: "t" }, n.title), h("div", { class: "s" }, n.sub + " · " + n.detail)), h("span", { class: "k" }, n.action))))]);
  }
  function sheetHistory() {
    return sheetShell("History", [D.history.length + " runs · percentages round up"], [h("div", { class: "v-table", style: "margin-top:10px" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "run"), h("th", {}, "state"), h("th", {}, "components"), h("th", { class: "num" }, "cost"))), h("tbody", {}, ...D.history.map((x) => h("tr", {}, h("td", {}, h("code", {}, x.id), h("span", { class: "dim" }, " " + x.kind)), h("td", {}, h("span", { class: "lamp " + x.state, style: "margin-right:6px" }), x.state), h("td", {}, x.comps), h("td", { class: "num" }, x.cost != null ? money(x.cost) : "·"))))))]);
  }

  // --------------------------------------------------------------- HUD
  function openHud(node) { closeHud(); $("#hud-root").replaceChildren(node); S.hud = node; }
  function closeHud() { $("#hud-root").replaceChildren(); S.hud = null; }
  function hudShell(q, ...body) { return h("div", { class: "hud", role: "status" }, h("div", { class: "q" }, h("span", {}, q), h("button", { class: "icon-btn", style: "width:22px;height:22px", "aria-label": "Close", onclick: closeHud, html: ICON.close })), ...body); }
  function hudCost(a) {
    const L = D.live, scope = a.cost_scope ? a.cost_scope.value : "unstated";
    if (scope === "all_runs" || (a.period && a.period.value !== "unstated" && scope === "unstated")) {
      const period = a.period ? a.period.value : "unstated";
      const days = { today: 0, this_week: 7, this_month: 30, all_time: 9999, unstated: 9999 }[period];
      const rows = D.history.filter((x) => x.cost != null && x.age <= days);
      const total = rows.reduce((s, x) => s + x.cost, 0);
      return hudShell("spend · " + (period === "unstated" ? "all runs" : period.replace("_", " ")), h("div", { class: "big num" }, money(total), h("small", {}, rows.length + " runs")), h("div", { class: "line" }, rows.map((x) => x.id + " " + money(x.cost)).join(" · ")), h("div", { class: "foot" }, h("span", {}, "every run's reported cost, summed"), h("a", { onclick: () => { closeHud(); openSheet(sheetHistory()); } }, "history")));
    }
    if (scope === "named_run" && a.run && a.run.value !== "not named" && a.run.value !== "the live run" && a.run.value !== L.id) {
      const x = D.history.find((r) => r.id === a.run.value);
      if (x) return hudShell("spend · run " + x.id, h("div", { class: "big num" }, x.cost != null ? money(x.cost) : "·", x.cap ? h("small", {}, "of " + money(x.cap)) : h("small", {}, "no cap")), h("div", { class: "line" }, x.state + " · " + x.comps + (x.tokens ? " · " + x.tokens + " tokens" : "")));
    }
    return hudShell("spend · live run " + L.id, h("div", { class: "big num" }, money(L.spend.amount), h("small", {}, "of " + money(L.spend.cap) + " cap")), h("div", { class: "meter" }, h("i", { style: `width:${L.spend.pct}%` })), h("div", { class: "line" }, h("b", { class: "num" }, L.spend.pct + "%"), " · " + L.spend.tokens + " tokens · every call reported, so this is the whole amount"), h("div", { class: "foot" }, h("span", {}, "cap from kstrl.toml factory.max_cost_usd"), h("a", { onclick: () => { closeHud(); openSheet(sheetConfig()); } }, "config")));
  }
  function hudWhy(a) {
    const id = a.component ? a.component.value : "not named";
    const f = D.failedComponents.find((x) => x.id === id);
    if (f) return hudShell("why " + id + " failed", h("div", { class: "big" }, h("span", { class: "word failed" }, f.gate), h("small", {}, "attempt " + f.tries + " of " + f.tries)), h("div", { class: "line" }, h("b", {}, f.cause), " · run " + f.run + " · " + f.when), h("div", { class: "foot" }, h("span", {}, "evidence: attempt-" + f.tries + "/test_suite.log"), h("a", { onclick: () => { closeHud(); openItem("fail-client-commands"); } }, "retry scope")));
    const c = D.live.components.find((x) => x.id === id);
    if (c) return hudShell("why " + id + " failed", h("div", { class: "big" }, h("span", { class: "word " + c.state }, c.state)), h("div", { class: "line" }, id + " has not failed in " + D.live.id + (c.state === "running" ? " · engineer, iteration " + c.iter + " of " + D.live.now.iterations : c.state === "completed" ? " · completed in " + c.time : " · waiting on " + c.deps.join(", "))));
    return hudShell("what failed", h("div", { class: "big" }, h("span", { class: "word failed" }, D.failedComponents.length), h("small", {}, "failed components")), h("div", {}, ...D.failedComponents.map((x) => h("div", { class: "row" }, h("span", { class: "lamp failed" }), h("span", {}, h("b", {}, x.id), " · " + x.gate + " · " + x.when)))), h("div", { class: "foot" }, h("span", {}, "one has a retry available"), h("a", { onclick: () => { closeHud(); openItem("fail-client-commands"); } }, "review retry")));
  }
  function hudAlive(a) {
    const n = D.live.now;
    return hudShell("is the agent alive", h("div", { class: "big" }, h("span", { class: "lamp passed", style: "width:14px;height:14px" }), "alive"), h("div", { class: "row" }, h("span", { class: "lamp passed" }), h("span", {}, h("b", {}, n.component), " wrote output ", h("b", { class: "num" }, n.output + " ago"), " · stale after " + n.staleAfter)), h("div", { class: "row" }, h("span", { class: "lamp passed" }), h("span", {}, "worker ", h("b", { class: "num" }, n.pid), " alive · checked " + n.checked + " ago, every 5s")), h("div", { class: "row" }, h("span", { class: "lamp passed" }), h("span", {}, "ks serve daemon alive · checked " + D.serve.checked + " ago")), h("div", { class: "foot" }, h("span", {}, "both are process checks the server made, not claims")));
  }
  function hudMain() {
    const d = D.delivery;
    return hudShell("is main green", h("div", { class: "big" }, h("span", { class: "word unknown" }, "not known"), h("small", {}, "main at " + d.mainAt)), h("div", { class: "line" }, h("b", {}, "CI unknown"), " · " + d.mainReason + " · read " + d.read + " ago"), h("div", { class: "row", style: "margin-top:6px;gap:4px" }, ...d.merges.map((m) => h("span", { class: "sq " + m.state, title: "PR #" + m.pr + " " + m.component + " " + stateWord(m.state), style: "width:14px;height:14px" }))), h("div", { class: "line" }, "1 passed · 1 failed · 1 unknown · 1 not read yet, of the run's merges"), h("div", { class: "foot" }, h("span", {}, "unknown never reads as green"), h("a", { onclick: () => { closeHud(); openSheet(sheetDelivery()); } }, "delivery")));
  }
  function hudNeeds() {
    return hudShell("what needs me", h("div", { class: "big" }, D.needs.length, h("small", {}, "3 decisions · 1 retry")), h("div", {}, ...D.needs.map((n, i) => h("div", { class: "row" }, h("kbd", {}, i + 1), h("span", {}, h("b", {}, n.title), " · " + n.sub)))), h("div", { class: "foot" }, h("span", {}, "press the number to open one")));
  }

  // ---------------------------------------------------------- views
  function rows(source) {
    const fc = D.failedComponents.map((x) => ({ id: x.id, label: x.id, state: "failed", cost: x.cost, age: x.age, when: x.when, run: x.run, group: { state: "failed", component: x.id, run: x.run, day: x.when.split(" ")[0] }, note: x.gate + " · " + x.cause }));
    const lc = D.live.components.map((c) => ({ id: c.id, label: c.id, state: c.state, cost: c.cost, age: 0, when: "today", run: D.live.id, group: { state: c.state, component: c.id, run: D.live.id, day: "today" }, note: c.what }));
    switch (source) {
      case "components": return lc.concat(fc);
      case "runs": return D.history.map((x) => ({ id: x.id, label: x.id, state: x.state, cost: x.cost, age: x.age, when: x.when + " ago", run: x.id, group: { state: x.state, run: x.id, day: x.when }, note: x.kind + " · " + x.comps }));
      case "costs": return D.history.filter((x) => x.cost != null).map((x) => ({ id: x.id, label: x.id, state: x.state, cost: x.cost, age: x.age, when: x.when + " ago", run: x.id, group: { state: x.state, run: x.id, day: x.when }, note: x.tokens + " tokens" }));
      case "findings": return D.findings.map((f, i) => ({ id: f.kind + i, label: f.kind.replace(/_/g, " "), state: f.severity, cost: null, age: f.run === "live01" ? 0 : 1, when: f.run, run: f.run, group: { state: f.disposition, component: f.component, run: f.run, severity: f.severity, phase: f.phase }, note: f.where + " · " + f.disposition }));
      case "ci": return D.delivery.merges.map((m) => ({ id: "PR #" + m.pr, label: "PR #" + m.pr + " " + m.component, state: m.state, cost: null, age: m.run === "live01" ? 0 : 2, when: m.commit, run: m.run, group: { state: m.state, run: m.run, component: m.component }, note: m.reason }));
      case "queue": return [{ id: "q1", label: D.serve.running, state: "running", cost: null, age: 0, when: "now", run: "live01", group: { state: "running" }, note: "runs live01" }].concat(D.serve.queued.map((q, i) => ({ id: "q" + (i + 2), label: q, state: "queued", cost: null, age: 0, when: "queued", run: "", group: { state: "queued" }, note: "starts when live01 finishes" })));
      case "config": return D.config.map((c) => ({ id: c.key, label: c.label, state: "", cost: null, age: 0, when: c.source, run: "", group: { state: c.source }, note: c.value }));
      case "events": return Object.entries(D.live.spans).flatMap(([cid, sp]) => sp.map(([ph, a, b]) => ({ id: cid + ph, label: cid + " · " + ph, state: ph === "engineer" && cid === "http-app" ? "running" : "completed", cost: null, age: 0, when: "+" + a + "m", run: "live01", group: { component: cid, phase: ph, state: "completed" }, note: (b - a).toFixed(1) + " min", a, b })));
      default: return [];
    }
  }
  function applySpec(spec) {
    let r = rows(spec.source);
    const days = { today: 0, this_week: 7, this_month: 30, all_time: 9999, unstated: 9999 }[spec.period] ?? 9999;
    r = r.filter((x) => x.age <= days);
    if (spec.filter && spec.filter !== "all") r = r.filter((x) => (spec.filter === "waiting" ? ["parked", "waiting", "queued", "pending"].includes(x.state) : x.state === spec.filter || (spec.filter === "completed" && x.state === "passed")));
    if (spec.sort === "cost") r.sort((a, b) => (b.cost || 0) - (a.cost || 0));
    else if (spec.sort === "time") r.sort((a, b) => a.age - b.age);
    else if (spec.sort === "name") r.sort((a, b) => a.label.localeCompare(b.label));
    else if (spec.sort === "state") r.sort((a, b) => a.state.localeCompare(b.state));
    return r;
  }
  function specFromArgs(args, text) {
    const v = (k, d) => (args[k] && args[k].value !== "unstated" && args[k].value !== "none" && args[k].value !== "not named" ? args[k].value : d);
    let source = v("view_source", null), form = v("view_form", null);
    if (!source) source = /finding/i.test(text) ? "findings" : /cost|spend|token/i.test(text) ? "costs" : "components";
    if (!form) form = { costs: "sparkline", findings: "table", events: "timeline", ci: "list", diff: "diff" }[source] || "list";
    if (source === "diff") form = "diff";
    return { source, form, group: v("view_group", null), sort: v("view_sort", null), filter: v("view_filter_state", "all"), period: v("period", "unstated"), placement: v("view_placement", "float"), component: v("component", null), run: v("run", null), text };
  }
  function specChips(spec) {
    const chips = [["source", spec.source], ["form", spec.form], ["filter", spec.filter !== "all" ? spec.filter : null], ["period", spec.period !== "unstated" ? spec.period.replace("_", " ") : null], ["group", spec.group], ["sort", spec.sort], ["run", spec.run && spec.run !== "the live run" ? spec.run : null], ["component", spec.component]];
    return h("span", { class: "spec" }, ...chips.filter(([, v]) => v).map(([k, v]) => h("span", { class: "chip", title: k }, h("b", {}, v))));
  }
  function renderForm(spec, r) {
    const box = h("div", { class: "v-" + spec.form });
    if (spec.form === "list") {
      const groups = spec.group ? groupBy(r, spec.group) : { "": r };
      for (const [g, items] of Object.entries(groups)) { if (g) box.append(h("h4", { style: "margin:8px 0 2px;font-size:11.5px;color:var(--ink3)" }, g)); items.forEach((x) => box.append(h("div", { class: "r" }, h("span", { class: "lamp " + (x.state || "pending") }), h("span", {}, x.label, h("span", { class: "m" }, " · " + x.note)), h("span", { class: "m num" }, x.cost != null ? money(x.cost) : x.when)))); }
      if (!r.length) box.append(h("div", { class: "dim" }, "nothing matches"));
    } else if (spec.form === "board") {
      const groups = groupBy(r, spec.group || "state");
      box.append(...Object.entries(groups).map(([g, items]) => h("div", { class: "col" }, h("h4", {}, g, h("span", { class: "num" }, items.length)), ...items.map((x) => h("div", { class: "card" }, h("span", {}, x.label), h("span", { class: "m num" }, x.cost != null ? money(x.cost) : x.when))))));
    } else if (spec.form === "timeline") {
      const W = 430, Hh = 24 + Math.max(1, r.length) * 22 + 20, L0 = 110;
      const svg = h("svg:svg", { viewBox: `0 0 ${W} ${Hh}` });
      const isEvents = spec.source === "events";
      const span = isEvents ? 64 : Math.max(7, ...r.map((x) => x.age)) || 7;
      const maxCost = Math.max(1, ...r.map((x) => x.cost || 0));
      svg.append(h("svg:line", { class: "axis", x1: L0, x2: W - 8, y1: 12, y2: 12 }));
      const ticks = isEvents ? [[0, "16:08"], [32, "+32m"], [64, "now"]] : [[span, span + "d ago"], [Math.round(span / 2), Math.round(span / 2) + "d"], [0, "now"]];
      ticks.forEach(([d, lbl]) => { const x = isEvents ? L0 + (d / span) * (W - L0 - 8) : L0 + (1 - d / span) * (W - L0 - 8); svg.append(h("svg:line", { class: "tick", x1: x, x2: x, y1: 8, y2: Hh - 12 })); svg.append(h("svg:text", { x, y: 8, "text-anchor": d === 0 && !isEvents ? "end" : "middle" }, lbl)); });
      r.forEach((x, i) => {
        const y = 30 + i * 22;
        svg.append(h("svg:text", { class: "n", x: 0, y: y + 4 }, x.label.length > 16 ? x.label.slice(0, 15) + "…" : x.label));
        if (isEvents) { const x1 = L0 + (x.a / span) * (W - L0 - 8), x2 = L0 + (x.b / span) * (W - L0 - 8); svg.append(h("svg:rect", { x: x1, y: y - 5, width: Math.max(2, x2 - x1), height: 10, rx: 2, fill: x.state === "running" ? "var(--accent)" : "var(--line2)" })); }
        else { const cx = L0 + (1 - x.age / span) * (W - L0 - 8); const rad = 4 + ((x.cost || 0) / maxCost) * 7; svg.append(h("svg:circle", { class: x.state, cx, cy: y, r: rad })); svg.append(h("svg:text", { x: cx + rad + 5, y: y + 4 }, (x.cost != null ? money(x.cost) + " · " : "") + x.when)); }
      });
      box.append(svg);
      box.append(h("div", { class: "dim", style: "font-size:11px" }, isEvents ? "one bar per phase, left to right in time" : "one mark per item, placed by when it failed; size is cost"));
    } else if (spec.form === "graph") {
      box.append(h("div", { class: "schematic", style: "padding:4px" }, schematic()));
    } else if (spec.form === "meter") {
      const L = D.live.spend;
      box.append(h("div", { class: "big num" }, money(L.amount), h("small", {}, "of " + money(L.cap) + " cap · " + L.pct + "%")), h("div", { class: "meter" }, h("i", { style: `width:${L.pct}%` })), h("div", { class: "dim", style: "font-size:11.5px;margin-top:6px" }, "live run " + D.live.id + " · whole amount · updates with the event stream"));
    } else if (spec.form === "sparkline") {
      const items = r.slice().reverse(), W = 430, Hh = 96, bw = Math.max(8, (W - 8) / items.length - 6), max = Math.max(1, ...items.map((x) => x.cost || 0));
      const svg = h("svg:svg", { viewBox: `0 0 ${W} ${Hh}` });
      items.forEach((x, i) => { const hh = Math.max(3, ((x.cost || 0) / max) * 50); const xx = i * (bw + 6); svg.append(h("svg:rect", { x: xx, y: 70 - hh, width: bw, height: hh, rx: 2 })); svg.append(h("svg:text", { x: xx + bw / 2, y: 86, "text-anchor": "middle" }, x.label.slice(0, 6))); svg.append(h("svg:text", { x: xx + bw / 2, y: 65 - hh, "text-anchor": "middle" }, x.cost != null ? "$" + x.cost.toFixed(0) : "")); });
      box.append(svg);
    } else if (spec.form === "table") {
      const groups = spec.group ? groupBy(r, spec.group) : { "": r };
      const tb = h("tbody", {});
      for (const [g, items] of Object.entries(groups)) { if (g) tb.append(h("tr", {}, h("td", { colspan: 3, style: "color:var(--ink3);font-size:11.5px;font-weight:600;padding-top:8px" }, g + " · " + items.length))); items.forEach((x) => tb.append(h("tr", {}, h("td", {}, h("span", { class: "lamp " + (x.state || "pending"), style: "margin-right:6px;width:8px;height:8px" }), x.label), h("td", { class: "dim" }, x.note), h("td", { class: "num" }, x.cost != null ? money(x.cost) : x.when)))); }
      box.append(h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, spec.source === "findings" ? "finding" : "item"), h("th", {}, "detail"), h("th", { class: "num" }, spec.source === "findings" ? "run" : "cost"))), tb));
    } else if (spec.form === "number") {
      box.append(h("div", { class: "big num" }, r.length), h("div", { class: "l" }, (spec.filter !== "all" ? spec.filter + " " : "") + spec.source + (spec.period !== "unstated" ? " · " + spec.period.replace("_", " ") : "")));
    } else if (spec.form === "diff") {
      box.append(h("div", { class: "diff" }, ...D.checkpoint.diff.map(([k, t]) => h("div", { class: k }, t))));
    }
    return box;
  }
  function groupBy(r, key) { const g = {}; r.forEach((x) => { const k = x.group[key] ?? x[key] ?? "other"; (g[k] = g[k] || []).push(x); }); return g; }
  function makeView(spec, meta) {
    const r = applySpec(spec);
    const view = { id: "v" + Date.now(), spec, pinned: spec.placement === "dock" };
    const title = spec.text.charAt(0).toUpperCase() + spec.text.slice(1);
    const el = h("div", { class: "fview", "data-id": view.id },
      h("div", { class: "fview-head" }, h("span", { class: "title", title: title }, title),
        h("button", { class: "icon-btn" + (view.pinned ? " on" : ""), title: view.pinned ? "Unpin" : "Pin to the page", onclick: (e) => { view.pinned = !view.pinned; e.currentTarget.classList.toggle("on", view.pinned); place(el, view.pinned); toast(view.pinned ? "Pinned · saved to .kstrl/views/" + view.id + ".json" : "Unpinned · still saved", "passed"); }, html: ICON.pin }),
        h("button", { class: "icon-btn", title: "Remove", onclick: () => { el.remove(); S.views = S.views.filter((v) => v !== view); toast("View removed", "waiting"); }, html: ICON.close }),
        specChips(spec)),
      renderForm(spec, r),
      meta ? h("div", { class: "meta" }, h("span", {}, meta), h("span", {}, r.length + " items · saved as JSON, editable by command")) : null);
    S.views.push(view);
    place(el, view.pinned);
    return el;
  }
  function place(el, docked) { (docked ? $("#dock") : $("#fviews")).append(el); }

  // -------------------------------------------------- new component
  function componentRequest(text, cov, form) {
    const name = text.replace(/\bof\b.*$/i, "").trim();
    const el = h("div", { class: "fview" },
      h("div", { class: "fview-head" }, h("span", { class: "title" }, "The catalogue cannot draw this"), h("span", { class: "chip low" }, "outside " + cov.outside.toFixed(2)), h("button", { class: "icon-btn", title: "Dismiss", onclick: () => el.remove(), html: ICON.close })),
      h("div", { class: "request" },
        h("h4", {}, "Component request: " + name),
        h("dl", { class: "kv" }, h("dt", {}, "asked for"), h("dd", {}, "“" + text + "”"), h("dt", {}, "data"), h("dd", {}, form.source), h("dt", {}, "must show"), h("dd", {}, "the same rows the " + (cov.form !== "unstated" ? cov.form : "list") + " form shows, as a " + name), h("dt", {}, "gates"), h("dd", {}, "tests · typecheck · lint · review · security, the same as any component")),
        h("div", { class: "route" }, h("span", { class: "st now" }, "1 · request drafted"), h("span", { html: ICON.arrow }), h("span", { class: "st" }, "2 · kstrl factory run builds it"), h("span", { html: ICON.arrow }), h("span", { class: "st" }, "3 · gates pass"), h("span", { html: ICON.arrow }), h("span", { class: "st" }, "4 · joins the catalogue"))),
      h("div", { class: "btn-row" }, h("button", { class: "btn primary", onclick: () => confirmCardFree("Queue the component request", "Adds a spec for a " + name + " component to the ks serve queue.", "A factory run builds it under the usual gates and cost cap; the catalogue gains it only after the gates pass. Nothing changes on this page until then.", "same as: ks queue add .kstrl/requests/" + name.replace(/\s+/g, "-") + ".md", () => { el.remove(); toast("Queued · slice: " + name + " component", "running"); }) }, "Queue it", h("kbd", {}, "↵")), h("button", { class: "btn", onclick: () => { el.remove(); const spec = specFromArgs({}, text); spec.form = cov.form !== "unstated" ? cov.form : "table"; makeView(spec, "nearest catalogue form instead"); } }, "Use the nearest form: " + (cov.form !== "unstated" ? cov.form : "table"))),
      h("div", { class: "meta" }, h("span", {}, "Jev judged the form outside the catalogue at " + cov.outside.toFixed(2) + " (threshold 0.50, measured)"), h("span", {}, "Jev picks; it does not build")));
    $("#fviews").append(el);
  }
  function confirmCardFree(title, does, then, extra, onYes) {
    const wrap = h("div", { class: "confirm", style: "position:fixed;z-index:60" });
    const card = h("div", { class: "confirm-card" }, h("h2", {}, title), h("p", {}, h("b", {}, does), " ", then), extra ? h("div", { class: "cmdline" }, extra) : null, h("div", { class: "btn-row" }, h("button", { class: "btn primary", onclick: () => { wrap.remove(); onYes(); } }, "Confirm", h("kbd", {}, "↵")), h("button", { class: "btn", onclick: () => wrap.remove() }, "Cancel", h("kbd", {}, "esc"))));
    wrap.append(card); document.body.append(wrap); card.querySelector(".btn").focus();
    wrap.addEventListener("keydown", (e) => { if (e.key === "Escape") { wrap.remove(); e.stopPropagation(); } if (e.key === "Enter") { wrap.remove(); onYes(); e.stopPropagation(); } });
  }

  // ------------------------------------------------------- command bar
  const ROUTE_LABEL = {
    open_run: ["Open run", "go"], open_component: ["Open component", "go"], open_decisions: ["Open needs you", "go"], open_failures: ["Open failures", "go"], open_delivery: ["Open delivery", "go"], open_serve: ["Open ks serve", "go"], open_config: ["Open configuration", "go"], open_learning: ["Open learning", "go"], open_history: ["Open history", "go"],
    ask_cost: ["Answer: spend", "ask"], ask_why_failed: ["Answer: why it failed", "ask"], ask_alive: ["Answer: is the agent alive", "ask"], ask_main_green: ["Answer: is main green", "ask"], ask_needs_me: ["Answer: what needs me", "ask"],
    decide: ["Decide", "act"], retry: ["Retry", "act"], snooze: ["Snooze", "act"], start_factory: ["Start a factory run", "act"], stop_run: ["Stop the run", "act"], serve_control: ["ks serve", "act"], ci_poll: ["Read CI now", "act"], toggle_theme: ["Switch theme", "act"],
    make_view: ["Make a view", "view"], no_match: ["Nothing here does that", "none"],
  };
  const KIND_ICON = { go: "arrow", ask: "ask", act: "act", view: "view", none: "none" };
  const input = $("#cmd-input"), panel = $("#cmd-panel");
  let cmdItems = [];
  function argsSummary(rec) {
    const out = [];
    for (const [k, a] of Object.entries(rec.args || {})) { if (["unstated", "not named", "none", "toggle", "all"].includes(a.value)) continue; out.push(h("span", { class: "chip" + (a.conf < THRESHOLD ? " low" : "") }, k.replace(/^view_|^decide_|^cost_|^snooze_|^serve_/, "").replace("_", " ") + " ", h("b", {}, a.value.length > 28 ? a.value.slice(0, 26) + "…" : a.value))); }
    return out;
  }
  function confBar(c) { const n = Math.round(c * 5); return h("span", { class: "conf" + (c < THRESHOLD ? " low" : "") }, ...[1, 2, 3, 4, 5].map((i) => h("i", { class: i <= n ? "on" : "", style: `height:${3 + i * 1.6}px` }))); }
  function localMatches(q) {
    const t = q.toLowerCase(); if (!t) return [];
    const out = [];
    D.live.components.forEach((c) => { if (c.id.includes(t)) out.push({ label: c.id, kind: "component · " + c.state, run: () => openComponent(c.id) }); });
    D.history.forEach((x) => { if (x.id.includes(t)) out.push({ label: x.id, kind: x.kind + " run · " + x.state, run: () => openSheet(sheetRun(x)) }); });
    D.needs.forEach((n) => { if ((n.title + " " + n.sub).toLowerCase().includes(t)) out.push({ label: n.title, kind: n.kind, run: () => openItem(n.id) }); });
    return out.slice(0, 4);
  }
  function findReplay(q) { const t = q.trim().toLowerCase(); return REPLAY.find((r) => r.text.toLowerCase() === t) || null; }
  function nearestReplay(q) { const t = q.trim().toLowerCase(); const words = t.split(/\s+/).filter(Boolean); return REPLAY.map((r) => ({ r, s: words.filter((w) => r.text.toLowerCase().includes(w)).length + (r.text.toLowerCase().startsWith(t) ? 2 : 0) })).filter((x) => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 3).map((x) => x.r); }
  function renderPanel() {
    const q = input.value; cmdItems = [];
    panel.replaceChildren();
    if (!q.trim()) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    const rec = findReplay(q);
    const local = localMatches(q);
    if (rec) {
      const [label, kind] = ROUTE_LABEL[rec.route];
      const low = rec.conf < THRESHOLD;
      if (rec.route === "no_match") {
        panel.append(h("div", { class: "cmd-row" }, icon("none"), h("span", { class: "label" }, h("b", {}, "Nothing on this page does that"), h("span", { class: "kind" }, "no match · " + Math.round(rec.top[0][1] * 100) + "%")), h("span", { class: "right" }, confBar(rec.conf))));
        const alts = rec.top.slice(1).filter(([k, p]) => p >= 0.05);
        if (alts.length) { panel.append(h("div", { class: "cmd-sect" }, h("span", {}, "closest, if you meant one of these"))); alts.forEach(([k, p]) => cmdItems.push({ node: rowFor(k, p, rec), run: () => execute(k, rec) })); }
      } else if (!low) {
        cmdItems.push({ node: h("div", { class: "cmd-row" }, icon(KIND_ICON[kind]), h("span", { class: "label" }, h("b", {}, label), ...argsSummary(rec)), h("span", { class: "right" }, confBar(rec.conf), h("kbd", {}, "↵"))), run: () => execute(rec.route, rec) });
        const alts = rec.top.slice(1).filter(([k, p]) => p >= 0.05 && k !== "no_match");
        if (alts.length) { panel.append(h("div", { class: "cmd-sect" }, h("span", {}, "or"))); }
        alts.forEach(([k, p]) => cmdItems.push({ node: rowFor(k, p, rec), run: () => execute(k, rec) }));
      } else {
        panel.append(h("div", { class: "cmd-sect" }, h("span", {}, "Not sure what you mean · pick one"), h("span", {}, "confidence " + rec.conf.toFixed(2) + " under 0.80")));
        rec.top.filter(([, p]) => p >= 0.03).forEach(([k, p]) => cmdItems.push({ node: rowFor(k, p, rec), run: () => execute(k, rec) }));
      }
    }
    // the ordered list of items: routed rows first, then local matches
    const routed = cmdItems.slice();
    panel.replaceChildren(...[...panel.children].filter((c) => c.classList.contains("cmd-sect") && !routed.length), ...routed.map((x) => x.node));
    if (rec && rec.route !== "no_match" && routed.length) {
      // keep the "or" divider between the first row and the alternatives
      const first = routed[0].node; panel.replaceChildren(); panel.append(rec.conf < THRESHOLD ? h("div", { class: "cmd-sect" }, h("span", {}, "Not sure what you mean · pick one"), h("span", {}, "confidence " + rec.conf.toFixed(2) + " under 0.80")) : first);
      const rest = rec.conf < THRESHOLD ? routed : routed.slice(1);
      if (rest.length) { if (rec.conf >= THRESHOLD) panel.append(h("div", { class: "cmd-sect" }, h("span", {}, "or"))); rest.forEach((x) => panel.append(x.node)); }
    } else if (rec && rec.route === "no_match") {
      panel.replaceChildren(h("div", { class: "cmd-row" }, icon("none"), h("span", { class: "label" }, h("b", {}, "Nothing on this page does that"), h("span", { class: "kind" }, "no match · " + Math.round(rec.top[0][1] * 100) + "%")), h("span", { class: "right" }, confBar(rec.conf))));
      if (routed.length) { panel.append(h("div", { class: "cmd-sect" }, h("span", {}, "closest, if you meant one of these"))); routed.forEach((x) => panel.append(x.node)); }
    }
    if (local.length) { panel.append(h("div", { class: "cmd-sect" }, h("span", {}, "On this page"), h("span", {}, "matched locally, no model"))); local.forEach((l) => { const node = h("div", { class: "cmd-row" }, icon("arrow"), h("span", { class: "label" }, h("b", {}, l.label), h("span", { class: "kind" }, l.kind))); cmdItems.push({ node, run: l.run }); panel.append(node); }); }
    if (!rec) {
      const near = nearestReplay(q);
      panel.append(h("div", { class: "cmd-sect" }, h("span", {}, "Not recorded"), h("span", {}, "this prototype replays " + REPLAY.length + " real Jev answers")));
      near.forEach((r) => { const node = h("div", { class: "cmd-row" }, icon("ask"), h("span", { class: "label" }, h("b", {}, r.text), h("span", { class: "kind" }, ROUTE_LABEL[r.route][0].toLowerCase()))); cmdItems.push({ node, run: () => { input.value = r.text; renderPanel(); } }); panel.append(node); });
    }
    panel.append(h("div", { class: "cmd-foot" }, h("span", { class: "keys" }, h("span", {}, h("kbd", {}, "↑↓"), "move"), h("span", {}, h("kbd", {}, "↵"), "run"), h("span", {}, h("kbd", {}, "esc"), "close")), h("span", {}, rec ? `Jev ${SUMMARY.model || "jev-1.13.0"} · ${rec.latency_ms} ms · ${rec.tokens.toLocaleString()} tokens · recorded` : "type a recorded phrasing")));
    S.cmdSel = 0; markSel();
  }
  function rowFor(k, p, rec) { const [label, kind] = ROUTE_LABEL[k]; return h("div", { class: "cmd-row" }, icon(KIND_ICON[kind]), h("span", { class: "label" }, h("b", {}, label), h("span", { class: "kind" }, kind)), h("span", { class: "right" }, h("span", { class: "pbar" }, h("i", { style: `width:${p * 100}%` })), h("span", { class: "num" }, Math.round(p * 100) + "%"))); }
  function markSel() { cmdItems.forEach((it, i) => { it.node.classList.toggle("sel", i === S.cmdSel); it.node.onclick = () => { S.cmdSel = i; runSel(); }; }); }
  function runSel() { const it = cmdItems[S.cmdSel]; if (!it) return; it.run(); if (!panel.classList.contains("hidden") && findReplay(input.value)) { input.value = ""; renderPanel(); input.blur(); } }
  function execute(route, rec) {
    const a = rec.args || {};
    const v = (k) => (a[k] ? a[k].value : null);
    switch (route) {
      case "open_run": { const id = v("run"); const x = D.history.find((r) => r.id === id) || D.history[0]; if (!id || id === "the live run" || id === "live01") { setFocus("run"); S.sel = null; closeSheet(); } else openSheet(sheetRun(x)); break; }
      case "open_component": { const id = v("component"); if (id && id !== "not named") openComponent(id); else openSheet(sheetDecisions()); break; }
      case "open_decisions": openSheet(sheetDecisions()); break;
      case "open_failures": openItem("fail-client-commands"); break;
      case "open_delivery": openSheet(sheetDelivery()); break;
      case "open_serve": openSheet(sheetServe()); break;
      case "open_config": openSheet(sheetConfig()); break;
      case "open_learning": openSheet(sheetLearning()); break;
      case "open_history": openSheet(sheetHistory()); break;
      case "ask_cost": openHud(hudCost(a)); break;
      case "ask_why_failed": openHud(hudWhy(a)); break;
      case "ask_alive": openHud(hudAlive(a)); break;
      case "ask_main_green": openHud(hudMain()); break;
      case "ask_needs_me": openHud(hudNeeds()); break;
      case "decide": {
        const t = v("target_item") || "not named", ch = v("decide_choice") || "unspecified";
        const named = a.target_item && a.target_item.conf >= THRESHOLD && /\b(comp-c|client-commands|fda682|halted|integration)\b/i.test(rec.text);
        // a target Jev filled with high confidence but that was never typed is not trusted: open the list instead
        if (!named) { openSheet(sheetDecisions()); toast("Which item? None was named", "waiting"); break; }
        if (t.startsWith("checkpoint")) openSheet(sheetCheckpoint(ch !== "unspecified" ? ch : null));
        else if (t.startsWith("merge gate")) openSheet(sheetGate(ch !== "unspecified" ? ch : null));
        else openSheet(sheetHalt());
        break;
      }
      case "retry": { const id = v("component"); if (id === "client-commands") openSheet(sheetFailure(true)); else if (id && id !== "not named") toast(id + " is not failed; nothing to retry", "waiting"); else openItem("fail-client-commands"); break; }
      case "snooze": { const t = v("target_item") || ""; if (t.startsWith("merge gate")) openSheet(sheetGate("later")); else { openSheet(sheetHalt()); } break; }
      case "start_factory": openSheet(sheetShell("Start a factory run", ["spec.md · project snippetvault"], [h("h3", {}, "Preflight"), gates([{ name: "kstrl.toml", state: "passed", note: "valid" }, { name: "factory lock", state: "failed", note: "held by live01 · refuses until it finishes" }, { name: "base branch", state: "passed", note: "main at cea97b4" }]), h("div", { class: "muted", style: "margin-top:12px;font-size:12.5px" }, "Preflight refuses before anything is spent. Queue it instead and ks serve starts it when live01 finishes."), h("div", { class: "btn-row" }, h("button", { class: "btn primary", onclick: () => confirmCard("Queue spec.md", "Adds spec.md to the ks serve queue.", "Starts when live01 finishes and every admission check passes; spends against a $78.00 cap.", "same as: ks queue add spec.md", () => { closeSheet(); toast("Queued · slice 5", "running"); }) }, "Queue it"))])); break;
      case "stop_run": openSheet(sheetShell("Stop live01?", [h("span", { class: "lamp running" }), "running · http-app · iteration 3 of 10"], [h("div", { class: "muted", style: "margin-top:8px" }, "Only a run this page's server started can be stopped here. live01 was started by ks serve."), h("div", { class: "btn-row" }, h("button", { class: "btn danger", onclick: () => confirmCard("Stop live01", "Ends the process group of the run.", "http-app's attempt is lost; completed components keep their merges. The run is recorded as stopped.", null, () => { closeSheet(); toast("live01 stopped", "failed"); }) }, "Stop"))])); break;
      case "serve_control": { const act = v("serve_action"); openSheet(sheetServe()); setTimeout(() => confirmCard((act === "pause" ? "Pause" : "Resume") + " ks serve", act === "pause" ? "Stops admitting queued work." : "Starts admitting queued work again.", act === "pause" ? "The running item finishes; nothing new starts until resumed." : "The next queued item starts when its admission checks pass.", "same as: ks queue " + act, () => { closeSheet(); toast("ks serve " + act + "d", "waiting"); }), 0); break; }
      case "ci_poll": openSheet(sheetDelivery()); setTimeout(() => confirmCard("Read CI now", "Asks GitHub for the checks on every recorded merge commit and records what it read.", "Changes nothing else. A state it cannot read is recorded as unknown.", "same as: ks ci poll", () => { closeSheet(); toast("ks ci poll ran · 4 read, 1 unknown", "passed"); }), 0); break;
      case "toggle_theme": { const t = v("theme"); setTheme(t === "dark" || t === "light" ? t : S.theme === "dark" ? "light" : "dark"); break; }
      case "make_view": {
        const cov = COVERAGE.find((c) => c.text.toLowerCase() === rec.text.toLowerCase());
        const spec = specFromArgs(a, rec.text);
        if (cov && cov.outside >= COVER_HIGH) { componentRequest(rec.text, cov, spec); break; }
        if (cov && cov.outside >= COVER_LOW) { spec.form = cov.form !== "unstated" ? cov.form : spec.form; const el = makeView(spec, "Jev: " + rec.conf.toFixed(2) + " route · form outside the catalogue " + cov.outside.toFixed(2)); el.insertBefore(h("div", { class: "nearest" }, h("span", {}, "Not quite what was asked. Nearest form: ", h("b", {}, spec.form)), h("span", { class: "spacer" }), h("button", { class: "btn", style: "height:26px;font-size:12px;padding:0 10px", onclick: () => componentRequest(rec.text, cov, spec) }, "Request a " + rec.text.split(" ")[0] + " component")), el.children[1]); break; }
        makeView(spec, "Jev filled the spec at " + rec.conf.toFixed(2) + " · " + rec.latency_ms + " ms");
        break;
      }
      case "no_match": toast("Nothing on this page does that", "waiting"); break;
    }
  }
  input.addEventListener("input", renderPanel);
  input.addEventListener("focus", renderPanel);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); S.cmdSel = Math.min(cmdItems.length - 1, S.cmdSel + 1); markSel(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); S.cmdSel = Math.max(0, S.cmdSel - 1); markSel(); }
    else if (e.key === "Enter") { e.preventDefault(); runSel(); }
    else if (e.key === "Escape") { if (input.value) { input.value = ""; renderPanel(); } else input.blur(); }
  });
  document.addEventListener("click", (e) => { if (!$("#cmd").contains(e.target)) panel.classList.add("hidden"); });

  // ---------------------------------------------------------- keyboard
  document.addEventListener("keydown", (e) => {
    const inField = e.target === input;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); input.focus(); input.select(); return; }
    if (inField) return;
    if (e.key === "/" ) { e.preventDefault(); input.focus(); return; }
    if (e.key === "Escape") { if (S.confirm) cancelConfirm(); else if (S.sheet) closeSheet(); else if (S.hud) closeHud(); return; }
    if (S.confirm && e.key === "Enter") { e.preventDefault(); S.confirm.querySelector(".btn.primary").click(); return; }
    if (S.sheet && !S.confirm) {
      const choices = [...S.sheet.querySelectorAll(".choice")];
      if (choices.length) {
        const byKey = choices.find((c) => c.querySelector("kbd") && c.querySelector("kbd").textContent === e.key);
        if (byKey) { byKey.click(); return; }
        if (e.key === "ArrowDown" || e.key === "j") { S.choiceSel = Math.min(choices.length - 1, S.choiceSel + 1); choices[S.choiceSel].focus(); return; }
        if (e.key === "ArrowUp" || e.key === "k") { S.choiceSel = Math.max(0, S.choiceSel - 1); choices[S.choiceSel].focus(); return; }
      }
      if (e.key.toLowerCase() === "r" && S.sheet.querySelector(".btn.primary")) { S.sheet.querySelector(".btn.primary").click(); return; }
      return;
    }
    if (/^[1-4]$/.test(e.key)) { const n = D.needs[Number(e.key) - 1]; if (n) openItem(n.id); }
    if (e.key === "t") setTheme(S.theme === "dark" ? "light" : "dark");
  });

  // ------------------------------------------------------------- toast
  let toastTimer = null;
  function toast(msg, state) { const root = $("#toast-root"); root.replaceChildren(h("div", { class: "toast" }, h("span", { class: "lamp " + (state || "passed") }), msg)); clearTimeout(toastTimer); toastTimer = setTimeout(() => root.replaceChildren(), 3200); }

  // ------------------------------------------------------------- boot
  function renderAll() { renderTelemetry(); renderNeeds(); renderRun(); renderMain(); renderHist(); }
  renderAll();
  // Screenshot states: #s=<state>&q=<command>&theme=<dark|light>
  const st = params.get("s"), q = params.get("q");
  const type = (text, run) => { input.value = text; input.focus(); renderPanel(); if (run) runSel(); };
  if (st === "command" && q) type(q, false);
  else if (st === "run" || (st === "exec" && q)) { if (st === "run") setFocus("run"); if (q) { type(q, true); } }
  else if (st === "checkpoint") openItem("checkpoint-comp-c");
  else if (st === "confirm") openSheet(sheetCheckpoint("approve_run"));
  else if (st === "failure") openItem("fail-client-commands");
  else if (st === "delivery") openSheet(sheetDelivery());
  else if (st === "gate") openItem("gate-client-commands");
  else if (st === "views") { type("show me failed components this week by cost as a timeline", true); const r2 = findReplay("cost per run as a sparkline"); if (r2) { const sp = specFromArgs(r2.args, r2.text); sp.placement = "dock"; makeView(sp, "Jev filled the spec at " + r2.conf.toFixed(2)); } }
  else if (st === "newcomp") { type("word cloud of finding kinds", true); }
  else if (st === "nearest") { type("heatmap of cost by hour of day", true); }
  else if (st === "ask" && q) type(q, true);
  if (st && st !== "command") { input.blur(); panel.classList.add("hidden"); }
})();
