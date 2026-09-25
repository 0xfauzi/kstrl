"""Keep the integration fixture repositories out of kstrl's own collection.

``integration/*_repo/`` are the miniature projects the integration review
calibration fixtures (#482) materialise and hand to the reviewer. Their test
modules import ``pastebin``, which is not on this suite's path, so collecting
them is an ImportError that takes the whole run down, exactly as for
``specs/*_repo/`` (see ``specs/conftest.py``).

Checked from both sides by ``tests/test_calibration_integration_fixture.py``,
and against the real collector by
``tests/test_calibration_repo_fixture.py::test_the_fixture_repositories_are_not_collected_by_our_own_suite``.
"""

from __future__ import annotations

collect_ignore_glob = ["*_repo"]
