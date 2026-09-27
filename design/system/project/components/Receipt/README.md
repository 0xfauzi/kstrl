Proposal: the receipt of a finished spec. It is the Spec level's third view, Receipt, once a spec has finished, and it answers four questions in this order: what did I get, is anything wrong with it, how much did it need me, what did it cost.

**What you got** is the left sheet: your spec's first sentence, then every part in the order it merged, each described in the words the architect gave it in the plan, with its tries, PR and CI. A part an open finding concerns carries the finding's id.

**The one thing to know** takes the ink tile. For search it is the integration review's finding: accents are folded two ways. Blocking is off by default, so it was recorded and nothing was built from it; the tile says so and offers the path kstrl has, a follow-up spec added to the queue.

**How much it needed you.** At L2 under `ks serve` every merge parks, a run ends when every part left is waiting on an approval, and kstrl applies approvals only when a run starts. So your visits are what move the spec forward. The strip shows the five factory runs over the spec's 2h 51m, with a `you` diamond where your visit started one. search needed 8 answers in 4 visits. That is the evidence for, or against, moving to L3.

**Cost and CI**: spend by role from `component_usage`, and CI read only on the merge commits kstrl recorded.

An earlier version drew every part's time as a Gantt chart. It was replaced: it asked the reader to decode a legend to learn one fact (your visits start the runs), which the strip shows directly.

**Built on**: `manifest.json` (parts, descriptions, tries), `pr_merged` events, the CI ledger, `.kstrl/runs/<run>/integration/review-1.json`, the run directories (one per factory run) and `inbox.jsonl` decisions for the visits, `component_usage` by phase.
