A field holds one value kstrl will use: a command, a number, a path. Values kstrl runs or counts are set in the measure voice (`k-field-measure`); words are set in the agent voice.

**Markup**

```html
<div class="k-field k-field-measure" data-invalid="true">
  <div class="k-field-box">
    <span class="k-field-affix">$</span>
    <input class="k-field-input" id="budget" value="-5" aria-label="Daily budget" aria-invalid="true" aria-describedby="budget-msg">
  </div>
  <span class="k-field-msg" id="budget-msg"><span class="k-mk sm fail"></span><span>Must be 0 or more. 0 means no budget.</span></span>
</div>
```

The field fills its container; the page sets the width. A key that focuses the field (/ for Settings' filter) is a keycap after the input, inside the box, and the input names it in `aria-keyshortcuts`. The message is always wrapped in its own span, so a wrapped line starts under the first word, not under the mark.

**Area** (`k-field-area`, a `textarea.k-field-input`): several lines of your words, a spec, an answer or a line of guidance, in the human voice at `intent` (19/26), because what you write is quoted back as yours. The box pads 8 by 10 and grows with what you write (`field-sizing: content`; the page's `rows` are the least it shows, and a `max-height` the page sets is the most, after which it scrolls inside itself); it has no drag grip. Its edge, hover and focus ring are the field's. A spec is written with its file name above it in a measure field (`specs/` as the affix), because `ks queue add` takes a file and copies it: editing the file afterwards changes nothing that runs.

**Anatomy**: box 32px, 10px inline padding, radius 6 (`radius-sm`), `raised` with a 1px `line-input` edge. Value 14px (`body`), or 13px `measure` in a measure field (`k-field-measure`); affix 13px `measure` in `text-3`. Message 12px/16px `text-2`, 6px under the box; its 12px mark is centred on the first line.

**States**
- Hover: the edge strengthens to `text-3`.
- Focus: a 2px `focus` ring on the box (`:focus-within`), 2px off, on any focus, pointer included, because typing follows.
- Invalid: the edge turns `fail` and the message says what kstrl accepts, with the fail mark. Wording follows kstrl's own check: `daily_budget_usd` must be 0 or more and 0 turns the budget off.
- Set elsewhere: `data-locked="true"` and a `readonly` input, never `disabled`, so the value can still be focused, selected and copied. Transparent with a `line` edge and a `text-2` value, and the message names where the value comes from ("Set by KSTRL_SERVE_DAILY_BUDGET_USD, which wins over kstrl.toml. Change it there."), which is how kstrl resolves settings: environment over kstrl.toml over defaults.

**Measured** (both themes): the edge, `line-input`, is 3.35:1 (day) and 3.43:1 (night) on its `raised` fill and more on `window`. At night `raised` is only 1.09:1 on `window`, so the edge is the only thing that shows where the field is; `line-strong` (2.00:1 at night) failed this, which is why `line-input` exists. The invalid edge is 5.13:1 and 6.06:1. Placeholder and affix `text-3` are 5.04:1 and 5.20:1 on `raised`.

**Changed from the frames**: the Queue's spec editor was drawn by the page: a 1.5px `you` edge where focus is the 2px `focus` ring (rufous means something waits for you, not where you are), a painted caret, and a 24px title and 16px text that are on no type token.

**Don't**: validate only on submit when the rule is known (say it as the value is typed); use a placeholder as the label; disable a field without saying why.
