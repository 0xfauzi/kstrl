A passage of your words that something points at, and the tag that points at it: a decision the architect closed itself, a question waiting for you, the finding the ink tile states.

**Markup**

```html
<p>A tag is <mark class="k-hl">a short word</mark><span class="k-ref">2</span> such as python or sql.</p>
...
<div><span class="k-ref">2</span> <b>assumed</b> Lowercase, 1 to 32 characters.</div>
```

**The passage** (`k-hl`, on a `mark`): `selected` with a 2px `line-strong` underline, radius 4. It is cloned across lines (`box-decoration-break: clone`), so every line of a wrapped passage is padded and rounded and reads as marked. `k-hl-ask` is the passage a question is about: `you-tint` with a `you` underline.

**The tag** (`k-ref`): a 16px pill in `measure`, 11px at 600, padding 5, radius 8, at least as wide as it is tall. A tag and what it points at are matched by the tag's id (2, ?, IF-1) and nothing else, never by position. In running text the page sets its margin and lifts it to sit on the line (`vertical-align:3px` beside 19 to 21px `human` text).
- Default: `raised` with a 1px `line-strong` edge, `text-2`: a decision, an assumption, a record.
- `k-ref-ask`: `you`, `you-ink` type: a question waiting for you.
- `k-ref-ink`: `text`, `window` type: it points at the ink tile's statement, the one loud tile, so it wears the ink.

**Changed from the frames**: Escalation, the Text view and Receipt drew these themselves: pins as 11px pills, the Receipt's finding tag as a 10.5px rectangle with radius 4, and a passage that wrapped lost its padding and rounding on every line but the last.

**Don't**: pair a tag with its target by order on the screen; use a tag without the thing it points at on the same screen; highlight a passage nothing points at.
