A stat is one measured number, large, with its unit or denominator small beside it and a label above.

**Markup**

```html
<div class="k-stat">
  <span class="k-label">Spend today</span>
  <span class="k-stat-value">≥$31.10<small>of $40.00</small></span>
</div>
```

**Sizes**: 30px/34px (`stat`: 600, measure voice, -0.03em) when the number has a tile of its own; `k-stat-sm` 22px/26px (`stat-sm`, -0.03em) when it sits among others. The unit is 13px (`measure`; 12px, `measure-inline`, in the small size) at 500 in `text-3`, untracked, 4px from the number. The label is `k-label`, 12px `text-3`, 6px above.

**The lower bound**: kstrl adds up what each call reported it cost. A call that reports nothing adds nothing and kstrl never estimates one (`kstrl/serve.py`, H4), so a spend total is the least that was spent, written with `≥`. It must be in the measure voice: Geist Mono draws `≥` and Instrument Sans does not.

**On ink**: the unit and label turn `window` at 72%.

**Do**: make every number add up with what the screen draws. **Don't**: set a claim (an agent's "all stories done") as a stat; round a measurement into something it did not measure.
