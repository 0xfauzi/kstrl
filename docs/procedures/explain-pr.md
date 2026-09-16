# Explaining a PR

Canonical text for kstrl. The `explain-pr` skill wrapper points here; this
file carries the procedure.

**Applies after a PR is opened**, when the owner wants to understand the change
in relation to the whole system. **Does not apply** to reviewing a PR for
defects; that is a code review, and H1 forbids an AI doing it on its own work.

## What this produces, and what it must not produce

A lesson answers **what changed and why**. It does not answer **where a change
landed and how far it reaches**: that is a property of the tree, and a count
copied into prose rots the moment the tree moves.

So the lesson never restates a number, a module list or a reach the diff
already carries. It names the file and lets the reader read the tree. One
fact, one definition.

## The procedure

### 1. Fix the subject before reading anything

    gh pr view <n> --json number,title,body,headRefName,baseRefName,state
    git fetch origin <headRefName> <baseRefName>
    git merge-base origin/<baseRefName> origin/<headRefName>

Diff against the PR's own base; PRs stack. For a merged PR use `gh pr diff <n>`.
Read code from the remote refs (`git show origin/<headRefName>:<path>`), never
from the working tree, which may hold untracked or evolved copies. Every
module, class or function the lesson names must resolve at the head of the
range; a name that does not resolve is a claim the lesson cannot make.

### 2. Read the register

`docs/lessons/register.md` holds the glossary and one log entry per lesson.
A term already in the glossary is not re-explained or redefined. Check whether
the PR was taught before (a PR that grew under review gets a second lesson
teaching the delta), and check every glossary entry against the diff: an entry
the change contradicts is revised in place, and the new log entry says the
lesson reverses the old one.

### 3. Delegate to a fresh reader

Spawn one subagent to write the lesson. Do not write it in the session that
authored the PR. Give it only raw inputs: the PR number, title and body; the
diff command; `CLAUDE.md`; `docs/lessons/register.md`;
the two newest lessons in `docs/lessons/` as examples of the form; and this
file. Do not summarise the change for it. It marks every claim it took from
the PR body but could not confirm in the diff.

### 4. The lesson is one arc

1. **Locate.** The problem the change solves, in a few short paragraphs before
   any file name. Then a vocabulary table. Then the whole job as a picture the
   reader can walk.
2. **Justify.** One heading per decision, worded as a question a person would
   ask. Under each: the question in one or two plain sentences; a widget that
   lets the reader answer it by hand; then the first answer the author tried
   and what killed it. A change that rejected nothing is trivial or
   under-examined, and saying which is itself teaching.
3. **Operate.** What someone needs to know to make the next change here.
4. **Judge.** What would make this wrong, as retrieval practice: the reader
   commits to an answer before seeing yours.

Close Justify with what the change deletes and the rule each piece serves
(`CLAUDE.md`, or H1 to H4 in `docs/adversarial-roadmap.md`). Keep the whole
lesson readable in one sitting.

### 5. Draw it, and let the reader operate it

A lesson is mostly picture. Prose carries only the argument between pictures.
Reach for these in order: a widget the reader can operate; a figure, when the
thing has a shape but no input to move; a table, when the content is really
pairs; prose, last.

**Class A, pictures of the whole system.** Do not. No system map is
generated in this repository, and a hand-drawn one is a claim about the tree
that nothing checks. Point at `ARCHITECTURE.md`.

**Class B, pictures of a mechanism the change introduces.** This is where a
lesson's drawing goes: a rank rule, a retry loop with its budget, a prefix
assembly order. Author these inline as SVG: one idea per figure;
every box and edge labelled in the glossary's words; colour carries meaning or
is absent; legible on paper; no libraries, fonts or raster images.

**Class C, a widget the reader operates.** The reader moves the input the rule
reads, and the rule's answer changes in front of them. One rule per widget. A
"try this" line under it naming two moves and what each shows. The answer as a
number and as a sentence. The reachable range contains the interesting cases.
Invented data is labelled invented. Plain SVG and plain DOM.

**Verify every widget before shipping it.** A widget carries more authority
than prose because the reader watched it happen. Pull its rule into a
standalone script under `docs/lessons/verify/<lesson>/`, feed it the widget's
own data, sweep the range, and read the output against every claim the prose
makes. Two lessons elsewhere shipped widgets that could never reach the case
the prose promised; both fell out in seconds once the rule ran outside the
page.

### 6. Write it self-contained, into the repository

One HTML file at `docs/lessons/pr-<n>.html` (or `pr-<n>-part-<k>.html`).
Inline CSS and SVG, no fetches, no fonts to load, printable. Name a component
in `<code>`, never as a link to another page. A shipped lesson is immutable
except for a supersession banner, and for a link whose target has left the
repository, which is rewritten to plain text and recorded in the register.

### 7. Write it as a teacher

The reader knows this system and does not yet know this change. Teach the idea,
then name it. Say why it matters before any file name. Replace the repo's word
with a plain word and use it everywhere. Give permission before instructions
("drag things; nothing can break"). Flag the moment confidence drops. Never
perform rigour: let the reader press the button and read the answer.

Then run the checker until it exits 0:

    python3 ~/.claude/skills/explain-pr/scripts/lesson_lint.py docs/lessons/pr-<n>.html --glossary docs/lessons/register.md

It parses every inline script and judges authored prose; generated figures
carry `data-generated` and are skipped. It cannot judge whether a widget
teaches the truth; step 5 owns that.

### 8. Record what it taught

Add one entry to `docs/lessons/register.md` under `## Log`, newest last:

    ### PR <n>: <title>

    - file: pr-<n>.html
    - range: <base>..<head>
    - parts: <the modules the change reaches>
    - rules: <H1-H4, or the CLAUDE.md rule the change serves>
    - reverses: <earlier entry, or (none)>

    Then what the lesson taught, not what the PR did.

Add only new terms to `## Glossary`, defining what each term is. When a lesson
reverses an earlier one: `reverses:` on the new entry, `superseded-by:` on the
old entry, and a banner at the top of the old lesson. Then:

    python3 ~/.claude/skills/explain-pr/scripts/lesson_lint.py --register docs/lessons/register.md

## The known limits

The register records what was taught, not what the reader retained; ask them.
A lesson teaches the range it was given; a later lesson teaches the delta and
names what it reverses. The register's `parts:` are only as good as the
reading that produced them. The checker counts sentences, not teaching. The
fresh reader is fresh but not independent: it reads the same diff the author
wrote.
