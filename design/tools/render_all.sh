#!/bin/sh
# Render every card, both themes, into $1/<Card>.png and $1/<Card>-night.png
cd "$(dirname "$0")"; out=$1; mkdir -p $out
for c in $(ls ../system/project/components | grep -v '\.css$'); do
  [ -f ../system/project/components/$c/preview.html ] || continue
  python3 render.py $c >/dev/null && cp out/render/$c.png $out/$c.png
  THEME=night python3 render.py $c >/dev/null && cp out/render/$c-night.png $out/$c-night.png
done
echo rendered $(ls $out | wc -l)
