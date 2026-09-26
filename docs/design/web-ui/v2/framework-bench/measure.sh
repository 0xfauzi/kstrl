#!/bin/sh
# Measures the numbers FRAMEWORK.md cites. Run from anywhere:
#   sh docs/design/web-ui/v2/framework-bench/measure.sh > docs/design/web-ui/v2/framework-bench/out.txt
# Server side: install weight (site-packages bytes added on top of the
# harness) and install time for each candidate, then a hello-world SSE
# latency per candidate (sse_bench.py). Client side: the gzipped size of each
# candidate's runtime as served by a CDN. Nothing here touches the repo's
# own virtualenv: each candidate gets a throwaway venv under $TMPDIR.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PY=3.12

echo "== server install weight and time (uv, warm cache after the first line) =="
for spec in "stdlib:" "starlette+uvicorn:starlette uvicorn" "fastapi+uvicorn:fastapi uvicorn" "litestar+uvicorn:litestar uvicorn" "aiohttp:aiohttp" "flask:flask"; do
  name=${spec%%:*}; pkgs=${spec#*:}
  uv venv -q -p $PY "$WORK/$name" >/dev/null 2>&1
  start=$(python3 -c 'import time;print(time.time())')
  if [ -n "$pkgs" ]; then
    # shellcheck disable=SC2086  # $pkgs is a space-separated list on purpose
    VIRTUAL_ENV="$WORK/$name" uv pip install -q $pkgs >/dev/null 2>&1
  fi
  end=$(python3 -c 'import time;print(time.time())')
  bytes=$(du -sk "$WORK/$name/lib/python$PY/site-packages" | cut -f1)
  count=$(find "$WORK/$name/lib/python$PY/site-packages" -maxdepth 1 -name "*.dist-info" | wc -l | tr -d " ")
  printf '%-20s %6s KB in site-packages  %3s dists  install %.2fs\n' "$name" "$bytes" "$count" "$(echo "$end - $start" | bc)"
done

echo
echo "== hello-world SSE latency (time to first event, then per-event delay at 5 events/s, 50 events) =="
for name in stdlib starlette fastapi aiohttp; do
  case $name in
    stdlib) venv="$WORK/stdlib";;
    starlette) venv="$WORK/starlette+uvicorn";;
    fastapi) venv="$WORK/fastapi+uvicorn";;
    aiohttp) venv="$WORK/aiohttp";;
  esac
  VIRTUAL_ENV="$venv" uv pip install -q httpx >/dev/null 2>&1
  "$venv/bin/python" "$HERE/sse_bench.py" "$name" || echo "$name: failed"
done

echo
echo "== client runtime size =="
python3 "$HERE/client_size.py"
