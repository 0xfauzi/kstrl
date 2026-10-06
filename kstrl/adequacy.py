"""What #696 slice 8 left of the R8.5 adequacy module: a test-path predicate.

Layer 0 (test-diff discipline and oracle linting), Layer 1 (patch
coverage) and Layer 2 (diff mutation) are gone (#696 decisions 7 and 8):
each read one language's test files or ran one language's tools. The
code reviewer's test-weakening criterion (``review.REVIEWER_PROMPT``)
replaces Layer 0, and Phase 1 records ``test_adequacy`` as not measured
where Layer 0 ran (``verify.LAYER0_NOT_MEASURED``).

The two patterns below have one reader, ``integration_fix.is_test_path``,
which scopes an integration fix to the test entries of the components
that own the cited files. #696 decision 6 removes that reader too (an
integration fix then hands every finding off), in the slice that rewrites
the integration fix scope; this module goes with it.
"""

from __future__ import annotations

import re

#: Test-file path fragments by Python's convention.
TEST_PATH_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")

#: Test paths by the conventions of languages other than Python (#627):
#: Jest and Vitest ``__tests__/`` and a ``.test.`` or ``.spec.`` infix
#: (``bulk.test.ts``, ``a.spec.tsx``); Go ``a_test.go`` and any other
#: ``_test`` stem (``a_test.cc``); RSpec ``spec/`` and ``a_spec.rb``; a
#: ``test_`` stem as a file or a directory (``test_io.c``, ``test_data/``);
#: and a bare ``test`` or ``tests`` entry. It is matched against
#: allowedPaths entries, which are directory prefixes (ending ``/``) as
#: well as files.
NON_PYTHON_TEST_PATH_RE = re.compile(
    r"(^|/)(__tests__|spec)/"
    r"|(^|/)tests?/*$"
    r"|(^|/)test_[^/]*/*$"
    r"|\.(test|spec)\.[^/]+$"
    r"|_test\.[A-Za-z0-9]+$"
    r"|_spec\.rb$"
)


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))
