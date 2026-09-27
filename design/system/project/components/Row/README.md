A row is one item in a list: the inbox, the queue, the command window.

**Markup**: `<div class="k-row" role="option" aria-selected="false" tabindex="-1">` holding its mark, a span with `k-row-title` and `k-row-sub`, and `k-row-meta`. In a list you move through (the inbox), roving tabindex: the selected row is the one tab stop. In the command window focus stays in the input and the current row is marked with `aria-activedescendant`, so rows there are not focusable.

**Anatomy**: at least 44px tall, 20px mark column, 10px gaps, padding 7 by 10, radius 6 (`radius-sm`). Title 14px/600 (`body`), one line; the line under it 12px (`label` at 400) `text-2`; the measured fact at the end 11px `measure` `text-3`. The title, the fact at the end and a `?` mark share the title's baseline; a status mark and keycaps are boxes and centre on the row (centred, the fact sat 7px off the title it belongs to). A notice, which asks nothing of you (a health check, a calibration), takes the ring dot (`k-dot k-dot-ring`, it waits quietly) in the mark's column, centred in it.

**States**: hover `raised`; selected `selected`; a selected ask (`k-row-ask`) `you-tint` with a 1px `you` edge; focus the 2px `focus` ring, which needs the list's 4px gutter. Unavailable (`aria-disabled="true"`): the title steps down to `text-2` at 500 and the meta says when it will be available; it stays selectable, so what sits beside it can say why. In the command window the pointer moves the selection, so a row there has no hover of its own.

**Keyboard**: Up and Down move and select, Home and End jump, ↵ opens.

**This replaces the command window's old row**, which the bundle defined separately with its own `sel` and `tt` classes.
