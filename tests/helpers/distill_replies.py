"""Synthetic distiller replies for #495.

The defect measured on #453 D13 was a reply whose first fact closed with
``"}},`` instead of ``"},``. ``BROKEN_REPLY`` has exactly that defect and
nothing else wrong with it: it is ``VALID_REPLY`` with one extra ``}``.
None of this text comes from the private repository the defect was
measured on.
"""

from __future__ import annotations

FACT_ONE = (
    '{"id":"fact-001","scope":"invariant","confidence":"asserted",'
    '"evidence":["src/store.py:10-20"],'
    '"claim":"Every write goes through save_record, which rejects a non-bool delete flag."}'
)
FACT_TWO = (
    '{"id":"fact-002","scope":"handler","confidence":"asserted",'
    '"evidence":["src/http.py:30-40"],'
    '"claim":"The request handler never sends a 501."}'
)
VALID_REPLY = '{"facts":[' + FACT_ONE + "," + FACT_TWO + "]}"
BROKEN_REPLY = '{"facts":[' + FACT_ONE + "}," + FACT_TWO + "]}"
EMPTY_REPLY = '{"facts": []}'
