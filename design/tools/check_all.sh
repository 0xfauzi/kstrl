#!/bin/sh
# Every check the design system has, in order. Stops at the first failure. It rebuilds the system first (audit_all.sh
# runs build_all.sh), so a pass is a pass of what is on disk now. Takes several minutes; the mutation run is the longest.
# Each step's full output is kept in out/logs/<step>.txt; a step's exit status is its own, never a pipe's.
set -e
cd "$(dirname "$0")"
mkdir -p out/logs
step() { name=$1; shift; echo "== $name"; if "$@" > "out/logs/$name.txt" 2>&1; then tail -1 "out/logs/$name.txt"; else tail -20 "out/logs/$name.txt"; echo "FAILED: $name (out/logs/$name.txt)"; exit 1; fi; }
step selftest python3 audit.py --selftest
step audit sh audit_all.sh
if grep -q '^FAIL' audit_all.txt; then echo "FAILED: audit (audit_all.txt)"; exit 1; fi
step render sh render_all.sh out/shots
step interact-day python3 interact.py
step interact-night python3 interact.py night
step pixels-day env PYTHONPATH=. python3 probe/frame_pixels.py
step pixels-night env PYTHONPATH=. python3 probe/frame_pixels.py night
step prototype-day python3 interact_prototype.py
step prototype-night python3 interact_prototype.py night
step mutations python3 probe/mutate_painted.py
echo "all checks passed"
