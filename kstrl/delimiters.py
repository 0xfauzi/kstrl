"""Untrusted-data delimiter tokens for adversarial prompts.

A LEAF module: it imports nothing from ``kstrl``, which is the point.
The generator used to live in ``kstrl.decompose`` beside the first prompt
that needed it, and every later consumer imported it from there. That was
fine while the consumers were other prompt builders, and stopped being
fine when ``kstrl.git`` needed it too (#266: ``pasted_change_source``
frames the diff it pastes, so it has to own the token). ``decompose``
pulls in the manifest, the event bus, the guards and the Linear client;
routing ``git`` through all of that to reach twelve lines of ``secrets``
put ``kstrl.evolution`` inside ``kstrl.loop``'s import closure, which
``tests/test_state_dir_scope.py`` forbids for reasons of its own.

So the token generator lives at the bottom of the graph, where anything
may reach it and it may reach nothing.
"""

from __future__ import annotations

import secrets

# R5.3: every prompt that embeds untrusted data wraps it between
# delimiter lines carrying a per-build random token, so injected text
# inside the data cannot forge a section boundary or masquerade as
# harness instructions. Shared by review / security / knowledge /
# decompose and by the change-source builders in kstrl.git.
_DATA_DELIMITER_PREFIX = "KSTRL-DATA"


def generate_data_delimiter() -> str:
    """Return a fresh untrusted-data delimiter token for one prompt build.

    128 bits of randomness: an attacker who controls data INSIDE a
    section cannot guess the token, so they cannot authentically close
    the section or open a new one. Callers must generate a new token per
    prompt build, never reuse a constant.
    """
    return f"{_DATA_DELIMITER_PREFIX}-{secrets.token_hex(16)}"
