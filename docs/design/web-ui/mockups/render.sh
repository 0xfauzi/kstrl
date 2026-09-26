#!/bin/sh
# Render every mock-up with headless Chrome: desktop at 1440x900, and the
# three screens an operator opens on a phone at 390x844 as well.
#   sh docs/design/web-ui/mockups/render.sh
#
# Headless Chrome clamps its window to 500px wide (measured: --window-size
# 390 lays the page out at innerWidth 500 and crops the capture), so the
# phone renders go through a wrapper page that holds the mock-up in a
# 390x844 iframe, and the capture is cropped to the frame.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
shot() {
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size="$3" \
    --screenshot="$2" "$1" >/dev/null 2>&1
}
for f in "$HERE"/*.html; do
  name=$(basename "$f" .html)
  shot "file://$HERE/$name.html" "$HERE/$name.png" 1440,900
done
phone() {
  printf '<!doctype html><html><body style="margin:0;background:#000"><iframe src="file://%s/%s.html" style="width:390px;height:%spx;border:0;display:block"></iframe></body></html>' \
    "$HERE" "$1" "$3" > "$TMP/$1.html"
  shot "file://$TMP/$1.html" "$TMP/$1.png" "500,$(( $3 + 60 ))"
  magick "$TMP/$1.png" -crop "390x$3+0+0" +repage "$HERE/$2.png"
}
for name in home inbox checkpoint run-board; do
  phone "$name" "$name-phone" 844
done
# the whole checkpoint page at phone width, so the choices are shown too
phone checkpoint checkpoint-phone-full 2300
# the whole integration review at desktop width, so every finding and its disposition is shown
shot "file://$HERE/integration.html" "$HERE/integration-full.png" 1440,1500
find "$HERE" -name '*.png' | wc -l
