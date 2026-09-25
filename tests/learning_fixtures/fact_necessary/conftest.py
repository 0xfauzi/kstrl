"""Keep the fixture's seed repository out of kstrl's own collection.

``repo/`` is the miniature project a factory run of this fixture starts from.
Its tests import ``ledgerlite``, which is not on this suite's path, so
collecting them is an ImportError that takes the whole run down, as for
``tests/adversarial_fixtures/specs/*_repo/``.

Checked against the real collector by
``tests/test_learning_fixture.py::test_the_seed_repository_is_not_collected_by_our_own_suite``.
"""

from __future__ import annotations

collect_ignore = ["repo"]
