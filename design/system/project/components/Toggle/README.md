A toggle switches one setting on or off. The word beside the track always says which, so the state never rests on colour or position alone. On is ink, never rufous: turning a setting on is not an ask. For a choice among named values, use Segmented.

**Markup**

```html
<button class="k-toggle" role="switch" aria-checked="true" aria-label="Test adequacy">
  <span class="k-toggle-track"><span class="k-toggle-knob"></span></span>
  <span class="k-toggle-word"><span class="k-toggle-on">on</span><span class="k-toggle-off">off</span></span>
</button>
```

Both words are always in the markup and share one grid cell; CSS shows the one that matches `aria-checked`. The control is therefore as wide as "off" in either state (on is 16px, off 18px at 13px/500, measured), so in a right-aligned settings row the track never moves under the pointer when it flips.

**Anatomy**: track 32 by 18, radius 9, 3px inset; knob 12px, travels 14px. Hit area is the whole button, 24px tall. Word 13px/500 (`small`), 8px from the track.

**Colour**: off, the track is `selected` with a 1px `line-input` edge and a `text-3` knob; on, the track is `text` and the knob `window`. Word `text-2` off, `text` on. Hover strengthens the off edge to `text-3`.

**Unavailable**: `aria-disabled="true"` (never `disabled`), with `aria-describedby` pointing at the reason, as when an environment variable decides the value: "Set by KSTRL_SIGNALS_ENABLED, which wins over kstrl.toml." The track goes `raised` with a `line` edge, knob `line-strong`, word `text-3`, and clicks do nothing. It stays focusable so the reason can be reached.

**Keyboard**: native button. Tab reaches it; Space or Enter flips it.

**Motion**: the knob moves on `dur-base` (180ms); track and knob colours change on `dur-fast` (120ms); both `ease-standard`. The card's filmstrip is computed from those tokens: at 36ms the colours are well under way and the knob has barely moved. Under reduced motion both complete in 1ms.

**Measured** (both themes): knob on the off track 4.56:1 (day) and 4.58:1 (night); the off edge, `line-input`, 3.62:1 and 3.75:1 on `window`; the on track 18.26:1 and 15.99:1 on `window`. Words 7.84:1 and 8.20:1 off. The focus ring follows the whole control at radius 12.

**Don't**: use a toggle for something that runs now (that is a Button); use rufous for on; leave out the word.
