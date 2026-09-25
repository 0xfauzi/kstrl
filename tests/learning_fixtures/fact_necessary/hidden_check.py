"""The fact-necessary fixture's hidden check. Never copied into the target repository.

Run as ``python hidden_check.py <worktree>``. It imports slice 2's
``ledgerlite.amounts.parse_amount`` from ``<worktree>/src`` and calls it with
inputs slice 2's spec names as invalid. It passes only when every call raises
``ValueError`` and every message ends with the suffix slice 1's spec states
and slice 2's spec does not.

It prints one verdict line, ``HIDDEN-CHECK-VERDICT: pass <reason>`` or
``HIDDEN-CHECK-VERDICT: fail <reason>``, and exits 0 on pass and 1 on fail.
``kstrl.learning_fixture`` reads the last verdict line and refuses a run whose
verdict and exit code disagree, or that printed no verdict.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path

CONVENTION_SUFFIX = " [LL-417]"
VERDICT_PREFIX = "HIDDEN-CHECK-VERDICT:"

#: Each one breaks a rule slice 2's spec lists, so every correct
#: implementation raises ValueError for all three.
INVALID_INPUTS = ("", "12a", "1.234")


def _verdict(passed: bool, reason: str) -> int:
    print(f"{VERDICT_PREFIX} {'pass' if passed else 'fail'} {reason}", flush=True)
    return 0 if passed else 1


def _load_parse_amount(worktree: Path) -> Callable[[str], object]:
    sys.path.insert(0, str(worktree / "src"))
    module = importlib.import_module("ledgerlite.amounts")
    parse_amount: Callable[[str], object] = module.parse_amount
    return parse_amount


def main(argv: list[str]) -> int:
    worktree = Path(argv[1])
    try:
        parse_amount = _load_parse_amount(worktree)
    except Exception as exc:  # noqa: BLE001 - agent code: any import failure is slice 2's
        return _verdict(False, f"cannot import ledgerlite.amounts.parse_amount: {exc!r}")
    for text in INVALID_INPUTS:
        try:
            parse_amount(text)
        except ValueError as exc:
            if not str(exc).endswith(CONVENTION_SUFFIX):
                return _verdict(
                    False,
                    f"parse_amount({text!r}) message {str(exc)!r} does not end with "
                    f"{CONVENTION_SUFFIX!r}",
                )
        except Exception as exc:  # noqa: BLE001 - agent code: any other raise is a fail
            return _verdict(
                False, f"parse_amount({text!r}) raised {type(exc).__name__}, not ValueError"
            )
        else:
            return _verdict(False, f"parse_amount({text!r}) raised nothing")
    return _verdict(True, f"every error message ends with {CONVENTION_SUFFIX!r}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
