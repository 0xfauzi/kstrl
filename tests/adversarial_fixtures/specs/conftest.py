"""Keep the fixture repositories out of kstrl's own collection.

``specs/*_repo/`` are miniature projects the architect calibration
fixtures hand to the architect to read. Their test modules belong to the
fixture, not to this suite: importing one needs its package on the path,
which it is not, so collecting it is an ImportError and takes the whole
run down (measured on a probe tree: "ModuleNotFoundError: No module named
'pulsekit'", 1 error, collection interrupted).

This glob is checked, from both sides and against the real collector, by
``tests/test_calibration_repo_fixture.py``.
"""

from __future__ import annotations

collect_ignore_glob = ["*_repo"]
