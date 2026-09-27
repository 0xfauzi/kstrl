Proposal: how an ask arrives while you are in the app. The Factory level at 21:28:04, the moment kstrl parks search-index for approval.

**What changes on screen, and nothing else:** a notice card at the top right, the new ask fading into Needs you, the value it changed ("no merge parked: search-index") marked where it changed, and the tab title counting what waits on you, "(2) kstrl". The layout does not move.

**The card.** Kind, one line of what it is, one line of what waits on it, and two actions: Review (primary) and Later. It never offers Approve. You approve where the evidence is (the inbox item or the approval view), so a card can't turn into a one-tap merge. An ask's card stays until you pick one; Later removes the card and leaves the ask in Needs you. A notice (health, calibration) never gets a card.

**Built on**: a new line in `inbox.jsonl` (kind, part, evidence) and the `checkpoint_resolved` event with `decision="parked"`. The app watches both; kstrl sends nothing to the app.
