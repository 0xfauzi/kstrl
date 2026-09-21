"""Keep the fixture repositories out of kstrl's own collection.

``specs/*_repo/`` are miniature projects the architect calibration
fixtures hand to the architect to read. Their test modules belong to the
fixture, not to this suite: importing one needs its package on the path,
which it is not, so collecting it is an ImportError and takes the whole
run down (measured on a probe tree: "ModuleNotFoundError: No module named
'pulsekit'", 1 error, collection interrupted).

The glob is checked from both sides by
``tests/test_calibration_repo_fixture.py``: every declared fixture
repository ends in ``_repo`` so this covers it, and every ``*_repo``
directory here is a declared fixture repository so this ignores nothing
else. A third test drives the real collector over this directory and
fails if anything is collected at all.
"""

from __future__ import annotations

collect_ignore_glob = ["*_repo"]
