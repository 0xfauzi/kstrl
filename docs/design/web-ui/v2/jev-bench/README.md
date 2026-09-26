# Jev as the router behind the command bar: the measurement

This folder measures whether Jev (TypeSafe's System One model) can route what an operator types into kstrl's command bar. The bars below were written before the first call, and the results section was filled in after, without moving a bar.

Files: `catalogue.py` holds the closed set of 24 routes and 15 argument questions; `phrasings.json` holds 86 labelled phrasings (83 in the main set, and 3 view requests added by `extra_routes.py` so the coverage examples below also have a recorded route); `bench.py` makes the calls and scores them; `raw.jsonl` is every request and response, without the API key; `results.json` is the scored output the prototype replays.

## What is measured

One request per phrasing. The state is `{"command": <text>, "known": {components, runs, waiting_items}}`. The request carries the route question (a Choice over 24 options, one of them `no_match`) and all 15 argument questions (each a Choice over a closed set with a "not named" or "unstated" option), so the answer comes back in one round trip. Code reads only the arguments that belong to the chosen route.

Every free value is selected, not generated: run ids and component names are the ones the page already knows, put into the criteria; durations and periods are closed sets.

The phrasings were labelled before any call. Each has a primary route and, where a phrasing is honestly ambiguous, a list of other routes that would also be a correct reading. Arguments are labelled with the set of values accepted. The set holds 52 plain and terse commands, 18 view requests, 8 that match nothing, 4 with typos and 4 ambiguous single words.

## Acceptance bars, stated before running

| Measure | Bar | Why this bar |
|---|---|---|
| Top-1 route, lenient (primary or an accepted alternative) | at least 85% | systemap measured 83% top-1 for Jev choosing a module owner among about 20 cards; a command palette's options are more distinct than module ownership, so it should do at least as well. Below 85% about one command in seven misroutes, and the fallback list (the top alternatives) would be doing the routing. |
| Top-1 route, strict (primary label only) | at least 78% | The gap between strict and lenient is the share of phrasings that are ambiguous by construction; 7 points is the size of that share in the set. |
| Argument accuracy on correctly routed commands | at least 80% | A wrong argument produces the wrong view or acts on the wrong item. The function-calling cookbook shows argument confidences from 0.53 to 0.99 on 14 commands with no wrong argument; 80% leaves room for the "not named" cases, which are the hard ones. |
| No-match recall (of 8 no-match phrasings, routed to `no_match`) | at least 75% | A no-match that is routed somewhere becomes a wrong action offered; the confirmation step catches it, but the page should not offer it. 6 of 8 is the floor. |
| False no-match rate (real commands routed to `no_match`) | at most 5% | A real command answered with "nothing matches" is the failure the operator feels most. |
| Latency p50, end to end from this laptop | at most 0.6 s | systemap measured 0.27 s p50 for one Choice over about 20 cards. This request carries 16 questions and a larger state. The palette filters locally at once; the Jev answer must arrive before the operator has read the local matches, which is under a second. |
| Latency p95 | at most 1.5 s | Above about a second the operator notices the wait and needs a progress indicator; 1.5 s is where the design must show one. |
| Input tokens per request | at most 6,000 | systemap measured about 2.4k for a Choice over about 20 cards. 16 questions with descriptions should fit in 6k. At $0.042 per million input tokens, 6k is $0.00025 per command, or 2.5 cents for a hundred commands a day. |

A bar that fails is reported as failed, and the design carries the fallback; the bar is not moved.

The confidence threshold is not a bar but a measurement: the sweep reports, for each threshold from 0 to 1, how many right and how many wrong routings have route confidence at or above it. The threshold the prototype uses is the lowest one that keeps no wrong routing, if such a threshold exists, and the design says what happens below it.

## Results

Run on 2026-09-26 from a laptop on a home connection, model `jev-1.13.0`, 86 phrasings: 83 in two passes (166 paid calls plus one warm-up) and 3 in one pass. Accuracy is scored on the first pass; latency and tokens on every call (169). `summary.txt` is the script's output and `results.json` the per-item detail.

| Measure | Bar | Measured | Result |
|---|---|---|---|
| Top-1 route, lenient | at least 85% | 95.3% (82 of 86) | pass |
| Top-1 route, strict | at least 78% | 87.2% (75 of 86) | pass |
| Argument accuracy on correctly routed commands | at least 80% | 96.6% (85 of 88 labelled arguments) | pass |
| No-match recall | at least 75% | 100% on the 8 pure no-match phrasings; 80% when the two ambiguous single words labelled no-match ("why", "run") are counted, since both were routed to an accepted alternative | pass |
| False no-match rate | at most 5% | 0% (no real command was routed to `no_match`) | pass |
| Latency p50 | at most 0.6 s | 0.241 s | pass |
| Latency p95 | at most 1.5 s | 0.288 s (max 0.536 s) | pass |
| Input tokens per request | at most 6,000 | 3,065 mean, 3,072 max | pass |

By kind, lenient top-1: terse 12 of 12, plain 33 of 36, view 18 of 18, no-match 8 of 8, ambiguous 4 of 4, typo 3 of 4.

The four misses, with the route confidence:

- "spend on live01" went to `open_run` (0.79); `ask_cost` was second at 0.13. The run id pulled the routing toward opening the run.
- "whats queued" went to `open_decisions` (0.62); `open_serve` was third at 0.05. "Queued" was read as the operator's queue of decisions, not the serve queue. The catalogue description of `open_serve` should name "queued" plainly; this is a prompt fix and is left as measured.
- "decide later" went to `snooze` (0.74); `decide` was second at 0.17. Deferring a decision and hiding an item are close in meaning, and both are harmless: neither pushes, merges or spends.
- "cst of live01" (typo) went to `open_run` (0.32); `ask_cost` was second at 0.34 probability, so the two were within 0.02 of each other and the confidence was low.

The three wrong arguments: "approve only, don't run anything" and "aprove the merge" both filled `target_item` with the comp-c checkpoint when no item was named (confidence 0.92 and 0.94, which is the case the design must not trust: a confident guess at a target that was never typed). "list runs sorted by cost" chose `costs` as the data source over `runs` (0.54). The design's answer is that the confirmation step always shows the target it resolved, and that a `decide` whose target was not typed opens the decision list rather than a specific item when more than one is open.

### Confidence

Route confidence separates right from wrong on this set. The median confidence of a right routing is 0.96, of a wrong one 0.68. The sweep:

| threshold | right kept (of 82) | wrong kept (of 4) |
|---|---|---|
| 0.50 | 75 | 3 |
| 0.70 | 70 | 2 |
| 0.75 | 70 | 1 |
| 0.80 | 65 | 0 |
| 0.90 | 60 | 0 |

The lowest threshold with no wrong routing kept is 0.80. Seventeen right routings sit below it, and they are the honest cases: single words with more than one reading ("inbox" 0.43, "ci" 0.47, "merge" 0.27, "comp-c" 0.76), view requests whose form was implied rather than said ("a meter of spend against the cap, dock it" 0.25, where `make_view` was still first), and two no-match phrasings ("hello" 0.65).

So the design treats 0.80 as the line: at or above it the command bar shows the routed action first, ready to run on Enter (a decision still goes through its confirmation). Below it the bar shows the top three options from the probability distribution as a list and the operator picks; nothing runs on Enter alone. This threshold belongs to `jev-1.13.0` and this catalogue; a new model version or a changed catalogue means a re-run.

### What this measurement does not show

It is one operator's phrasings, written by the designer, on a catalogue of 24 routes. It does not measure a catalogue of hundreds of runs and components (the candidate lists here have 10 components and 8 runs; the design caps a candidate list at 40 by local substring match before asking Jev). It does not measure a slow network. It does not measure what happens when the same phrasing is sent twice: the two passes were not compared answer by answer.

### Can Jev tell when the catalogue cannot draw what was asked

`coverage_bench.py` measures the judgment behind the new-component state. One Noul asks whether the requested visual form is outside the catalogue of nine forms, with the catalogue in the question. Twenty view requests, ten inside (list, board, timeline, graph, meter, sparkline, table, number, diff, and "bar chart", which the sparkline entry covers) and ten outside (heatmap, sankey, calendar, treemap, pie, file map, word cloud, gauge cluster, scatter, radar), labelled before the calls. Bar, stated before running: at least 8 of the 10 outside requests judged outside at the threshold that keeps every inside request inside.

Result: every inside request scored at most 0.11, and every outside request at least 0.12. At a threshold of 0.15 all ten inside requests stay inside and 9 of 10 outside requests are caught (calendar scored 0.12). The bar passes, but the margin between the two groups is 0.01, which is no margin. Only four outside requests scored above 0.5 (word cloud 0.88, radar 0.71, pie 0.66, treemap 0.62); heatmap scored 0.27 and was given `table` as the nearest form.

So the design does not treat this Noul as a switch. Above 0.5 the page drafts the component request. Between 0.15 and 0.5 it renders the nearest form Jev chose and offers the request beside it in one line ("Not quite a heatmap. Request a heatmap component"). Below 0.15 it renders the form. `coverage_summary.txt` and `coverage_raw.jsonl` hold the run.

### Cost

At $0.042 per million input tokens and 3,065 tokens per command, one command costs $0.00013. The 190 calls of this folder's runs (169 routing calls, one warm-up, 20 coverage calls) cost about $0.02.
