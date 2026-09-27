"""The type scale: one source for the bundle's --t-* variables, tokens.json's type groups and the audit's type check.
Designed from a census of 3,226 rendered text runs (50 family-and-size pairs, 155 styles, 8.1% on a token): each step is a
role the pages already had, the steps are whole pixels, and a measurement sits one step below the text it is set in, because
Geist Mono reads larger than Instrument Sans at the same size."""
from __future__ import annotations

# (name, voice, size, line-height, weights allowed (first is the default), tracking, sample, usage)
SCALE = [
  ('intent-display', 'human', 44, 48, (400,), '-.01em', 'search', 'The spec a page is about, once: the Factory level’s building spec.'),
  ('intent-xl', 'human', 34, 40, (400,), '-.01em', 'People can find a snippet by the words in it.', 'The sentence or name at the top of a page: the title row in your words, the intent a receipt answers.'),
  ('intent', 'human', 20, 28, (400,), '0', 'Revoked links return 404, like links that never existed.', 'Your words as reading text: a spec document, your guidance as you write it, a spec named as a tile’s subject.'),
  ('intent-sm', 'human', 16, 22, (400,), '0', '“Snippets can carry tags.”', 'Your words in a row: a spec named in a list, a sentence quoted under a name.'),
  ('statement-xl', 'agent', 34, 40, (500,), '-.02em', 'Criterion 3 asks for 100 ms over 10,000 snippets.', 'The Stage’s speaker: what the agent in focus is saying, the one thing that frame is about.'),
  ('page-title', 'agent', 26, 32, (600,), '-.02em', 'search-query', 'The title row’s name for a part or a page, and the title of the item a detail pane is about.'),
  ('statement', 'agent', 22, 28, (500,), '-.015em', 'No test measures the 100 ms bound.', 'The one loud statement: the ink tile’s words, an agent’s last words.'),
  ('title', 'agent', 20, 26, (600,), '-.01em', 'Are tags shared by everyone?', 'A detail pane’s name, a question put to you, a status as a tile’s headline.'),
  ('input', 'agent', 17, 24, (400, 600), '0', 'Ask kstrl, or type a command', 'The command input, and a short heading at 600: a notice’s title, the command window’s answer, a claim.'),
  ('body', 'agent', 14, 20, (400, 600, 500), '0', 'Every gate passed on try 2 of 4.', 'Reading text, row titles and names at 600, what agents said, the consequences of actions.'),
  ('small', 'agent', 13, 18, (400, 500, 600), '0', 'Pushes the branch and merges its PR.', 'Secondary text: a consequence under a choice, a hint, a tab, meta beside a title.'),
  ('label', 'agent', 12, 16, (500, 400, 600), '0', 'Needs you', 'Section labels in text-3, captions, a sub-line under a title, a step’s label.'),
  ('micro', 'agent', 11, 16, (600, 400, 500), '0', 'reviewer', 'Chips and tags: a role, a kind, a count in a pill.'),
  ('stat-lg', 'measure', 44, 48, (600,), '-.03em', '≥$42.46', 'The one number a page is about (today’s spend).'),
  ('stat', 'measure', 30, 34, (600,), '-.03em', '≥$31.10', 'A tile’s number.'),
  ('stat-sm', 'measure', 22, 26, (600,), '-.03em', '8/10', 'A number in a small tile, or in a grid of tiles.'),
  ('measure-code', 'measure', 16, 24, (400,), '0', 'snip search tags:python', 'Code inside your words, where 13 would read small beside the serif.'),
  ('measure', 'measure', 13, 20, (400, 500, 600), '0', '48 passed · 0 failed · 41s', 'A value: test counts, durations, diff stats, spend, commit ids, paths, code in a well.'),
  ('measure-inline', 'measure', 12, 16, (400, 500, 600), '0', 'dur-fresh', 'A measurement inside small text: the header’s spend and liveness, a token named in a caption.'),
  ('measure-small', 'measure', 11, 16, (500, 400, 600), '0', '⌘ K', 'Keycaps, a row’s accessories (its age, its tries), anatomy notes, a chart’s labels.'),
]
FAMILY = {'human': 'Instrument Serif', 'agent': 'Instrument Sans', 'measure': 'Geist Mono'}

def css_vars() -> str:
    """:root variables: --t-<name> is the font shorthand at the default weight; --t-<name>-ls its tracking; -size and -lh its size and
    line height alone, for a size variant (a small button) that must keep its base rule's weight and line height."""
    out = []
    for n, v, s, lh, ws, ls, _, _ in SCALE:
        out.append(f'--t-{n}: {ws[0]} {s}px/{lh}px var(--font-{v}); --t-{n}-ls: {ls}; --t-{n}-size: {s}px; --t-{n}-lh: {lh}px;')
    return ':root { ' + ' '.join(out) + ' }'

def css_classes() -> str:
    """.ty-<name>: a step as a class, for a run no component covers (a consequence beside a button). Font and tracking together."""
    return '\n'.join(f'.ty-{n} {{ font:var(--t-{n}); letter-spacing:var(--t-{n}-ls); }}' for n, *_ in SCALE)

def js_table() -> str:
    """The scale for the audit's browser check: [family, size, line-height, weights, name, tracking in em]."""
    import json
    return json.dumps([[FAMILY[v], s, lh, list(ws), n, 0.0 if ls == '0' else float(ls[:-2])] for n, v, s, lh, ws, ls, _, _ in SCALE])
