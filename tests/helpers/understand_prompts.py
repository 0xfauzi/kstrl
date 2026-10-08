"""Tables for the two understand instructions in ``kstrl/init_cmd.py``:
``DEFAULT_UNDERSTAND_PROMPT`` (``ks understand``) and
``DEFAULT_FEATURE_UNDERSTAND_PROMPT`` (``ks feature``), enrolled by #654
slice 8.

Until then ``tests/test_prompt_enrollment_walk.py`` exempted both as
documentation templates. Each is a full instruction body that ``run_loop``
sends to a model, so each now has a version and a snapshot. They live here
rather than in ``tests/test_prompt_versions.py`` for the reason
``tests/helpers/builder_prompts.py`` gives: that file is close to the repo's
800-line ratchet.

H2/H3 SCOPE. No role reads the content of the map these prompts produce, and
no calibration fixture scores one, so they carry the H3 obligation and no H2
obligation the suite can discharge (owner decision on #654, 2026-10-08).

RENDER-EXEMPT, for the reason ``DEFAULT_PROMPT`` is: ``ks init`` writes each
body to disk verbatim and ``run_loop`` reads the file back, so there is no
render step to orphan. ``SCAFFOLDED_TEMPLATES`` records every body each one
has shipped, and ``tests/test_prompt_staleness.py`` pins which constant
``run_init`` writes to which file.
"""

from __future__ import annotations

from kstrl import init_cmd

UNDERSTAND_PROMPTS: dict[str, str] = {
    "DEFAULT_UNDERSTAND_PROMPT": init_cmd.DEFAULT_UNDERSTAND_PROMPT,
    "DEFAULT_FEATURE_UNDERSTAND_PROMPT": init_cmd.DEFAULT_FEATURE_UNDERSTAND_PROMPT,
}

UNDERSTAND_VERSIONS: dict[str, str] = {
    "DEFAULT_UNDERSTAND_PROMPT": init_cmd.DEFAULT_UNDERSTAND_PROMPT_VERSION,
    "DEFAULT_FEATURE_UNDERSTAND_PROMPT": init_cmd.DEFAULT_FEATURE_UNDERSTAND_PROMPT_VERSION,
}

#: The pin. Nothing here computes a hash. 1.0.0 opens each series on the
#: body the scaffold ledger labels 2026-07-21, unchanged by #654 slice 8.
UNDERSTAND_SNAPSHOTS: dict[str, tuple[str, str]] = {
    "DEFAULT_UNDERSTAND_PROMPT": (
        "cfd43bfeb80eaaf559ccb32d993fc2c5b2471ff90c7816648743135c2aa29688",
        "1.0.0",
    ),
    "DEFAULT_FEATURE_UNDERSTAND_PROMPT": (
        "eb3637acf1918da23e27ad3f4d30bab32b1edd797b4bd1b5587b82b656affb09",
        "1.0.0",
    ),
}

UNDERSTAND_RENDER_EXEMPT = frozenset(UNDERSTAND_PROMPTS)
