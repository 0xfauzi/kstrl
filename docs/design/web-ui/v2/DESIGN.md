# The web UI, round 2: a command-first page whose details float

This document is for the owner, who decides whether this is the design, and for the engineers who build it. It replaces PLAN.md's section 11 (the visual system) and its screen layouts. PLAN.md's section 1 (the eight operator questions), section 6 (the architecture: a server inside kstrl, the readers reused, decisions through the CLI code paths) and the parity table still stand.

Round 1 was rejected because it was the terminal UI re-laid in a browser: a sidebar of views, tables of rows, paragraphs where a shape would do. Round 2 starts from the operator's jobs and asks, for each, what the least text is that answers it. The result is one page with a command field at the top and nothing fixed below it but the answers to the eight questions, drawn as shapes. Everything else floats in when asked for and leaves when dismissed.

The prototype is `prototype/index.html`; the renders are in `shots/`; the Jev measurements are in `jev-bench/README.md`; the framework comparison is `FRAMEWORK.md`.

## 1. The page at rest answers the eight questions without prose

At rest the page holds four regions and the command field. Each region is one of the operator's jobs and each is drawn, not written:

| Region | Questions | What is drawn |
|---|---|---|
| Needs you | Q4, Q5 | one row per item: a glyph for the kind (a person for a decision, a clock for a halt, a cross for a failure), the item's name, one line of context, its age, and the number key that opens it |
| The live run | Q1, Q2, Q6 | a progress ring (components done of total), the component and phase now running, two liveness lamps (last output age against the stale threshold; worker process alive with the check's age), the spend meter (amount, cap, percentage, whole or lower bound), the elapsed clock; below them the schematic: components as nodes in dependency tiers, each with a seven-segment ring for the seven phases, the running one turning; below that the phase timeline: one track per component, phases as coloured spans on the run's clock |
| Main | Q8 | the commit main is at with its CI state and reason, then a ladder of merges, one square per merge commit coloured by CI state (green passed, red failed, amber unknown, hollow not read), with the component and commit beside it; under it ks serve: the daemon lamp and the queue as two pills |
| History | Q1 over time, Q2, Q3 | cost bars for the last nine runs, coloured by state, dashed when the run refused before spending; selecting a bar shows that run's state and note in one line; beside the bars the live event feed, six lines |

Nothing on this page is a table of prose, and no region has a boxed header: a region is a small title, a count and a one-line hint at the right, then its shape. There is no sidebar because there are no views to list: what the operator would go to, they ask for.

The eight questions are answered at a glance: what is running (the ring, the node that turns), what it cost (the meter), why something failed (the red row under Needs you, opened with one key), what needs me (the region), how do I act (the number keys and the command field), is the agent alive (two green lamps with their ages), what the reviewers found (inside the decision sheet, beside the choices), is main green (the ladder).

## 2. Command first: one field, four kinds of request

The command field is the primary surface. It is always visible at the top, `⌘K` or `/` focuses it, and everything the page can do is reachable from it. Four kinds of request go through it:

- Go: open a run, a component, the decisions, the failures, delivery, the serve queue, configuration, learning, history. The target opens as a sheet (a floating panel from the right) or, for the live run, expands the run region.
- Act: decide a waiting item, retry, snooze, start a factory run, stop a run, pause or resume ks serve, read CI now, switch theme. Every act passes through a confirmation that names its consequence (section 5).
- Ask: how much was spent, why a component failed, is the agent alive, is main green, what needs me. The answer is a HUD (a small floating panel at the bottom right): one number or one word, one line of context, one link to the place that holds more.
- Make a view: a list, board, timeline, graph, meter, sparkline, table, number or diff over runs, components, events, costs, findings, CI, the queue or config, filtered, grouped, sorted, floating or docked (section 6).

The routing is a Jev Choice over 24 routes, one of them "nothing here does that", with 15 argument Choices in the same request (the function-calling cookbook's dispatcher shape, fanned out). Free values are never generated: run ids and component names are the ones the page knows, put into the criteria as candidates, and Jev selects among them; durations and periods are closed sets; every set has a "not named" or "unstated" option. The request is one round trip. Measured on 86 labelled phrasings: 95.3% top-1 route accuracy, 96.6% argument accuracy, 0.24 s median latency, 3,065 input tokens, $0.00013 per command (`jev-bench/README.md`).

The command panel shows what Jev picked as one row: the route as a short label, the arguments it read as chips, a five-bar confidence mark, and Enter. Beside it, matches found locally by substring (components, runs, waiting items) are listed under "On this page", with no model call. The panel's footer names the model version, the latency and the token count of the recorded answer, so the operator can see what the routing cost.

Confidence is not permission. The measured threshold at which no wrong routing survived on the labelled set is 0.80. At or above it the panel shows the routed action first, ready on Enter. Below it the panel says "Not sure what you mean" and lists the top options from the probability distribution with their percentages; the operator picks one; nothing runs on Enter alone. A route of "nothing here does that" shows as such, with the closest alternatives dimmed under it, so a request the page cannot serve is never quietly served as something else.

One more rule came out of the measurement: an argument Jev fills with high confidence but that was never typed is not trusted. "aprove the merge" (the typo is the phrasing as typed) filled the target with the comp-c checkpoint at 0.94 confidence, and there were three open items. <!-- codespell:ignore aprove --> So a decision whose target does not appear in the typed text opens the decision list, not an item.

The prototype replays the 86 recorded answers. A typed text that matches a recorded phrasing shows the real route, confidence, alternatives, arguments, latency and token count; any other text shows "not recorded" and the nearest recorded phrasings, so the owner never sees an invented routing.

## 3. Details float

Nothing below the command field is a fixed column of text. Three floating surfaces carry the detail:

- The sheet: a 480px panel that slides in from the right over the page, for anything that needs evidence and a decision, or more than a line. A decision sheet is evidence above (gates as lamps with a word, changed files as a minimap of added and removed lines, findings as rows with a severity glyph, the spend so far) and the choices pinned at the sheet's foot so all of them are in view with their consequences while the evidence scrolls. On a phone the sheet is the whole screen.
- The HUD: a 340px panel at the bottom right for an answer. It holds one big number or word, a meter where the answer is an amount, one line of context, and a link to the place with more. It closes with Esc or when a sheet opens.
- Made views: floating panels at the bottom left, each with its spec as chips, a pin control and a close control. Pinning moves the view into the dock at the top of the page, where it stays across visits.

Elevation is declared once per surface: floating surfaces carry a soft offset shadow and no border; docked regions carry a hairline and no shadow. Motion is one authored moment per surface: the sheet slides 16px in over 180ms with an exponential ease-out, the command panel drops 4px over 140ms, the HUD and made views rise 8px over 160ms, the running node's ring turns once every 2.4s, and the running lamp pulses. All of it stops under a reduced-motion preference.

## 4. The visual world

The world is a control room's mimic diagram fused with the precision of a good product interface: a calm surface where the running system is drawn as a schematic, state is a shape and a colour, and colour is spent on nothing else. The direction was dealt by the design skill's concept roll from a grounded list of seven; the process-control faceplate was the sixth candidate and the assigned one, and it carried the brief well: faceplates are floating panels, alarm summaries are Needs you, trend pens are sparklines, the schematic is the run, and an interlock that states what it will do before an output changes is the confirmation step.

Colour strategy: restrained. Two composed themes, not one inverted.

| Role | Light | Dark | Use |
|---|---|---|---|
| canvas | `#EEEDE8` | `#121417` | the page |
| surface | `#F6F5F1` | `#181B1F` | docked regions' shapes (schematic, timeline, bars) |
| float | `#FFFFFF` | `#1F2329` | the command field and panel, the sheet, the HUD, made views |
| inset | `#E6E4DD` | `#0E1013` | logs, diffs, meters' tracks |
| ink, ink 2, ink 3 | `#1B1A17`, `#5A5852`, `#6B6962` | `#E9E7E1`, `#A8A69E`, `#8C8A80` | text in three weights of importance |
| hairline | `#DCDAD2` | `#2A2E35` | dividers inside docked regions |
| accent | `#1D6A8E` | `#6FC3E3` | selection, focus, the primary action, the running thing |
| passed | `#3FA463` lamp, `#1F7A3E` text | `#4CBF6C`, `#7ED697` | completed, passed, CI passed |
| failed | `#DD4B3E`, `#B3261E` | `#E25A4D`, `#FF8E82` | failed, blocking, CI failed |
| unknown | `#DFA524`, `#8A5A00` | `#DFA524`, `#F3C45A` | unknown, waiting, halted, advisory |
| parked | `#9268CE`, `#6B3FA0` | `#A487E0`, `#CBB3F5` | waiting on a person |

Contrast, computed with the WCAG formula against each theme's canvas: light ink 14.9:1, ink 2 6.1:1, ink 3 4.7:1, accent 5.1:1, passed text 4.6:1, failed 5.6:1, unknown 5.1:1, parked 6.3:1; dark ink 14.9:1, ink 2 7.6:1, ink 3 5.3:1, accent 9.3:1, passed 10.5:1, failed 8.3:1, unknown 11.3:1, parked 9.9:1. White on the light accent is 6.0:1 and the dark accent's ink on it 8.9:1. Every text colour is above 4.5:1 on its canvas and on the float surface except dark ink 3 on the float (4.6:1) and light ink 3 on the float (5.5:1), both above the line. A lamp never appears without a word or a title beside it.

Type: Atkinson Hyperlegible Next for the interface, 14px on a 20px line, weights 400 to 700; it was designed for legibility at small sizes with unambiguous letterforms (a slashed zero, a tailed l), which is what a page of ids and numbers needs, and it carries tabular numerals, measured in the rendering test. JetBrains Mono for ids, commits, paths, logs and diffs at 12px. Numbers in the interface are set in the interface face with tabular figures, not in the mono face; the mono face marks a thing you could paste into a shell.

Spacing is a 4px scale. Page gutter 24px, region gap 22 by 26px, row height 38px in Needs you, node 160 by 44 in the schematic, sheet 480px, HUD 340px, command field 40px high and at most 620px wide. Radii: 10px on floats and shapes, 8px on rows, 6px on chips, 4px on keys.

## 5. Consequences stay explicit

A decision never fires from the routing. Jev may pick the action and even the choice ("approve and merge comp-c" routes to decide with choice approve_run at 0.96); the page then opens the sheet with that choice selected and shows the confirmation, which states the consequence in the words carried from round 1's checkpoint:

| Choice | Key | Does | Then |
|---|---|---|---|
| Approve and run | 1 | Pushes kstrl/factory/comp-c, opens its PR and merges it. | comp-c completes once the merge is confirmed; without gh it stays unpushed and the run says so. |
| Approve only | 2 | Records the approval and nothing else. | Nothing merges until the next factory run, which then behaves as above. |
| Reject | 3 | comp-c fails and its dependents are skipped. Nothing is pushed. | The branch is kept for you to read. |
| Send back to the engineer | 4 | The engineer runs comp-c again with a note that a human reviewer asked for changes; no reason is passed on. | Uses one retry; with none left, comp-c fails as on Reject. |
| Decide later | 5 | Leaves the question open. | The run waits at this point; the item stays under Needs you and the elapsed clock keeps running. |

The confirmation card repeats the chosen choice's two sentences, names the CLI command it is the same as where one exists (`ks inbox approve`, `ks retry client-commands --max-parallel 2`, `ks ci poll`, `ks queue pause`), and offers Confirm on Enter and Cancel on Esc. The line under the buttons says nothing runs before Confirm. The same shape carries the retry (the scope is stated before the button is offered: what is reset, what is kept, the limits it runs under, what is not repeated), the serve controls, CI polling, stopping a run, queuing a spec, and queuing a component request.

## 6. Views on request

A view is a typed spec, not code:

```
{ source: components, form: timeline, filter: failed, period: this_week, sort: cost, group: null, placement: float }
```

The sources are the readers PLAN.md section 6.2 already names. The forms are the catalogue: list, board, timeline, graph, meter, sparkline, table, number, diff. Jev fills the spec from the typed text through the argument Choices; code validates it (an unknown form or source cannot be produced, because the sets are closed), applies the filter, period, sort and grouping, and renders the form at once. "Show me failed components this week by cost as a timeline" was routed at confidence 1.00 with every one of its five arguments right, and renders as one mark per failed component placed by when it failed, sized by its cost.

Defaults when the text does not say: the form follows the source (costs draw as a sparkline, findings as a table, events as a timeline, CI as a list, anything else as a list); the period is all time; the placement is floating. A made view carries its spec as chips in its header, so the operator can read what was understood and re-issue a corrected command. Saved views persist as JSON files under `.kstrl/views/`; pinning docks one at the top of the page and keeps it there; a docked view can be unpinned or removed from its header.

## 7. When the catalogue cannot draw it

Jev picks; it does not build. When a request asks for a form the catalogue does not have, one Noul, carrying the catalogue in its question, judges whether the requested visual is outside it. Measured on twenty requests (`jev-bench/README.md`): every request inside the catalogue scored at most 0.11 and every request outside it at least 0.12, with four outside requests above 0.5 (word cloud 0.88, radar 0.71, pie 0.66, treemap 0.62). The margin between the groups is one hundredth, so the page does not treat the judgment as a switch:

- at or above 0.50 the page drafts a component request: the name, the data source, what it must show, and the gates it must pass, with the route drawn as four steps (request drafted, a kstrl factory run builds it, the gates pass, it joins the catalogue); Queue it adds the request to the ks serve queue through a confirmation; Use the nearest form renders the form Jev chose instead;
- between 0.15 and 0.50 the page renders the nearest form and offers the request in one line above it ("Not quite what was asked. Nearest form: table. Request a heatmap component");
- below 0.15 it renders the form.

The component is built by a factory run (kstrl building its own UI) or by another generative model, and enters the catalogue only after kstrl's gates. The prototype shows the request state and the queueing; it does not build anything, and says so on the panel.

## 8. Keyboard

`⌘K` or `/` focuses the command field; `↑` `↓` move in its panel; Enter runs the selected row; Esc clears, then blurs. At rest, `1` to `4` open the Needs you items in order, and `t` switches theme. In a decision sheet the number on each choice selects it, `↑` `↓` or `j` `k` move between choices, `r` starts a retry where one is offered, and Enter in a confirmation confirms. Esc closes a confirmation, then a sheet, then a HUD, in that order. The key hints are shown once, in a quiet line at the foot of the page, and on the controls themselves.

## 9. The phone

At 390px the page is the same page, laid out for one hand. The command field docks at the bottom within thumb reach and its panel opens upward. The strip keeps the project, the compact spend and the theme control. The regions stack in reading order: Needs you, the run, Main, History. The schematic becomes a list of nodes, each with its phase ring, its state line and its tier, because a five-column schematic at 390px is not readable. The phase timeline keeps its tracks with a narrower label column. A sheet is the whole screen with its decision foot above the command field; the HUD and made views span the width above it. Nothing scrolls sideways.

## 10. What the prototype does not do, and what is not measured

The prototype replays recorded Jev answers; it does not call the API. It holds no live event stream; every value comes from the fixtures. `selftest.py` drives the prototype's keyboard and command flows in headless Chrome and checks 21 behaviours this document promises (number keys, choice keys, the confirmation and Esc, routing a recorded phrasing, the low-confidence list, the untyped-target rule, making, pinning and removing a view, the component request, the not-recorded label, the theme key, the retry command); all 21 pass on the committed prototype. Text selection colour, the caret, focus rings and scrollbars are themed; hover states exist; the loading and error states of a real server (a reader that fails, a stream that drops) are not drawn and are a first-slice task. The docked view's persistence is a toast, not a file.

Not measured: the routing on a catalogue with hundreds of candidates (the design caps candidate lists at 40 by local substring match before asking Jev, which is a rule, not a result); the routing over a slow connection; how a second operator's phrasings route; whether the 0.80 threshold holds for a later model version (the threshold belongs to `jev-1.13.0` and this catalogue).

## 11. The second reviewer's rounds did not run

Both Codex critique rounds were planned and neither ran: the first request returned nothing because the Codex account had hit its usage limit, and the owner then stopped further use. No finding in this document comes from a second reviewer. The defects found and fixed by the designer's own review of the renders are listed in `review-record-v2.md` in the coordinator's scratch folder for this lane. The four questions the owner asked of round 2 (is it still TUI-derived, is it a wall of text, do the details float, is it polished to the standard of Linear and Raycast) are still open for an independent reviewer.
