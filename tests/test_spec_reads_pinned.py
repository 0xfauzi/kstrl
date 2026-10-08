"""#639 owner decision 11: every read of a planned spec goes through the pin.

A plan pins the sha256 of the spec text the architect read
(``kstrl.decompose.load_spec_input``). After decompose, the only reader of
that spec is ``kstrl.plan_gate.pinned_spec``: it reads the spec again and
returns the text only when its digest is the pinned one, so no role can be
handed a spec that changed after the plan was made. Before this guard,
``acceptance_design.design_plan`` called ``load_spec_input`` itself and
gave the verification designer whatever the file held at that moment.

The guard is a census, closed by construction: every node in ``kstrl/``
that spells ``load_spec_input`` or ``spec_location`` (a def, an import, a
name, an attribute, a string), keyed by module and scope. The first reads
a spec, the second resolves where a planned spec lives, so a reader that
opens the file itself names it too. A new reader, an alias import or a
``getattr`` by name moves a row, and the message says where reads go.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.astwalk import assert_census, label, package_sources, parsed, spells
from tests.helpers.astwalk.scope import scope_of

#: Derived by running: write a dict empty, run the test, and read the
#: ``Found:`` dict out of the failure.
EXPECTED_SPEC_READS: dict[str, dict[str, int]] = {
    "load_spec_input": {
        # The definition, and the read whose text the architect is given.
        "decompose.py::<module>": 1,
        "decompose.py::_decompose_spec_impl": 1,
        # The import, and the one re-read, which compares the digest.
        "plan_gate.py::<module>": 1,
        "plan_gate.py::pinned_spec": 1,
    },
    "spec_location": {
        # The definition, and the one re-read that resolves the path.
        "plan_gate.py::<module>": 1,
        "plan_gate.py::pinned_spec": 1,
    },
}


def _module_and_scope(source_file: Path, node: object) -> str:
    owner = scope_of(parsed(source_file))
    return f"{label(source_file)}::{owner.get(id(node), '<module>')}"


@pytest.mark.parametrize("reader", sorted(EXPECTED_SPEC_READS))
def test_every_spec_read_after_decompose_goes_through_the_pin(reader: str) -> None:
    assert_census(
        sources=package_sources(),
        sees=spells(reader),
        key=_module_and_scope,
        expected=EXPECTED_SPEC_READS[reader],
        control=(
            f"from kstrl.decompose import {reader} as read\n",
            f"text = decompose.{reader}(path)\n",
            f"found = getattr(decompose, '{reader}')\n",
        ),
        message=(
            f"The places that name {reader} changed. After decompose, a "
            "spec is read only through kstrl.plan_gate.pinned_spec, which "
            "returns the text only when its digest is the one the plan pins "
            "(#639 decision 11). Call pinned_spec instead of reading the spec."
        ),
    )
