"""Tables for the engineer-facing notice in ``kstrl/feedforward.py``:
``SECTION_FAILED_PROMPT``, enrolled by #428.

Everything ``build_codebase_scan_context`` returns is pasted into the engineer
prompt, so the notice is read by a model as part of its instructions. PR #417
removed "Raise codebase_scan.max_context_tokens to see it." from the
dependency graph's notice by hand and left no guard behind; enrolling the
bodies is what makes a reword move a hash and a version with it. #428 and
#626 enrolled seven notices; #696 slice 6 removed the six that reported on
the Python-only public interfaces and dependency graph, with those sections.

They live here rather than in ``tests/test_prompt_versions.py`` for the reason
``tests/helpers/builder_prompts.py`` gives: that file is close to the repo's
800-line ratchet.

ONE VERSION FOR THE MODULE, as the #303 builder fragments do. The unit is the
notice vocabulary one module delivers to one role, so a reword bumps
``CODEBASE_SCAN_NOTICE_PROMPT_VERSION``.

H2/H3 SCOPE. This is engineer-facing CONTEXT, like ``DECISIONS_CONTEXT_PROMPT``
and the #303 builder fragments: the calibration suite scores only the role
ids in ``kstrl.calibration.MIN_ROLE_DETECTION_RATE`` against planted-bug
fixtures, and has no fixture that scores a codebase scan notice. It carries
the H3 obligation (body, version and snapshot move together) and no H2
obligation the suite can discharge.

NOT RENDER-EXEMPT. Unlike the #303 fragments, the notice IS returned verbatim
by exactly one production path, so it has a real entry in ``_RENDERERS`` and
gets the exact-equality orphan guard rather than an exemption with a reason
that would not be true.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from kstrl import feedforward

#: One row, read the way ``builder_prompts._BUILDERS`` is read: the module,
#: the name of its shared version constant, and the names it covers.
_NOTICES: tuple[tuple[ModuleType, str, tuple[str, ...]], ...] = (
    (feedforward, "CODEBASE_SCAN_NOTICE_PROMPT_VERSION", ("SECTION_FAILED_PROMPT",)),
)

NOTICE_PROMPTS: dict[str, str] = {
    name: getattr(module, name) for module, _version, names in _NOTICES for name in names
}

NOTICE_VERSIONS: dict[str, str] = {
    name: getattr(module, version) for module, version, names in _NOTICES for name in names
}


# --- production renderer ---------------------------------------------------
#
# Returns the notice through the real function that emits it, so
# ``test_renderer_renders_the_enrolled_body`` can patch the constant to a
# fieldless marker and demand the production path return THAT and nothing
# else. ``str.format`` ignores keyword arguments a template does not use.


def _section_failed(tmp_path: Path) -> str:
    """A root that is a file: walking it raises, and the failure is the section."""
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("", encoding="utf-8")
    return feedforward._module_map_section(not_a_directory)


NOTICE_RENDERERS: dict[str, tuple[ModuleType, Callable[[Path], str]]] = {
    "SECTION_FAILED_PROMPT": (feedforward, _section_failed),
}

#: The pin. Not derived from ``_NOTICES``, and nothing here computes a hash.
NOTICE_SNAPSHOTS: dict[str, tuple[str, str]] = {
    "SECTION_FAILED_PROMPT": (
        "dcfe42ac8897607f28e9296af3f08b55953f35357c168ad11b7987d7ec4ea2ee",
        "2.0.0",
    ),
}

assert set(NOTICE_PROMPTS) == set(NOTICE_SNAPSHOTS) == set(NOTICE_RENDERERS), (
    "the three tables name different notices: "
    f"in _NOTICES only: {sorted(set(NOTICE_PROMPTS) - set(NOTICE_SNAPSHOTS))}; "
    f"in NOTICE_SNAPSHOTS only: {sorted(set(NOTICE_SNAPSHOTS) - set(NOTICE_PROMPTS))}; "
    f"missing a renderer: {sorted(set(NOTICE_PROMPTS) - set(NOTICE_RENDERERS))}. "
    "A new notice needs a _NOTICES entry, a snapshot row and a renderer."
)
