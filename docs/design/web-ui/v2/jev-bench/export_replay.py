"""Write prototype/replay.js from the recorded Jev answers.

The prototype's command bar replays these answers: for a typed text that
matches a recorded phrasing it shows the recorded route, confidence, the
top alternatives and the arguments the route reads. Nothing in replay.js
is invented; run this after bench.py. Usage: python3 export_replay.py
"""

# ruff: noqa: E501
from __future__ import annotations

import json
from pathlib import Path

from catalogue import ROUTE_ARGS

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "prototype" / "replay.js"


def main() -> None:
    res = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
    raw = {}
    for line in (HERE / "raw.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["pass"] == 0:
            raw[r["id"]] = r
    entries = []
    for it in res["items"]:
        answers = raw[it["id"]]["answers"]
        route = answers["route"]
        args = {
            a: {"value": answers[a]["choice"], "conf": round(answers[a]["confidence"], 2)}
            for a in ROUTE_ARGS.get(route["choice"], [])
        }
        top = sorted(route["probabilities"].items(), key=lambda kv: -kv[1])[:4]
        entries.append(
            {
                "text": it["text"],
                "route": route["choice"],
                "conf": round(route["confidence"], 2),
                "top": [[k, round(v, 2)] for k, v in top],
                "args": args,
                "latency_ms": round(raw[it["id"]]["latency_s"] * 1000),
                "tokens": raw[it["id"]]["usage"]["input_tokens"],
                "expected": it["expected"],
                "correct": it["lenient"],
            }
        )
    cov = []
    for line in (HERE / "coverage_raw.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        cov.append(
            {
                "text": r["text"],
                "outside": round(r["answers"]["outside"]["noul"], 2),
                "form": r["answers"]["form"]["choice"],
                "labelled_outside": r["outside"],
            }
        )
    body = (
        "// Recorded Jev answers, exported by jev-bench/export_replay.py. Model jev-1.13.0, 2026-09-26.\n"
        "// Nothing here is invented: each entry is one real request and its answer.\n"
        f"window.JEV_REPLAY = {json.dumps(entries, indent=1)};\n"
        f"window.JEV_COVERAGE = {json.dumps(cov, indent=1)};\n"
        f"window.JEV_SUMMARY = {json.dumps({k: res[k] for k in ['model', 'n_items', 'route_top1_lenient', 'route_top1_strict', 'arg_accuracy', 'latency_p50_s', 'latency_p95_s', 'input_tokens_mean', 'lowest_threshold_with_no_wrong_kept']})};\n"
    )
    OUT.write_text(body, encoding="utf-8")
    print(f"wrote {OUT} with {len(entries)} recorded commands and {len(cov)} coverage judgments")


if __name__ == "__main__":
    main()
