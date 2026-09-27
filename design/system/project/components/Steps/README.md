Steps are a line of stations or checks, one segment each: a part's line (Build, Check, Your approval, Merge) or a try's checks (verify, review, security, distill). They also draw a count toward a threshold, one step per unit, `done` for each one counted (9 of 15 clean merges toward L3), and a sequence of runs (6 of 7 CI runs passed, the seventh `now`); unlabelled, always beside the words that say the numbers.

**States** (`data-step`): `done` `text-3`; `now` `text`, when kstrl itself is running the step (verify runs the tests); `work`, when an agent is (review, security, the distiller), with a 45° stripe; `you` when it waits for you; `fail`; and not started, `selected`.

**Colour never works alone.** By day `text-3`, `work`, `you` and `fail` are within 1.12:1 of each other in lightness (measured), so a colour-blind reader could not tell a finished step from a failed one. So: the working step is striped; a labelled step in `work`, `you` or `fail` carries its mark; every labelled state is spoken ("review: failed"); and an unlabelled line is hidden from assistive technology and must sit beside a mark and a word that say the state, as the part card's first line does. `done` against not started (4.56:1) and `now` against `done` separate by lightness.

**Unlabelled**: `<div class="k-steps">` of `k-step` with a `k-step-bar`: 5px bars, radius 2.5, 3px apart.

**Labelled**: an ordered list, 6px bars, 6px apart, labels 12px; the step that matters is 600 in `text`, finished steps `text-2`. Four labelled steps need 296px: the widest label, "security" at 600 with its mark, is 70.5px (measured). The line is a size container: below 300px the labels leave the drawing (screen readers still read them) and one status line names the step that matters now, with its mark and its state ("security an agent is working", "verify is next", "every check passed"). The frames give the check sequence 254px to 307px, so both layouts occur.

**Don't**: show `now` for an agent's check (that is `work`); draw an unlabelled line without its mark and word.
