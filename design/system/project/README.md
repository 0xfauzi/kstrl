kstrl builds software from specs. You write a spec; an architect turns it into parts with dependencies; engineer agents build each part; something that did not write the code measures it; you decide what merges. This system designs that experience: a map of the factory you can zoom into, a command window that reaches every action and question, and your spec as the thing you edit.

The sections after this one describe how the factory runs underneath (its loops, states and decisions). Read them before designing anything that shows a number or offers a choice.

## Principles

1. **You work in specs and the plan, not code.** You write a spec; the architect turns it into parts; engineers build them; something that did not write them measures them; you approve what merges. Code is one level down (Open the diff) and never the default view.
2. **kstrl's own structure is the layout.** The map is the plan, live: the dependency graph of parts from `manifest.json`, and inside each part the line it moves along (Build, Check, Your approval, Merge). No shape is drawn that the factory does not have.
3. **Zoom, do not navigate.** Four levels: Factory (built, building, next), Spec (its parts as a graph, or its text), Part (every try across the line), Step (one check on one try). `⌘+` and `⌘−`, scroll, or `↵` on a selection move one level. Each level shows more about fewer things, with the level above kept in a small map.
4. **⌘K reaches everything.** Every action, and the questions kstrl can answer from its own records: why did this stop, what is waiting on me, what did it cost, what did the architect decide, what runs next and why not yet, is anything not being checked.
5. **Every line says who wrote it.** Your words, an agent's words and a measurement each have their own typeface (see Voices). A claim is never set in the measurement voice.
6. **A claim sits beside the measurement that checks it.** The engineer's per-story claims sit beside what verify, review and security found. A sensor that did not run is drawn as an absence, never as a pass.
7. **Every action says what it will do before you do it,** naming the parts it affects: "search-index fails and search-api is skipped. Nothing is pushed."
8. **Rufous is yours.** `you` marks only what waits on you and the one action you are about to take.
9. **Only what kstrl does.** Every figure is a record kstrl writes and every action calls a path kstrl has. Where kstrl does less than a person would expect, the screen says so at the point it matters ("Your own words are not passed on").

## Voices

Three families, one per author. Choose the family by who produced the text, never by emphasis.

| voice | family | styles | use for |
|---|---|---|---|
| You | `human` (Instrument Serif) | `intent-display`, `intent-xl`, `intent`, `intent-sm` | spec sentences, your answers to questions, your guidance, your decisions ("Approve."). Always quoted exactly, in curly quotes, with a source line under it in `label`: "you, in specs/snippetvault.md". |
| An agent | `agent` (Instrument Sans) | `statement-xl`, `page-title`, `statement`, `title`, `input`, `body`, `small`, `label`, `micro` | the interface itself and everything an agent said: plans, claims, decisions it made, explanations. |
| Measured | `measure` (Geist Mono) | `stat-lg`, `stat`, `stat-sm`, `measure-code`, `measure`, `measure-inline`, `measure-small` | anything a tool counted or recorded: test counts, durations, diff stats, spend, commit ids, paths, branch names, keycaps. |

The input is set in `input` while you type a command and switches to the `human` family once what you type reads as a sentence: the window is showing you that you are now writing intent.

**The scale.** Those 20 steps are the only type there is (the type foundation lists each with its size, line height, weights and tracking). Pick a step by role, never by size: its usage says what it is for. A page writes `font:var(--t-<step>)` and `letter-spacing:var(--t-<step>-ls)`, restating a weight only when the step allows it; a size variant of a component (a small button) changes `font-size:var(--t-<step>-size)` alone, so its base rule's weight and line height hold; a run no component covers takes a class, `.ty-<step>`. A measurement inside words (`v-measure`) sits one step below them, 11 in a 12px caption and 12 in 13px text, because Geist Mono reads larger than Instrument Sans at the same size. Before the scale the cards had 50 family-and-size pairs and 166 styles on 3,226 runs; after it, 23 pairs and 53 styles, and the only sizes off it are drawings of other apps and the wordmark.

## Colour

