# DESIGN.md - Ralph dashboard visual system

Terminal UI (Textual). The single source of truth for tokens is
`kstrl/tui/theme.py` (`RALPH_THEME`); this file documents intent.

## Theme

Dark, warm near-black ramp. Scene: a developer's terminal, evening
ambient light, next to an editor - a control room, not a poster.

## Color

Composed in OKLCH, shipped as hex. Strategy: restrained with one
committed accent.

| Role | Hex | Use |
|---|---|---|
| background | `#161310` | screen |
| surface | `#1e1a15` | masthead, docked bars |
| panel | `#27221a` | chips, quiet buttons, separators |
| foreground | `#ece5d8` | body text (>=4.5:1 everywhere it appears) |
| muted | `#a2967f` | secondary text, placeholders |
| accent (amber) | `#e5a84f` | selection, focus, primary action, running state, brand |
| success | `#8fc470` | completed / passed only |
| error | `#e26d5a` | failed only |
| steel | `#82a7ba` | verifying (adversarial phases) only |
| violet | `#b48ec9` | merge parked only |
| warning | `#d9b036` | findings, caution copy only |

Rules: exactly one accent; state colors never do chrome duty; empty
data renders as a dim `·`, never `-`.

## Typography / glyphs

Monospace (the terminal's). Hierarchy via weight + the muted/foreground
split, not size. Status glyphs are unicode, defined once in
`theme.STATUS_GLYPHS`: ○ pending, ● running, ◐ verifying, ✓ completed,
⏸ merge parked, ✗ failed, ◌ skipped. ◆ marks an open checkpoint.

## Layout grammar

- 1-line masthead on `surface`: brand chip (amber reverse) > project >
  state chip > elapsed; meter right (`12.4k+ tok · $1.87+ · run id`).
- Panel titles: 1 line, muted bold lowercase, `border-bottom: solid
  panel` - the only separator vocabulary.
- Board: content-height table, numerics right-aligned, cursor =
  `accent 22%` row tint.
- The remaining height always holds live data (activity feed on the
  overview, transcript on the detail screen). Dead space is a defect.
- Modals: `round` border at 60% role color with a border title; body
  on `background` inside `surface` dialog; ONE accent-filled primary
  button, other actions quiet (panel bg, consequence-colored text).

## Motion

None decorative. The only animation is content arriving (feed lines,
transcript follow). Reduced-motion is the default state of the system.
