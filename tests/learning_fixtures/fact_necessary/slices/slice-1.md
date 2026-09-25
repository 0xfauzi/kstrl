# ledgerlite, slice 1: accounts

ledgerlite is a small in-memory ledger library. It uses the standard library
only and supports Python 3.11 or later. The package lives in `src/ledgerlite/`
and its tests in `tests/`.

## What to build

A module `src/ledgerlite/accounts.py` with:

- `class LedgerError(ValueError)`: the error ledgerlite raises for invalid input.
- `@dataclass class Account` with `name: str` and `balance_cents: int`.
- `open_account(name: str) -> Account`: a new account with a balance of 0.
  Raises `LedgerError` when `name` is empty or longer than 40 characters.
- `deposit(account: Account, amount_cents: int) -> None`: adds to the balance.
  Raises `LedgerError` when `amount_cents` is not a positive integer.
- `withdraw(account: Account, amount_cents: int) -> None`: subtracts from the
  balance. Raises `LedgerError` when `amount_cents` is not a positive integer,
  or when the balance is smaller than `amount_cents`.

Each error message says which rule the input broke.

## Project-wide error convention

Every error message that any ledgerlite module raises ends with the error code
suffix ` [LL-417]`: a space, then `[LL-417]`. The operations team's alert
router matches on that exact suffix and drops any message without it. The rule
applies to every module in this repository, including modules that later specs
add, and those specs will not repeat it.

## Tests

pytest tests in `tests/test_accounts.py` covering every rule above, including
the suffix on every error message.
