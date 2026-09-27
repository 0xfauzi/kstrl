Proposal: learning. What an engineer reads before it builds a part, in the order kstrl assembles it, which of those you write, and what kstrl has noticed across runs.

**Why this shape.** "Learning" in kstrl is not one mechanism. It is the context every engineer starts from, so the screen shows that context in its real order (`factory.py`, the operator-context assembly):
1. Facts from earlier parts (the distiller's output, in three tiers: this part in full within 2,000 tokens, its dependencies in full within 1,000, one sentence from every other part within 500).
2. Golden patterns, `scripts/kstrl/golden-patterns.md`, up to 6,000 characters, the start kept.
3. The architect's decisions that bind the part.
4. The codebase scan.
5. The retry context, on a retry only.
6. Your guidance, `scripts/kstrl/memory.md`, up to 4,000 characters, the newest `## Guidance` entries kept. It comes last on purpose, so it is read after what the controller said.
7. Then CLAUDE.md and the engineer prompt.

This stack is what a factory part reads. A one-off `ks feature` run passes no context prefix, so its engineer reads only layer 7.

The layers you write carry a rule and an action. All of them are read once, when a part's try starts, so a change reaches the next try and not the running one.

**What kstrl noticed** is `ks evolve`: a failure signature seen at least twice in the last 10 runs. Review, security, verification and contract patterns are routed to "candidate lessons", but the playbook that would hold them has no writer or reader in kstrl today. The screen says so, and offers the one path that reaches every engineer: a line in your guidance. A `/memory` comment on a kstrl pull request appends one too (500 characters at most).

**Facts** are counted by confidence (`review_passed`, `test_verified`, `asserted`; a fact is downgraded to asserted when review did not pass). "Referenced" is `measure_fact_utilization`: the first 30 characters of a fact found in the engineer's progress notes or added lines. That undercounts paraphrase, so it is a lower bound.

**Built on**: `.kstrl/knowledge/<part>/<run>/*.md`, the operator files in `scripts/kstrl/`, `ks evolve` output, and the per-tier utilization counts.