- **Ground**: `canvas` behind the window; `window` for the window; `raised` for inputs, evidence blocks and the action panel; `selected` for the selected row. `line` is a hairline and never the only edge of a control. `line-strong` edges keycaps and buttons, which their label identifies. `line-input` is the edge that alone shows where an input is (a field, a toggle's off track): at least 3:1 on every ground in both themes, because at night `raised` is 1.09:1 on `window` and a fill identifies nothing.
- **Text**: `text` for primary, `text-2` for subtitles and consequences, `text-3` for section labels, timestamps and keycap legends.
- **`you`**: the ask mark, the primary action's label or fill, the command window's focus ring. `you-tint` is the background of a selected ask. Text on a `you` fill is `you-ink`.
- **Selection on the map** is a `text` ring, 2px, 2px off the tile, card or cell, and keyboard focus on anything selectable is the same ring (on the map, focus and selection are one thing). It is not `you`: a selected part is not waiting on you, and the rufous focus ring is for controls.
- **States**: `pass` a measurement agreed or a part merged; `fail` a measurement disagreed or a part stopped, and the one destructive action in a panel (Reject); `work` an agent is working now, and `work-tint` fills an agent's working time (iteration blocks). A state colour always travels with its mark and a word.
- **Themes**: `day` is the default; `night` is the alternative, chosen by the person or by the system setting. Design every screen in `day` first and check it in `night`. Every text pair is at least 4.5:1 in both themes; the weakest is `text-3` on `selected` (4.58:1 night, 4.56:1 day).
- `fail` and `you` are close in lightness at night (1.18:1 between them). They are told apart by mark (cross against diamond) and by position, and must never appear as bare colour.

## Craft

These are what make a screen read as designed rather than assembled. Check every frame against them.

- **One column grid per screen.** Every row (axis, lanes, footnotes) uses the same columns, so a label, a plot and a caption line up from top to bottom. Pad cards 20px on the sides; separate rows by 8px.
- **Text beside text sits on one baseline.** A name and its caption, a label and its value, a number and the words after it: a row of them is `align-items: baseline`, and a mark, a dot, a chip or keycaps in that row are boxes, centred on it (`align-self: center`). Before this rule the check found 136 pairs in 28 cards off their neighbour's baseline: 105 by 1px (text of two sizes centred), 11 by 2 to 3px (top aligned), and 20 by 7 to 8px (a row's age or try count centred on a two-line row, beside its title). A row led by a box taller than its text (the Needs you bar's 44px asks) stays centred on that box, and a grid of drawings keeps its columns' text on one baseline while the drawings centre.
- **Tiles in a row end together.** They share the row's height, so the shorter one's content stops short of its foot: at most 40px under text or controls, where a footnote moves to the tile's foot instead, and 80px under an anatomy drawing, where whitespace frames the diagram. Rebalance by moving a section to the other tile, laying a list in two columns, or giving a filmstrip its own full-width tile, never by padding. An anatomy area is its drawing plus 6px clear above and below (it had been 1 to 35px). Before this rule Keycap and Ring each ended 110px short.
- **Five steps carry a dense screen**: `label`, `small` and `body` in the agent's voice and `measure-inline` and `measure-small` in the measurement's; all 25 frames use all five. Every other step is there for a role (the name or sentence the frame is about, a stat, a chip's `micro`), and a frame uses 6 to 15 steps in all. Half-pixel sizes (10.5, 12.5, 13.5) are gone: each rounded to the step whose role it had. Lowercase is never letter-spaced.
- **Colour only where work is live.** Past work is `selected` on `window`; only the thing happening now gets `work` (outline plus a 45° stripe of `work` over `work-tint`); a failure is a single `fail` block or rule. The eye should find now before anything else.
- **Grids are hairlines, dashed `1 3`, in `line`**, behind the content, never through it.
- **Accents are small**: a fast-check result is a dot (`k-dot`, 6px, the size at which its ring variant still reads as a ring), not an icon. A mark earns an icon only where it is the subject.
- **Write to fit.** A caption, label or note is written for the space it has. An ellipsis is a failure of writing, allowed only for text kstrl supplies at run time (a log line). Text never ends on one word alone: the foundations set `text-wrap: pretty` once, on the root, so every wrapping line inherits it; a short quote of your words in a list (`intent-sm`) balances its lines instead (`text-wrap: balance`), so two lines are near equal rather than a line and a tail; a name a new break would split at its hyphen is a `v-token`, which the audit checks.
- **Surfaces lift by one step**: `window` cards on `canvas` with `shadow-card`; a segmented control's selected item is `thumb` on a `selected` track (`thumb` is `window` by day and a step lighter than the track at night, where `window` would read as a hole).
- **Radius follows size**: tiles `radius-xl` (18px), dense tiles, part cards and the command window `radius-lg` (14px), grid cells (`k-tile-cell`) and wells (`k-well`, evidence, a log excerpt, code) `radius-md` (10px), keycaps and chips `radius-sm` or less.
- **One loud tile per screen.** The ink tile (`text` fill, `window` type) holds the single statement that most needs reading: a checker's latest words, or the finding that sent a part back. Idle things (the queue, parts nobody is working on, an empty question slot) sit in dashed tiles.
- **Role chips name who is working**: `engineer` in `work` on `work-tint`, a checker (`reviewer`) in `window` on `text`. No chip when no agent is working.
- **Big numbers are measurements**: `stat-sm`, `stat` and `stat-lg` (22, 30 and 44px `measure` at 600 and `-.03em`), with the unit or denominator small beside them in `text-3` and untracked: letter-spacing inherits as a length, so a 13px unit that does not reset it is set 0.05em tight under a 22px number and 0.07em under a 30px one.
- **Every number adds up.** A summary row counts what the screen draws.
- **Page classes never redefine the bundle's global classes.** Everything the bundle defines is namespaced (`k-` components, `v-` voices, `t-` text colours such as `t-fail`) except `grow`; a page stylesheet that names `grow` restyles it for the whole page. State words (`wait`, `you`, `sm`) are free for page classes: the marks' variants only apply together with `k-mk`.
- **Compose from the components.** A page uses `k-button`, `k-key`, `k-seg`, `k-tabs`, `k-toggle`, `k-field`, `k-tile`, `k-well`, `k-chip`, `k-stat`, `k-meter`, `k-bar`, `k-ref` and `k-hl`, `k-dot`, the ring, `k-steps`, `k-card`, `k-row`, `k-choice`, `k-need`, `k-notice`, `k-fresh`, `k-typing`, and the frame's bands (`k-header`, `k-titlerow`, `k-needs`) and the command window (`k-cmd`), and may add layout (margin, placement, width) around them, never its own colours, sizes or states. Where a component has sizes, the page picks one: a tile is dense, default, hero or flush, and a page never sets a tile's padding (the frames had drifted to 28 different ones). Every page-made copy found so far had drifted: 30, 26 and 24px buttons the system does not have, chords 8px apart instead of 4, a selected segment drawn as a dark hole at night, idle tiles drawn solid where the rule said dashed, state edges at 1px on some tiles and 1.5px on others, and a stray `work` dot at 12 o'clock on every iteration ring.
- **A page paints nothing of its own.** Whatever has a fill, a shadow or an edge is a component; the rest of a page is layout and 1px `line` rules. Two exceptions, each named where it appears. The card scaffold's surfaces: `c-tile`, `c-ink`, and `c-stage`, a patch of `canvas` with a 1px `line` edge for a component shown where it lives (a band, a notice, the command window). And illustrations, drawings of what is not kstrl (a browser's tab, an operating system's banner, cap-height guides), which sit inside `data-illustration="what it draws"` and are each drawn one way wherever they recur. The audit's painted check enforces this and lists every illustration for a person to read.
- **A band says what gives way.** The header and Needs you stay one line: as the screen narrows they set parts aside in a stated order (`data-fit-drop`, see Chrome) and never clip; the title row wraps its facts under the title instead. Checked at 1280, 1024 and 768 at every level.
- **Hidden means hidden.** The bundle makes `hidden` win over a component's own display, so a filter or a band can hide what it hides.
- **Every character comes from a shipped font.** Instrument Sans has no arrows, no `≥` and no key symbols; Geist Mono has arrows and `≥` but no Command, Option or Control. So a key is a keycap (never a symbol in running text), a spend total is set in `measure` so its `≥` is drawn, and statistics are written in words as kstrl writes them ("3 sigma"). The audit checks every character against the font its element asks for.

## The map

- **Every level has the same title row**: the name on the left (a spec in `human` at 32px, a part in `agent` at 26px semibold, with the working role as a chip) and its meta in `text-2`; on the right, the view tabs where a level has two views (Graph and Text, Stage and Grid), then the zoom control (Factory, Spec, Part, Step) as a segmented control. The header's crumbs say the path; there is no mini map.
- **Factory level**: tiles. Built on the left (each spec as the small shape of its plan, with a total), the spec being built as the hero (its plan with every part named and in its state, and four counts), the queue in a dashed tile on the right with `ks serve`'s admission checks under it, and the factory's conditions as a row of four tiles (trust, spend today, main, learning).
- **Spec level (graph)**: parts as cards laid out by dependency, left to right; edges solid when the dependency has merged, dashed while it is pending, `fail` when it has failed. A card: mark and name, the working role (or the state when no agent works), four segments for the line (done in `text-3`, current in `work` or `you`, stopped in `fail`), one sentence of what is happening, and its tries as dots. The selected card carries the selection ring and `↵`.
- **Spec level (text)**: a document, not tiles. Your spec in `human` at reading size on one sheet, the architect's decisions as cards pinned to the words they resolved, the parts it made at the foot of the sheet, and a dashed tile where an escalated question would wait.
- **Part level**: one row per try under the station columns; a return line in `fail` from the check that sent it back to the next try's Build; untried stations are an outlined empty cell, stations a try never reached are a dotted rule. The selected cell opens below as tiles: its most serious finding in the ink tile, the next finding, and the engineer's claim with what you can do.
- **Step level**: full screen, in two modes of the same moment. **Stage**: the agent that spoke last takes the stage, its words large, the part's checks and stories beside it, every other working agent a tile below; it follows whoever spoke last and `P` pins it. **Grid**: tiles of different sizes, the run in the large tile (agents working, the plan with working parts lit, counts), one tile per agent (a ring for iterations or the check sequence, and its last line), and a checker's latest statement in the widest tile. `↵` on an agent opens **one agent**: everything it has written in this try (its log, its notes, the prompts it was sent), with its iterations and stories as kstrl recorded them beside the text. The log carries no times or iteration markers, so that structure is drawn beside it, never inside it.
- The needs-you strip is always at the bottom. Its right side carries the one hint that matters on that screen.

## Pages beside the map

The map has four zoom levels. Everything else is a page one step off it, and each page opens from the place on the map that raises its question, as well as from ⌘K. There is no sidebar.

| page | opens from | what it answers |
|---|---|---|
| Inbox | the Needs you strip | what waits on me, and what each answer does |
| Notifications | ⌘K | what reaches me, where, and what does not |
| Queue | the Factory level's Next tile | what runs next, and adding a spec |
| Trust | the Trust tile | how much kstrl may do without me |
| Spend | the Spend tile, the header's spend | what it cost, against which limits |
| Learning | the Learning tile | what engineers read, and what kstrl noticed |
| Health | the Main tile, a health notice | is the factory working as it usually does |
| Set up | the first run, ⌘K | how kstrl will check the work here |
| Settings | ⌘K, a problem notice | what each setting decides, and where its value comes from |
| A feature run | the Needs you strip, ⌘K | a one-off `ks feature` run: understood, implemented, repaired |

A page's title row has no zoom control, and `esc` returns to the map where you left it. A finished spec's receipt and a stopped plan's question are not pages: they are views of the Spec level (Receipt, Text).

## Frames: what is wired and what stays a picture

A frame is one screen at one moment. Frames drawn at the same moment (the same spend today, the same last event) are wired together: in each of them, a crumb, zoom level, view or need whose destination is drawn at that moment goes there, and the control you used keeps focus in the new screen, its thumb or bar sliding from where it was. A control whose destination is not drawn at that moment stays static, because following it would change the time as well as the place. Every frame still opens on its own screen, so its card shows what its name says.

**Wired: search being built, ≥$31.10 today, last event 3s ago.** Map0Factory, Map1Spec, Map4Plan, Map2Part, Map3Reviewer, Map5Question, Map6Approve, Notify2Paths, Notify3Channels, and the pages Inbox, Learning, Queue, Settings and Trust.
- The zoom moves between the four levels: the Factory, search as a graph or as text (whichever you left), search-query, and search-query's review on try 2. The crumbs zoom out, and a page's root crumb lands on the Factory.
- Graph and Text switch the Spec level's view. One event and Every event switch Notifications.
- Approve search-index, in Needs you, opens the approval over the screen you are on; nothing is approved from the strip. On the Spec level the ask field and ⌘K open the command window with its questions about search.
- A window: the page under it is inert, Tab stays inside, and esc or the scrim closes it, with focus back on what opened it, or on the ask field when nothing did.

**Wired: the Step level at 20:46, ≥$19.40 today.** Map3Step and Map3StepGrid switch between Stage and Grid. In Map3Agent, the crumb "being built" returns to the view you left.

**Static inside the wired frames**, because the destination is not drawn at that moment: search-highlight stopped (its Part level); the zoom, and the crumbs above "being built", at 20:46; the views Notes and Prompt (one agent), Log and Prompt (the reviewer) and the pages' own views (Inbox, Learning, Trust, the Settings sections); the ask field and ⌘K off the Spec level, because the questions the window lists are about search. Inside a window only esc, the scrim and Tab are wired: its rows, choices and actions are specified working in the CommandWindow and Choice cards. `↵` on a part, an agent or a tile, and the tiles, cards and rows themselves, are not wired.

**Static frames.** Escalation (planning stopped at 00:04), FeatureRun (a feature run, last event 1m ago), Map3Grid8 (import, eight agents at 10:14), Notify1Arrive (an ask arriving, ≥$29.36) and Setup (no runs yet) are each the only screen drawn at their moment. Receipt, Health and Spend share the end of the day (22:31, not live), but no Factory is drawn then, and none of their crumbs, views or needs leads from one of them to another. Their controls keep their focus order and single tab stops, and do nothing. The Chrome card is where the header, the title row and Needs you are specified working at every level and at three widths.

**How it is checked.** Every state a wired frame reaches in one move is the static frame of that screen, pixel for pixel: 55 states in each theme. The one exception is inside the zoom's and the views' own boxes, where the script places the thumb and bar at whole pixels and the static frame paints the item's box: 8 to 88 pixels differ there, from an offset of at most 0.47px, and the interaction tests hold both within 1px of the item. `interact.py` drives the wiring by keyboard and pointer, 55 checks in each theme. Wiring found three defects, all fixed: Map2Part's grid, a positioning layer over the whole screen, sat above its title row and took every click meant for the zoom; Map3Reviewer and Learning said nothing needed you while search-index waited for approval at the same moment, and Needs you is the app's, not the level's; and the audit's collision check hit-tests, so it could not see an inert page under an open window and read the window as text over text. It now lifts `inert` while it measures, held by a clean fixture and a mutation.

## Live

Every level updates while the factory runs. The rules:

- **The layout never moves.** The plan fixes where every card is for the whole run; events change what is inside them.
- **What changed is marked where it changed**: a `selected` background on exactly that value, fading in 2 seconds. That fade, ticking ages and a typing indicator (three dots, while an agent's output is growing) are the only motion.
- **Say whether the view is live**: "live · last event 3s ago" in the header while `events.jsonl` grew in the last 60 s; otherwise "not live · last event 21:14".
- **Freshness is an age, not a colour**: "output 4s ago". Past 60 s it reads "no output for 2m", plainly, because kstrl does not act on silence.
- **Spend moves when a phase ends** (`component_usage` is emitted then), so a card mid-phase says what has not been counted yet rather than showing a stale total as current.
- **What you are reading does not change under you.** A notice offers the update ("try 3 finished: review failed · Show"); a growing log follows the end only while you are at the end.
- **A new ask arrives once**: it appears in Needs you with the same fade, and the tab title gains its count, "(2) kstrl".
- Show only what kstrl emits. The no-progress breaker's streak is not emitted until it trips, so no screen counts toward it.

## Notifications

Notifications connect every screen. Each one is a record kstrl filed (an inbox item or an event), delivered, and each has one place it lands you.

- **Asks interrupt, once.** An ask gets a notice card in the app, a desktop banner when the app is in the background, a count in the tab title ("(2) kstrl") and a place in Needs you. A repeat adds to its item and stays quiet.
- **Notices never interrupt.** Health and calibration wait in the inbox.
- **No approving from a notice.** A card offers Review and Later; approval happens where the evidence is.
- **A notice never takes focus.** It is announced; F6 moves focus to its Review, and esc there means Later. Its keys act only once focus is in it, because ↵ also zooms in on the map.
- **Closed means quiet.** With the app closed, only the hooks in `[notify]` and GitHub labels reach you. Say so where it matters, including what does not reach a hook.
- **Every notification lands somewhere specific**: an ask on its inbox item, a stopped part on its Part level, a question from the architect on the Spec text view.

## The command window

- `radius-lg`, `shadow-window`, three rows: the input (56px), the body, the action bar (44px). Opened with `⌘K` over any level, which dims behind it with `scrim`.
- A list on the left (sections in `label`: the answer, things to do about it, other questions) and the answer or the item's detail on the right, with its sources as `measure` chips.
- The action bar: the consequence or the standing condition on the left; the action ↵ runs, in `you` with its keycap, on the right. On a question, ↵ goes to where the answer is and never does anything consequential. An action that cannot run yet stays in the list, says when ("after this run") and is shown unavailable in the bar.
- The query keeps focus the whole time: arrows move the active row (`aria-activedescendant`), the pointer moves it too, Tab stays in the window, esc closes it and focus goes back to where you were. The Chrome and CommandWindow cards hold the full contract, and the interaction test drives it.
- Every consequential action carries its consequence in `text-2` before it is taken. Every shortcut is a keycap (`.k-key`, chords grouped in `.k-keys`).

## Content

- Lead with the verb: "Approve and merge", "Retry with a note", "Tell the engineers…", "Open the diff".
- Name the parts a choice affects. Not "dependents are skipped" but "http-server is skipped".
- Say what the factory does not do yet, at the point it matters: "The engineer is told a person asked for changes. Your own words are not passed on today." An answer the code only records says "recorded, not acted on".
- Numbers carry a denominator or unit (`try 3 of 4`, `1 of 4 slots`). A lower bound is written `≥$6.12`. A figure without a cap says "no budget is set". A count kstrl does not record says so rather than showing 0.
- Time is relative where you act (`12m`, `started 1m ago`) and absolute in records (`21:03`).
- A token in prose (a part's name, a flag) is set in `v-token`, so it never breaks at its hyphen: never "search-" at the end of one line and "index" at the start of the next.
- Section labels are sentence case: "Needs you", "Answer from the run records", "Other questions about search".
- No emoji, no exclamation marks, no em dashes.

## Marks

Eight drawn marks, one meaning each, as `.k-mk.<name>` in `bundle.css`, coloured by token: `you` (diamond, needs you), `work` (open arc, an agent is working), `wait` (dashed ring, waiting on a dependency), `pass` (check, a measurement agreed), `fail` (cross, disagreed or stopped), `landed` (filled check, merged), `skip` (slashed ring, a dependency failed), `absent` (dashed square, a sensor did not run). Use `.sm` (12px) inline in text. They replace the TUI's Unicode glyphs on these surfaces.

## Iconography and the name

- The kestrel mark (`assets/Logos`) is the only logo. It sits at 18px in `text` at the start of the header and of the command window's input.
- The name is written `kstrl`, lowercase. As a wordmark it is set in `human`; in running text it is plain.
- No icon library. Anything that is not one of the eight marks is a word.

## Surfaces

- **The map**: four zoom levels, the default view.
- **The command window**: actions and questions, over any level.
- The terminal UI keeps its own theme for now.

## Definition of done

A component or frame is done when a person has looked at it at 2x in both themes, every interaction has been tried by keyboard and pointer, and `audit.py` passes it in both themes. The audit is the mechanical half; each check below is proven by a planted failure and a clean control (`audit.py --selftest`), and the new ones were mutation-tested against the defect that prompted them.

| check | rule |
|---|---|
| contrast | every drawn text run, `aria-hidden` illustrations included and screen-reader-only text excluded, is 4.5:1 on its composited ground (3:1 at 24px, or 18.66px bold) |
| boundary | an input's edge (a field box, a toggle's off track) is 3:1 on its own fill and on the surface around it |
| focus | every control takes focus by script and then shows a 2px outline at 3:1 on its surface; on ink the ring is `focus-on-ink`. Controls under an open modal (`inert`) are checked where they are live |
| target | every control is at least 24 by 24 |
| disabled | nothing uses native `disabled`: an unavailable control keeps focus (`aria-disabled`, or `readonly` for a value) and says why |
| glyph | every character, in text, `::before`/`::after` content, values and placeholders, exists in the shipped font its element asks for first |
| overflow | nothing, element boxes or the text inside them, leaves a tile, a card or an anatomy area; and no text runs past the edge of the drawing it is set in, which an SVG clips (Health's limit labels lost 3px of "sigma" that way) |
| collision | no two runs of text overlap, unless an opaque surface stacked between them hides one (a panel over the map, a window over an inert page: the stacking is measured with `inert` lifted, because inert content cannot be hit-tested) |
| fit | a card's content has equal top and bottom margins, within 4px; at its declared size the card never scrolls inside its frame, in any state its controls reach; and tiles that share a row end within 40px of its foot under text or controls, 80px under an anatomy drawing. One pixel of overflow draws a scrollbar and narrows the whole layout by its width: Chrome and Stat did in version 36, inside the 4px |
| raw colour | no hex, rgb or hsl literal in the component layer or a card's own styles |
| leak | a page's own rule, or a style attribute in its source, sets only layout (margin, placement, position, size, overflow, gap) and custom properties on an element that carries a component class; a page restyling a component's part is how its copies drift |
| broken token | no line ends at a token's hyphen with the token going on below |
| painted | no box a page paints itself: a fill, image, shadow or edge that the page's own CSS adds, `::before` and `::after` included, found by turning the page's styles off and comparing. What paints is a component, the card's ground (the viewport's size, `canvas`, nothing else), the documentation base (`c-tile`, `c-ink`, `c-stage`), or a rule line (one side, 1px solid `line`). A drawing of something that is not kstrl (a phone's notification, a pull request) sits inside `data-illustration="what it draws"`, which the audit lists for a person to read. Eight pages drew a meter at four heights and three radii before this check |
| doc base | `c-tile`, `c-ink` and `c-stage` are defined exactly as the card scaffold writes them, so the painted check cannot be passed by renaming |
| baseline | two neighbouring runs of text in one flex or grid row, each at most two lines and the first thing in its cell, share a first baseline within 1px. A run inside a box, a cluster of controls, and a big number or title beside a caption (sizes more than 1.5 apart) are not compared |
| type | every text run is a step of the scale: its family and size are a step's, its weight one the step allows, its tracking the step's, and, where it sets its own lines, its line height the step's (or its size, for a control on one line); a `v-measure` is smaller than the words it is in. A drawing of another app and the wordmark (`data-wordmark`) are exempt, and nothing else |
| motion | every transition and every animation that moves is neutralised under reduced motion; an animation whose keyframes change only opacity or colour moves nothing and is kept |
| hygiene | no page stylesheet redefines a global bundle class |

The audit runs after the web fonts have loaded and after the card's own scripts, which also wait for the fonts: everything measured earlier (thumbs, bars, filmstrips, anatomy) came out at the fallback font's widths.

What it does not check, and a person must: that motion reads as intended at speed, that the words are true to kstrl (every claim in a card is checked against kstrl's code before it is written), and that a screen reads as designed rather than assembled (see Craft).
