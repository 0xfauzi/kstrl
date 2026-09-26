#!/bin/sh
# Render the direction prototypes with headless Chrome: every screen at
# 1440x900, and direction A's Home at phone width (390x844) through the
# iframe wrapper (headless Chrome clamps its window to 500px wide).
#   sh docs/design/web-ui/styles/render.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
shot() {
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size="$3" \
    --screenshot="$2" "$1" >/dev/null 2>&1
}
for f in "$HERE"/*/*.html; do
  shot "file://$f" "${f%.html}.png" 1440,900
done
printf '<!doctype html><html><body style="margin:0;background:#000"><iframe src="file://%s/a-list-dark/home.html" style="width:390px;height:844px;border:0;display:block"></iframe></body></html>' \
  "$HERE" > "$TMP/phone.html"
shot "file://$TMP/phone.html" "$TMP/phone.png" 500,904
magick "$TMP/phone.png" -crop 390x844+0+0 +repage "$HERE/a-list-dark/home-phone.png"
find "$HERE" -name '*.png' | wc -l
