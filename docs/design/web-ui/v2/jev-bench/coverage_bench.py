"""Can Jev tell when a view request asks for a form the catalogue does not have?

This is the judgment behind the "new component request" state: when the
operator asks for a visual the catalogue cannot express, the page drafts a
component request instead of rendering the nearest form. A Noul asks whether
the requested form is outside the catalogue; the labelled set has 10 requests
inside it and 10 outside. Bars, stated before running: at least 8 of 10
outside requests judged outside at the threshold that keeps every inside
request inside, and that threshold reported. Run:

    uv run --with typesafe-sdk python coverage_bench.py

Appends to coverage_raw.jsonl and prints the sweep.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from catalogue import ARGS

HERE = Path(__file__).resolve().parent
RAW = HERE / "coverage_raw.jsonl"
MODEL = "jev-1.13.0"

FORMS = {k: v for k, v in ARGS["view_form"]["criteria"].items() if k != "unstated"}

ITEMS = [
    ("list of failed runs", False),
    ("board of components by state", False),
    ("timeline of the run's phases", False),
    ("graph of components and dependencies", False),
    ("meter of spend against the cap", False),
    ("sparkline of cost per run", False),
    ("table of findings by severity", False),
    ("just the number of failed components", False),
    ("the diff for storage", False),
    ("bar chart of tokens per component", False),
    ("heatmap of cost by hour of day", True),
    ("sankey of tokens flowing from phase to phase", True),
    ("calendar of runs by day", True),
    ("treemap of cost by component", True),
    ("pie chart of spend per phase", True),
    ("a map of which files each component touched", True),
    ("word cloud of finding kinds", True),
    ("gauge cluster of every run's spend", True),
    ("scatter of cost against tokens per run", True),
    ("radar chart of the five integration criteria", True),
]

QUESTIONS = {
    "outside": {
        "type": "noul",
        "instructions": {
            "catalogue": FORMS,
            "question": "Does `command` ask for a visual form that none of the entries in `catalogue` can draw? Yes means a new kind of visual is needed; no means one of the catalogue forms is what was asked for or a close equivalent.",
        },
    },
    "form": {"type": "choice", **ARGS["view_form"]},
}


def main() -> None:
    from typesafe_sdk import TypeSafeClient

    recs = []
    with TypeSafeClient(model=MODEL) as client, RAW.open("a", encoding="utf-8") as out:
        for text, outside in ITEMS:
            t0 = time.perf_counter()
            resp = client.system_one(state={"command": text}, questions=QUESTIONS)
            dt = time.perf_counter() - t0
            raw = resp.model_dump()
            rec = {"text": text, "outside": outside, "answers": raw["answers"], "usage": raw["usage"], "latency_s": round(dt, 4)}
            out.write(json.dumps(rec) + "\n")
            recs.append(rec)
            print(f"{rec['answers']['outside']['noul']:.2f} {'OUT' if outside else 'in '} form={rec['answers']['form']['choice']:<10} {text!r}")
    ins = [r["answers"]["outside"]["noul"] for r in recs if not r["outside"]]
    outs = [r["answers"]["outside"]["noul"] for r in recs if r["outside"]]
    print(f"inside: max {max(ins):.2f}   outside: min {min(outs):.2f}")
    for t in [i / 20 for i in range(1, 20)]:
        print(f"t={t:.2f} inside judged inside {sum(x < t for x in ins)}/{len(ins)}  outside judged outside {sum(x >= t for x in outs)}/{len(outs)}")


if __name__ == "__main__":
    main()
