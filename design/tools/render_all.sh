#!/bin/sh
# Render every card, both themes, into $1/<Card>.png and $1/<Card>-night.png
cd "$(dirname "$0")" || exit 1; out=$1; mkdir -p "$out"
for d in ../system/project/components/*/; do
  c=$(basename "$d"); [ -f "$d/preview.html" ] || continue
  python3 render.py "$c" >/dev/null && cp "out/render/$c.png" "$out/$c.png"
  THEME=night python3 render.py "$c" >/dev/null && cp "out/render/$c-night.png" "$out/$c-night.png"
done
echo rendered "$(find "$out" -maxdepth 1 -type f | wc -l)"
