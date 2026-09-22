"""What R8.8 slice 1 does NOT build, checked structurally (#155).

Absence is checkable; a default is not. Both tests fail on collection
before ``kstrl.signals`` exists, which counts as red.
"""

from __future__ import annotations

import dataclasses

from kstrl.signals import SignalsConfig


def test_signals_config_has_no_enqueue_field() -> None:
    names = {f.name for f in dataclasses.fields(SignalsConfig)}

    assert "enqueue" not in names, (
        "SignalsConfig grew an enqueue field. Slice 1 of R8.8 observes and does "
        "not spend: absence is checkable, a default is not, and a later PR could "
        "flip a default 'now that it has been running a while'. The code path "
        "must not exist."
    )


def test_signals_config_has_no_token_field() -> None:
    names = {f.name for f in dataclasses.fields(SignalsConfig)}

    assert "token" not in names, (
        "SignalsConfig grew a token field. kstrl.toml is a tracked file "
        "gitleaks scans; the token is read from the env var named by "
        "token_env, never stored."
    )
    assert "token_env" in names
