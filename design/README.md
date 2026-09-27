# design/ - the kstrl app design system

A design system for a graphical kstrl app: the operator's window onto factory runs (the map from Factory to Step,
the command window, approvals, the pages beside the map). No such app exists yet. kstrl's UIs today are the Rich
console output in `kstrl/ui` and the Textual dashboard in `kstrl/tui`, whose visual rules are in `/DESIGN.md` and
whose tokens are `kstrl/tui/theme.py`. Those rules are for a terminal and stay as they are; this directory is for
the graphical app and does not change them.

The system is published as a Design System artifact at https://claude.ai/artifact/5d4LkaTZBHTrxmyKMCCqNX
(private to its owner). `system/project/` is byte for byte what that artifact serves, version 43.

## What is here

| path | what |
|---|---|
| `system/project/README.md` | the brand book: principles, voices, colour, craft rules, the map and the pages beside it, which frame controls are wired, the command window, content rules, the definition of done and the audit's checks |
| `system/project/sections/*.md` | how kstrl works, as a designer needs it: the loops, attention, states, decisions, a run, several projects |
| `system/project/tokens.json` | colour for both themes (day, night), the type scale, spacing, radii, shadows |
| `system/project/components/bundle.css` | every component as `k-` classes, and the type scale as `--t-*` variables |
| `system/project/components/<Name>/README.md` | a component's markup, anatomy, states, keyboard, measured contrast, and what not to do |
| `system/project/components/<Name>/preview.html` | reference markup, and the reference behaviour script for the controls (segmented, tabs, toggle, band fitting, the command window) |
| `system/project/components/Map*`, `Notify*`, `Inbox` ... | the 25 frames (1280 by 800): each README says which kstrl record every value comes from, with file and line |
| `system/project/fonts/`, `system/LICENSES/` | Instrument Sans, Instrument Serif and Geist Mono, and their licences (SIL OFL 1.1) |
| `tools/` | what builds and checks the system (below) |
| `prototype/kstrl-prototype.html` | the app as one clickable page: every screen drawn at one moment (search being built, 21:40), joined and wired (below) |

The logos are uploads in the artifact, not files; the repository's copies are `/assets/logo/kstrl-mark.svg` and
`kstrl-mark-dark.svg` (358 bytes each, where the artifact's records say 381: not compared byte for byte).
The artifact's page appends a generated "Consuming" section and card index to the brand book; that tail is not here.

## For an agent building the app

Read in this order: `system/project/README.md`, then the README of every component you use, then the README of the
frame you are building. Then:

- Style only with the tokens and the `k-` classes. A page adds layout around components (margin, placement, width),
  never its own colours, sizes or states. The brand book's "Compose from the components" lists them.
- Every value on a screen comes from the kstrl record its frame's README names. Where a README says a value is not
  recorded, or a channel is record-only, the screen says so; it does not invent a source.
- Behaviour follows the script in the component's `preview.html`. The interaction contract is executable:
  `tools/interact.py`, `tools/interact_chrome.py` and `tools/interact_frames.py` assert it.
- `bundle.css` reads its colour and shadow variables from a stylesheet the artifact's page generates from
  `tokens.json`. An app generates the same variables; `tools/render.py` shows how (its `style` is a complete replica).
- Done means what the brand book's "Definition of done" says, and `tools/check_all.sh` passes.

The app itself is tracked in #610, which replaces #433 and #592. It is a local web page served by kstrl (`ks web`,
and `ks serve` while it runs), and a checkpoint is answered through a file-backed channel with today's park as the
fallback, as the owner decided on #592. Its framework is not chosen, and where this system's navigation departs from
that plan is listed in #610 for the owner. `Map6Approve`'s README describes today's in-process channel, including why
a detach while it is open matters (`pipeline.py:4960-4971`); it changes with the file-backed channel.

## Building and checking

