# ledgerlite, slice 2: amount parsing

ledgerlite is a small in-memory ledger library. It uses the standard library
only and supports Python 3.11 or later. The package lives in `src/ledgerlite/`
and its tests in `tests/`.

## What to build

A module `src/ledgerlite/amounts.py` with one function:

- `parse_amount(text: str) -> int`: converts a decimal amount string into an
  integer number of cents. `"12"` is 1200, `"12.3"` is 1230, `"12.34"` is 1234
  and `"0.05"` is 5.

`parse_amount` raises `ValueError` when:

- `text` is empty;
- `text` contains any character other than the digits 0 to 9 and at most one `.`;
- `text` has more than two digits after the `.`.

Each error message says which of those rules the input broke. The module needs
nothing from `accounts.py`.

## Tests

pytest tests in `tests/test_amounts.py` covering every example and every rule
above.
