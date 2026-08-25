// Run the lesson's inline widget script under node, with no DOM, and compare
// every rule against the Python sweep that verified it.
//
// The page's script defines its rules on window.LESSON_RULES before it touches
// the document and returns early when there is no document, so the same code
// the browser runs is what is checked here. Each Python script prints its
// sweep as JSON with --json; this file re-runs the same grid through the JS
// rules and reports the first mismatch, or none.
//
//   node docs/lessons/verify/pr-221/check_widgets.mjs [docs/lessons/pr-221.html]

import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const lesson = process.argv[2] || join(here, "..", "..", "pr-221.html");
const html = readFileSync(lesson, "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const widget = scripts.find((s) => s.includes("LESSON_RULES"));
if (!widget) { console.error("no widget script found"); process.exit(1); }

const ctx = { console };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(widget, ctx);
const R = ctx.LESSON_RULES;
if (!R) { console.error("LESSON_RULES not defined"); process.exit(1); }

function py(name) {
  const out = execFileSync("python3", [join(here, name), "--json"], { maxBuffer: 1 << 28 });
  return JSON.parse(out.toString("utf8"));
}
function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
let failures = 0;
function report(name, rows, mismatch) {
  if (mismatch) { failures += 1; console.log(`FAIL ${name}: ${rows} rows; first mismatch ${JSON.stringify(mismatch).slice(0, 300)}`); }
  else { console.log(`ok   ${name}: ${rows} rows agree with the Python sweep`); }
}

// 5.1
{
  const rows = py("setpoint_agreement.py");
  let bad = null;
  for (const r of rows) {
    const j = R.setpoint(r.engineer_passes, r.review, r.c1, r.c2, r.mode, r.level);
    const o = R.oldRule(r.engineer_passes);
    if (!same([j.key, j.finding, j.severity, j.verdict, o], [r.key, r.finding, r.severity, r.verdict, r.old])) { bad = { r, j }; break; }
  }
  report("setpoint_agreement", rows.length, bad);
}
// 5.2
{
  const rows = py("retry_context_rank.py");
  let bad = null;
  for (const r of rows) {
    const j = R.retryEvaluate(r.sequence, r.legacy);
    if (!same(j, r)) { bad = { r, j }; break; }
  }
  report("retry_context_rank", rows.length, bad);
}
// 5.3
{
  const rows = py("budget_halt.py");
  let bad = null;
  for (const r of rows) {
    const j = R.budget(r.cap, r.n, r.mode, r.new_rule);
    if (!same(j, r.outcome)) { bad = { r, j }; break; }
  }
  report("budget_halt", rows.length, bad);
}
// 5.4
{
  const rows = py("admission_gates.py");
  let bad = null;
  for (const r of rows) {
    const j = R.admit(r);
    if (!same([j.gate, j.kind, j.reason], [r.gate, r.kind, r.reason])) { bad = { r, j }; break; }
  }
  report("admission_gates", rows.length, bad);
}
// 5.5
{
  const data = py("autonomy_ladder.py");
  let bad = null;
  for (const t of data.traces) {
    const s = R.newLadder();
    const lines = t.actions.map((a) => R.ladderAct(s, a, t.demote_on_calibration));
    const got = { lines, level: s.level, cooldown: s.cooldown,
      counters: [s.decisive_runs, s.components_merged, s.clean_merges, s.policy_violations],
      bundle: R.flagBundle(s.level), blockers: R.ladderBlockers(s) };
    const want = { lines: t.lines, level: t.level, cooldown: t.cooldown, counters: t.counters, bundle: t.bundle, blockers: t.blockers };
    if (!same(got, want)) { bad = { name: t.name, got, want }; break; }
  }
  let n = data.traces.length;
  if (!bad) {
    for (const c of data.clamps) {
      const j = R.runtimeLevel(c.level, c.max_level, c.policy, c.external);
      n += 1;
      if (!same([j.runtime, j.notes], [c.runtime, c.notes])) { bad = { c, j }; break; }
    }
  }
  report("autonomy_ladder", n, bad);
}
// 5.6
{
  const rows = py("prefix_stack.py");
  let bad = null;
  for (const r of rows) {
    const seq = R.prefixOrder(r.state, r);
    const got = [seq, R.afterRetry(seq), seq.length > 1 ? seq[seq.length - 2] : ""];
    if (!same(got, [r.order, r.after_retry, r.last_before_template])) { bad = { r, got }; break; }
  }
  report("prefix_stack", rows.length, bad);
}
// 5.7
{
  const rows = py("sense_commands.py");
  let bad = null;
  for (const r of rows) {
    const table = r.state === "main" ? R.COMMANDS : R.AFTER_237;
    const row = table.find((c) => c[0] === r.command);
    if (!row || row[1] !== r.reaches || row[2] !== r.standalone || R.classify(row[1], row[2]) !== r.verdict) { bad = { r, row }; break; }
  }
  const counts = [R.COMMANDS.length, R.AFTER_237.length, R.COMMANDS.filter((c) => c[2]).length, R.AFTER_237.filter((c) => c[2]).length];
  if (!bad && !same(counts, [15, 16, 0, 1])) { bad = { counts }; }
  report("sense_commands", rows.length, bad);
}
// the map tables
{
  const data = py("map_paths.py");
  const walk = R.WALK.map(({ title, nodes, measures, edges }) => ({ title, nodes, measures, edges }));
  const loops = R.LOOPS;
  let bad = null;
  if (!same(walk, data.walk)) { bad = { what: "walk" }; }
  else if (!same(loops, data.loops)) { bad = { what: "loops" }; }
  else if (!same(R.CHANGES, data.changes)) { bad = { what: "changes" }; }
  report("map_paths", data.walk.length + data.loops.length + data.changes.length, bad);
}

// The "try this" moves, as the reader would make them.
{
  const moves = [];
  const s1 = R.setpoint(true, "ran", "pass", "uncovered", "advisory", 0);
  moves.push(["5.1 one uncovered beside a pass agrees", s1.key === "agree"]);
  moves.push(["5.1 both uncovered files a finding, not covered", same([R.setpoint(true, "ran", "uncovered", "uncovered", "advisory", 0).key, R.setpoint(true, "ran", "uncovered", "uncovered", "advisory", 0).verdict], ["advisory", "not covered"])]);
  moves.push(["5.1 level 1 turns a fail finding blocking", R.setpoint(true, "ran", "fail", "pass", "advisory", 0).key === "advisory" && R.setpoint(true, "ran", "fail", "pass", "advisory", 1).key === "block"]);
  const v4 = R.retryEvaluate(["verification", "verification", "verification", "verification"], false);
  moves.push(["5.2 four verification attempts: current 1, old 4, new shorter", v4.current.length === 1 && v4.old_entries === 4 && v4.new_chars < v4.old_chars]);
  const rv = R.retryEvaluate(["review", "verification"], false);
  moves.push(["5.2 review then verification: review entry not re-measured", rv.not_remeasured.length === 1 && rv.resolved === 0]);
  moves.push(["5.3 cap 1, three components, hard, new: two halts", same(R.budget(1, 3, "hard", true).map((r) => r.split(":")[0].split(",")[0]), ["reviewed", "HALTED", "HALTED"])]);
  moves.push(["5.3 same, old rule: two merged unreviewed", R.budget(1, 3, "hard", false).filter((r) => r.startsWith("skipped")).length === 2]);
  moves.push(["5.3 cap 0 identical", same(R.budget(0, 3, "hard", true), R.budget(0, 3, "hard", false))]);
  const base = { after_r10_7: true, ledger_readable: true, paused: false, poison_streak: 0, budget_on: false, budget_reached: false, coverage_seen: true, allow_uncovered: false, max_open_prs: 1, open_prs: 0, create_prs: true, gh_ok: true, inbox_at_cap: false, lock_held: false, ready_item: true };
  moves.push(["5.4 one open PR at bound 1 waits", same([R.admit({ ...base, open_prs: 1 }).gate, R.admit({ ...base, open_prs: 1 }).kind], ["open-PR bound", "wait"])]);
  moves.push(["5.4 gh failed waits", R.admit({ ...base, gh_ok: false }).kind === "wait"]);
  moves.push(["5.4 poison streak 3 wins over the bound", R.admit({ ...base, open_prs: 1, poison_streak: 3 }).gate === "poison breaker"]);
  const st = R.newLadder();
  for (let i = 0; i < 8; i++) R.ladderAct(st, "run", false);
  for (let i = 0; i < 5; i++) R.ladderAct(st, "merge", false);
  R.ladderAct(st, "promote", false);
  const l2 = st.level;
  R.ladderAct(st, "violation", false);
  moves.push(["5.5 eight runs, five merges, promote: L2; violation: L1 with cool-down 10", l2 === 2 && st.level === 1 && st.cooldown === 10]);
  moves.push(["5.5 policy off clamps L3 to L2", R.runtimeLevel(3, 4, false, true).runtime === 2]);
  const all = { knowledge: true, golden: true, feedforward: true, retry: true, memory: true, claude_md: true };
  moves.push(["5.6 without memory, CLAUDE.md follows the retry context", R.afterRetry(R.prefixOrder("end", { ...all, memory: false })) === "claude_md"]);
  moves.push(["5.6 without memory or CLAUDE.md, the instructions follow", R.afterRetry(R.prefixOrder("end", { ...all, memory: false, claude_md: false })) === "template"]);
  moves.push(["5.6 today never lists golden or memory", !R.prefixOrder("today", all).includes("golden") && !R.prefixOrder("today", all).includes("memory")]);
  moves.push(["5.7 factory reaches through a run; status never; sense direct after #237", R.classify(true, false).startsWith("through") && R.classify(false, false).startsWith("does not") && R.AFTER_237.find((c) => c[0] === "sense")[2] === true]);
  moves.push(["5.8 inner-loop sensor is the implement loop; masthead chip is observe", R.CHANGES.find((c) => c.change.startsWith("a sensor between")).loop === "implement" && R.CHANGES.find((c) => c.change.startsWith("a safe-mode chip")).loop === "observe"]);
  moves.push(["walk step 5 measures only breaker and path guard", same(R.WALK[4].measures, ["Breaker", "PathGuard"])]);
  moves.push(["walk steps 10 and 11 light one ghost each, no edges", R.WALK[9].nodes.length === 1 && R.WALK[9].edges.length === 0 && R.WALK[10].nodes.length === 1]);
  for (const [name, ok] of moves) { if (!ok) failures += 1; console.log(`${ok ? "ok  " : "FAIL"} try-this: ${name}`); }
}

console.log(failures ? `RESULT: FAIL (${failures})` : "RESULT: PASS");
process.exit(failures ? 1 : 0);
