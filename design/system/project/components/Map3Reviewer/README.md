Proposal, step level, one checker: what `↵` on a reviewer opens. search-query's review on try 2: its verdict criterion by criterion, beside what the engineer claimed.

**Why this shape.** A checker's value is its disagreement with the builder, so the page is organised by the thing they disagree about: each story, with the engineer's claim ("done", from `prd.json` `passes`) and the reviewer's verdict on the same line, and every criterion under it. A story with a failed criterion gets the `fail` rule and outline; the criterion text stays in ink, because colour is for marks. The ink tile states the disagreement in one sentence.

**What the reviewer returns** (`REVIEWER_PROMPT`, version 2.0.0) is JSON: its own diffstat (`observedDiffstat`, mandatory), each story's criteria with a verdict (`pass`, `fail` or `advisory`), an explanation and a suggestion, `concerns` in eight categories (`scope_creep`, `security_concern`, `test_quality`, `unrelated_change`, `dead_code`, `error_handling`, `copy_paste`, `other`), `exhaustively_searched`, and `overallNotes`. A review passes when no criterion and no concern is `fail`.

**What kstrl does with it, shown on the screen:**
- It checks the reviewer's diffstat against git. A mismatch always adds an advisory concern, and in hard mode it voids the review as an infrastructure error. That is how kstrl knows the reviewer read the right change.
- A story the engineer marked done that the reviewer does not pass is a `claim_disagreement` finding. It blocks when `claim_agreement = "block"` or the ladder is at L1 or above, which it is here. When the review otherwise passes, kstrl sets the story's `passes` back to false in `prd.json` with a note; here the review failed anyway, so the part goes back with the findings.
- `exhaustively_searched` is the reviewer's own claim, and the screen labels it that way.

**Tabs.** Verdict is the parsed output. Log is `review.log`: the git commands it ran (`[Bash] git diff …`) and then its JSON, with every try appended and no separator. Prompt is `prompts/<part>/review-a<try>-c<call>.json`.

**One gap to know.** A review runs in the factory's parent process with no timeout (`[timeout] review_agent` was removed), and the scheduler's backstop covers only worker processes. A review that hangs is bounded only by the output-size cap. The screen shows its age like any agent's ("no output for 2m").

**Built on**: the manifest's `reviewFindings` (which keeps `- [pass]` lines as well as failures) and `findings[]`, `review_result`, `finding_recorded`, `prd.json` claims, `review.log`.
