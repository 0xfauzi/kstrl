"""Route three view requests that the coverage bench judged, so the prototype
can replay both judgments (route and catalogue coverage) for the same text.
Appends to phrasings.json (labelled here, before the calls) and raw.jsonl.
Run once: uv run --with typesafe-sdk python extra_routes.py
"""

# ruff: noqa: E501
from __future__ import annotations

import json
import time

from bench import MODEL, PHRASINGS, RAW, questions
from catalogue import build_state

EXTRA = [
    {
        "id": 84,
        "text": "heatmap of cost by hour of day",
        "route": "make_view",
        "args": {"view_source": ["costs"]},
        "kind": "view",
    },
    {
        "id": 85,
        "text": "word cloud of finding kinds",
        "route": "make_view",
        "args": {"view_source": ["findings"]},
        "kind": "view",
    },
    {
        "id": 86,
        "text": "treemap of cost by component",
        "route": "make_view",
        "args": {"view_source": ["costs", "components"], "view_group": ["component"]},
        "kind": "view",
    },
]


def main() -> None:
    from typesafe_sdk import TypeSafeClient

    doc = json.loads(PHRASINGS.read_text(encoding="utf-8"))
    have = {it["id"] for it in doc["items"]}
    new = [e for e in EXTRA if e["id"] not in have]
    if not new:
        print("already recorded")
        return
    doc["items"].extend(new)
    PHRASINGS.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    qs = questions()
    with TypeSafeClient(model=MODEL) as client, RAW.open("a", encoding="utf-8") as out:
        for item in new:
            state = build_state(item["text"])
            t0 = time.perf_counter()
            resp = client.system_one(state=state, questions=qs)
            dt = time.perf_counter() - t0
            raw = resp.model_dump()
            rec = {
                "id": item["id"],
                "pass": 0,
                "text": item["text"],
                "state": state,
                "model": raw["model"],
                "answers": raw["answers"],
                "usage": raw["usage"],
                "latency_s": round(dt, 4),
            }
            out.write(json.dumps(rec) + "\n")
            r = rec["answers"]["route"]
            print(
                f"{item['id']} {r['choice']} conf {r['confidence']:.2f} form={rec['answers']['view_form']['choice']} {item['text']!r}"
            )


if __name__ == "__main__":
    main()