Python 3.11 with `tools/requirements.txt` (Playwright and Pillow, outside kstrl's own dependencies), and Chromium.
Every command runs from `tools/`.

| command | what it does |
|---|---|
| `sh build_all.sh` | regenerates every card into `system/project`, wires the frames that share a moment (`wire_frames.py`), builds the prototype, and last applies the repository's whitespace hooks to what it wrote (`tidy.py`) |
| `python3 audit.py <Card>` | audits one card in both themes; `--selftest` proves every check against a planted failure and a clean control |
| `sh audit_all.sh` | rebuilds, then audits every card; detail in `audit_all.txt` |
| `sh render_all.sh <dir>` | renders every card, both themes, to PNGs (and the HTML the interaction tests drive, in `out/render`) |
| `python3 interact.py [night]` | drives the controls, the Chrome card, the command window and the wired frames by keyboard and pointer |
| `PYTHONPATH=. python3 probe/frame_pixels.py [night]` | every state a wired frame reaches in one move must be the static frame of that screen, pixel for pixel |
| `python3 probe/mutate_painted.py` | plants a hole in each audit rule; the self-test must go red for every one |
| `python3 build_prototype.py` | joins the screens drawn at 21:40 into `../prototype/kstrl-prototype.html` (`build_all.sh` runs it last) |
| `python3 interact_prototype.py [night]` | the prototype in the viewer's light or dark theme: every screen is its static frame pixel for pixel, every link lands where its table says, the keyboard, narrow widths |
| `sh check_all.sh` | all of the above, in order, stopping at the first failure |

The measurements are Chromium's: they were taken with Playwright 1.63 and its Chromium 1194. `browsers.py` finds a
Chromium under `PLAYWRIGHT_BROWSERS_PATH` or `~/.cache/ms-playwright`; set `KSTRL_DESIGN_CHROME` and
`KSTRL_DESIGN_HEADLESS` to use other binaries. Only that build, on Linux, has been run.

The generated files are committed, so a reader needs no build. A change is made in the generator, never in a
generated file: `build_all.sh` overwrites them. Commit the regenerated `system/project` with the change, and check
that `sh check_all.sh` passes first.

The repository's pre-commit hooks pass on every file here (`pre-commit run --files $(git ls-files design)`), with
one exception they do not see: `prototype/kstrl-prototype.html` is 563 KiB, over check-added-large-files' 500 KB, and
that hook looks only at files being added. `tools/ruff.toml` applies kstrl's lint rules to the tools without the
layout ones and keeps the formatter off them: their strings are their output, the reason `pyproject.toml` gives for
exempting the prompt bodies. CI runs nothing in this directory; `check_all.sh` is its gate.

## The prototype

`prototype/kstrl-prototype.html` is published at https://claude.ai/artifact/U4fcVPTsLoZFyb6xnUoeDG (private to its
owner). It is the page body the Artifact tool publishes as a plain page (the tool adds the
doctype and head). It holds the fonts, the tokens (day as the viewer's light theme, night as its dark), the bundle and
every screen drawn at 21:40, each scoped to itself, with one script that moves between them. `LINKS` in
`tools/build_prototype.py` is its whole navigation. A control whose screen is not drawn at 21:40 is left out of it
and does nothing; the page's own screen menu reaches every drawn screen, including those the app reaches through
something not drawn (⌘K's answers, a tile of a finished spec). Screens are added by drawing them as frames first,
then adding them to `SCREENS` and `LINKS`.

Opened straight from disk it would render in quirks mode, because it has no doctype. To open it locally, run
`python3 interact_prototype.py` (or `night`) in `tools/`: it first writes the page as the viewer serves it to
`tools/out/prototype/prototype-day.html` (`-night.html`), then tests it.

It has 23 screens. The published cards drawn at 21:40 (Factory, the Spec level as graph and text, search-query's
Part level and its try-2 review, Notifications, Inbox, Queue, Trust, Learning, Settings, and the approval and
question windows), and prototype frames drawn for it at the same moment: the Part level of the six other parts
(`gen_proto_parts.py`), the Step level as Stage and Grid and search-rank's engineer (`gen_proto_step.py`), and Spend
and Health (`gen_proto_pages.py`). Prototype frames are audited like cards (`audit.py Proto...`) but are not cards.

Not drawn at 21:40, so their controls do nothing: the views other than the first on Inbox, Learning, Trust,
Settings, Spend, Health and a step (Log, Notes, Prompt); the steps of search-schema, search-index and
search-highlight, and search-query's review on try 3; the command window's other answers; the receipts of the
specs built earlier this week.

## Publishing a change to the artifact

The artifact is a Design System type: its content is the files under `project/`. Publish the changed files with the
Artifact tool (`url` as above, `root` = `design/system`, `file_path` = one changed file's absolute path, `files` =
the others by their `project/...` paths), the index `project/design-system.json` last, with its `lastChange` updated.
Then read the changed files back and compare their sha256 with `system/project`.
