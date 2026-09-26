"""Measure Jev as the router behind kstrl's command bar.

Run from this directory, with TYPESAFE_API_KEY in the environment of this
process only:

    uv run --with typesafe-sdk python bench.py            # real calls
    uv run --with typesafe-sdk python bench.py --score    # re-score raw.jsonl

Every request and response is appended to raw.jsonl. The API key is never
written: the record holds the state, the questions, the answers, the usage
and the wall-clock latency. Scoring reads raw.jsonl back, so the numbers in
README.md can be recomputed without new calls.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from catalogue import ARGS, ROUTE_ARGS, ROUTE_QUESTION, build_state

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw.jsonl"
PHRASINGS = HERE / "phrasings.json"
RESULTS = HERE / "results.json"
MODEL = "jev-1.13.0"  # pinned: thresholds measured here belong to this version


def questions() -> dict[str, dict[str, Any]]:
    qs: dict[str, dict[str, Any]] = {
        "route": {"type": "choice", **ROUTE_QUESTION},
    }
    for name, spec in ARGS.items():
        qs[name] = {"type": "choice", **spec}
    return qs


def run_calls(repeat: int) -> None:
    from typesafe_sdk import TypeSafeClient  # imported here so --score needs no key

    items = json.loads(PHRASINGS.read_text(encoding="utf-8"))["items"]
    qs = questions()
    with TypeSafeClient(model=MODEL) as client, RAW.open("a", encoding="utf-8") as out:
        # one warm-up call so the first measured latency is not a cold connection
        client.system_one(state=build_state("warm up"), questions={"route": qs["route"]})
        for pass_no in range(repeat):
            for item in items:
                state = build_state(item["text"])
                t0 = time.perf_counter()
                resp = client.system_one(state=state, questions=qs)
                dt = time.perf_counter() - t0
                raw = (
                    resp.model_dump()
                    if hasattr(resp, "model_dump")
                    else json.loads(resp.model_dump_json())
                )
                rec = {
                    "id": item["id"],
                    "pass": pass_no,
                    "text": item["text"],
                    "state": state,
                    "model": raw.get("model"),
                    "answers": raw.get("answers"),
                    "usage": raw.get("usage"),
                    "latency_s": round(dt, 4),
                }
                out.write(json.dumps(rec) + "\n")
                out.flush()
                route = rec["answers"]["route"]
                print(
                    f"{item['id']:>3} {dt * 1000:6.0f}ms {rec['usage']['input_tokens']:>5}tok "
                    f"{route['choice']:<16} conf {route['confidence']:.2f}  {item['text']!r}",
                    file=sys.stderr,
                )


def percentile(xs: list[float], p: float) -> float:
    ys = sorted(xs)
    if not ys:
        return float("nan")
    k = (len(ys) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (k - lo)


def _score_item(it: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    answers = r["answers"]
    route = answers["route"]["choice"]
    accepted = [it["route"], *it.get("accept", [])]
    is_lenient = route in accepted
    arg_detail: dict[str, Any] = {}
    if is_lenient and route in ROUTE_ARGS:
        for arg, accepted_values in it.get("args", {}).items():
            if arg not in ROUTE_ARGS[route]:
                continue
            got = answers[arg]["choice"]
            arg_detail[arg] = {
                "got": got,
                "ok": got in accepted_values,
                "conf": round(answers[arg]["confidence"], 3),
            }
    return {
        "id": r["id"],
        "text": r["text"],
        "kind": it["kind"],
        "expected": it["route"],
        "accept": it.get("accept", []),
        "got": route,
        "p_expected": round(answers["route"]["probabilities"].get(it["route"], 0.0), 3),
        "conf": round(answers["route"]["confidence"], 3),
        "strict": route == it["route"],
        "lenient": is_lenient,
        "args": arg_detail,
        "latency_s": r["latency_s"],
        "input_tokens": r["usage"]["input_tokens"],
        "top3": sorted(answers["route"]["probabilities"].items(), key=lambda kv: -kv[1])[:3],
    }


def _sweep(
    right_conf: list[float], wrong_conf: list[float]
) -> tuple[list[dict[str, Any]], float | None]:
    sweep = []
    for t in [i / 20 for i in range(0, 21)]:
        sweep.append(
            {
                "t": t,
                "right_kept": sum(c >= t for c in right_conf),
                "wrong_kept": sum(c >= t for c in wrong_conf),
            }
        )
    clean = [s["t"] for s in sweep if s["wrong_kept"] == 0]
    return sweep, (min(clean) if clean else None)


def score() -> dict[str, Any]:
    items = {it["id"]: it for it in json.loads(PHRASINGS.read_text(encoding="utf-8"))["items"]}
    recs = [
        json.loads(line) for line in RAW.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    per_item = [_score_item(items[r["id"]], r) for r in recs if r["pass"] == 0]
    n = len(per_item)
    nomatch = [it for it in per_item if it["expected"] == "no_match"]
    others = [it for it in per_item if it["expected"] != "no_match"]
    args = [d for it in per_item for d in it["args"].values()]
    right_conf = [it["conf"] for it in per_item if it["lenient"]]
    wrong_conf = [it["conf"] for it in per_item if not it["lenient"]]
    by_kind: dict[str, list[int]] = {}
    for it in per_item:
        by_kind.setdefault(it["kind"], []).append(int(it["lenient"]))
    lat = [r["latency_s"] for r in recs]
    toks = [r["usage"]["input_tokens"] for r in recs]
    sweep, t_clean = _sweep(right_conf, wrong_conf)
    result = {
        "n_items": n,
        "n_calls": len(recs),
        "model": recs[0]["model"] if recs else None,
        "route_top1_strict": round(sum(it["strict"] for it in per_item) / n, 4),
        "route_top1_lenient": round(sum(it["lenient"] for it in per_item) / n, 4),
        "by_kind": {k: round(sum(v) / len(v), 3) for k, v in by_kind.items()},
        "arg_accuracy": round(sum(d["ok"] for d in args) / len(args), 4) if args else None,
        "args_scored": len(args),
        "nomatch_recall": round(sum(it["got"] == "no_match" for it in nomatch) / len(nomatch), 4)
        if nomatch
        else None,
        "false_nomatch_rate": round(sum(it["got"] == "no_match" for it in others) / len(others), 4)
        if others
        else None,
        "latency_p50_s": round(percentile(lat, 0.5), 3),
        "latency_p95_s": round(percentile(lat, 0.95), 3),
        "latency_max_s": round(max(lat), 3),
        "input_tokens_mean": round(statistics.mean(toks)),
        "input_tokens_max": max(toks),
        "right_conf_median": round(statistics.median(right_conf), 3) if right_conf else None,
        "wrong_conf_median": round(statistics.median(wrong_conf), 3) if wrong_conf else None,
        "threshold_sweep": sweep,
        "lowest_threshold_with_no_wrong_kept": t_clean,
        "items": per_item,
    }
    RESULTS.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true", help="score raw.jsonl without calling")
    ap.add_argument("--repeat", type=int, default=1, help="passes over the phrasings")
    a = ap.parse_args()
    if not a.score:
        run_calls(a.repeat)
    res = score()
    keys = [
        "n_items",
        "n_calls",
        "model",
        "route_top1_strict",
        "route_top1_lenient",
        "by_kind",
        "arg_accuracy",
        "args_scored",
        "nomatch_recall",
        "false_nomatch_rate",
        "latency_p50_s",
        "latency_p95_s",
        "latency_max_s",
        "input_tokens_mean",
        "input_tokens_max",
        "right_conf_median",
        "wrong_conf_median",
        "lowest_threshold_with_no_wrong_kept",
    ]
    print(json.dumps({k: res[k] for k in keys}, indent=1))
    print("\nmisses:")
    for it in res["items"]:
        if not it["lenient"]:
            print(
                f"  {it['id']:>3} {it['text']!r}: expected {it['expected']} got {it['got']} conf {it['conf']} top3 {it['top3']}"
            )
    print("\nwrong arguments:")
    for it in res["items"]:
        for arg, d in it["args"].items():
            if not d["ok"]:
                print(f"  {it['id']:>3} {it['text']!r}: {arg} got {d['got']!r} conf {d['conf']}")


if __name__ == "__main__":
    main()
