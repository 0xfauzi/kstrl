Proposal: settings. `kstrl.toml`, organised by the decision each setting governs, with every value's source and what it does, and the problems kstrl would refuse to start on.

**Why this shape.** kstrl reads 29 sections and about 167 settings, and most people will touch perhaps a dozen of them. So the page groups settings by the question a builder has ("how is work checked", "how much runs at once", "money", "trust", "the queue", "notifications", "what engineers read", "the agent"), each group counts its settings, and the rest go under "Everything else" (116, most never touched). Each row gives a plain name, the real `[section] key`, the control, its source (default, kstrl.toml, or the environment), and one line of consequence where the consequence isn't obvious.

**What the screen has to say, from the code:**
- Every value's source follows kstrl's own rule: environment over file over default. An environment override gets its own tile, because it silently wins over the file.
- Any bad value, unknown key or retired name refuses every command (only `[evolution]` just warns). The problem tile says exactly that, names the key, and offers the correction kstrl's own message suggests.
- Security review is off by default (`[security] mode = "skip"`); a project that turns it on shows the row as set in kstrl.toml.
- `claim_agreement` defaults to advisory but blocks at L1 and above, so the row says which applies here.
- `[policy] enabled` off keeps the level in force at L2 or below; the Trust page says the same.

**Saving.** The app writes kstrl.toml at the repository root, and runs the same checks every command runs before writing, so a value that would stop kstrl cannot be saved. That is the app's behaviour. `ks config show` only reads, lists 17 of the 29 sections, and has no rows for `[agent] budget_usd` or `[factory] claim_agreement`; the TUI's config screen is read-only.

**State outside the repository** (the footer): the inbox, the trust level, spend, the queue pause and the CI ledger live in `${XDG_STATE_HOME:-~/.local/state}/kstrl/<repo-id>/`, where `<repo-id>` is a hash of the origin URL, so every clone of the same remote shares them.

**Built on**: the loaders registered in `config_preflight.py`, `config_report.py`, the preflight problem messages (`config_keys.py`, `config_preflight.py`), `statedir.py`.
