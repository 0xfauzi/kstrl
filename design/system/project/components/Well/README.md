A record set into a tile: evidence (what something that did not write the work measured), a log excerpt with where it came from, code or configuration, a command to copy. The tile is the surface; the well is the record on it.

**Markup**

```html
<div class="k-well">
  <p>Overlapping matches ("sea" inside "search") still render as two spans.</p>
  <span class="v-measure">engineer.log · iteration 6</span>
</div>
```

**Anatomy**: `raised`, `radius-md` (10px; radius follows size), padding 10 by 12. One padding: a page never sets a well's padding, fill or radius, and lays out what is inside it with its own inner element (a grid of measurements, a row with a Copy button), never on the well itself.

**Kinds**
- Default: any record.
- `k-well-alert`: a record that failed its check (a story the reviewer found not met). The tile's alert edge, 1.5px `fail`, inset, always with the `fail` mark and a word.
- `k-well-code`: code, a command or a configuration, in `measure` (13/20), one `k-well-line` per line. A line too long for the well wraps at a space and hangs 2 characters in, so a continuation never reads as a new line. Nothing is cut and nothing scrolls out of sight. In a command, each word is a `v-token` and an option travels with its value (`--actor <you>`), so the line breaks only between them, never inside `--to`.

**Where it sits**: in a tile. On the canvas it would be a tile, and a well in a well is one level too many.

**Changed from the frames**: seven pages drew this block themselves, with five paddings (12 by 16, 10 by 12, 10 by 14 by 8, 9 by 12, 8 by 10) and two radii. The hook configuration was cut at its right edge by `overflow:hidden`; Trust's command broke inside its flags (`promote -`, `-to`); and a failed story was drawn white with a 3px bar where every other alert in the system is an edge. The command itself was wrong: `ks autonomy promote` has no `--to` (it raises one level) and `--ack` takes your reason, so it now reads as kstrl prints it, `ks autonomy promote --actor <you> --ack <why>`.

**Don't**: put a well on the canvas or inside another well; set a well's padding or type on the well itself; let a code line clip or scroll where the reader cannot see it.
