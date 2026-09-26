#!/bin/sh
# Render the round 2 prototype with headless Chrome: each state at 1440x900
# in both themes, and the phone states at 390x844 through an iframe wrapper
# (headless Chrome clamps its window to 500px wide, so a wrapper page holds
# the prototype in a 390x844 frame and the capture is cropped to the frame).
#   sh docs/design/web-ui/v2/render.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/shots"
PROTO="$HERE/prototype/index.html"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"
# One capture, bounded to 45 s: a hung Chrome is killed by its own pid and the
# capture is retried once, so a render never blocks the whole run.
shot() {
  for attempt in 1 2; do
    "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size="$3" \
      --virtual-time-budget=2500 --screenshot="$2" "$1" >/dev/null 2>&1 &
    pid=$!
    i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 90 ]; do sleep 0.5; i=$((i + 1)); done
    if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; echo "timeout on $2 (attempt $attempt)" >&2; continue; fi
    wait "$pid" 2>/dev/null
    return 0
  done
  return 1
}
# name|hash
STATES='
overview|s=overview
command-routed|s=command&q=why%20did%20client-commands%20fail
command-low|s=command&q=merge
command-nomatch|s=command&q=git%20push%20origin%20main
ask-cost|s=ask&q=cost%3F
ask-main|s=ask&q=is%20main%20green
run|s=run
checkpoint|s=checkpoint
confirm|s=confirm
failure|s=failure
delivery|s=delivery
views|s=views
newcomp|s=newcomp
nearest|s=nearest
'
for theme in dark light; do
  echo "$STATES" | while IFS='|' read -r name hash; do
    [ -n "$name" ] || continue
    shot "file://$PROTO#$hash&theme=$theme" "$OUT/$name-$theme.png" 1440,900
  done
done
phone() {
  printf '<!doctype html><html><body style="margin:0;background:#000"><iframe src="file://%s#%s" style="width:390px;height:844px;border:0;display:block"></iframe></body></html>' \
    "$PROTO" "$2" > "$TMP/$1.html"
  shot "file://$TMP/$1.html" "$TMP/$1.png" 500,904
  magick "$TMP/$1.png" -crop 390x844+0+0 +repage "$OUT/$1.png"
}
phone phone-overview-dark "s=overview&theme=dark"
phone phone-overview-light "s=overview&theme=light"
phone phone-checkpoint-dark "s=checkpoint&theme=dark"
phone phone-command-dark "s=command&q=why%20did%20client-commands%20fail&theme=dark"
phone phone-failure-light "s=failure&theme=light"
find "$OUT" -name '*.png' | wc -l
