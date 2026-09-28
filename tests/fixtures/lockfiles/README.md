# Captured lockfiles (#630)

Real lockfiles written by each toolchain, read by `tests/test_policy_lockfiles_e2e.py`.
Each directory holds one lockfile per state: `base` (the main branch), `add` (the branch
adds one package or more) and `bump` (the branch changes the version of a package the
base already has). Do not edit them by hand: a hand edit tests the reader against a
format no tool writes.

| Directory | Tool | base | add | bump |
|---|---|---|---|---|
| `cargo` | cargo 1.94.0 | itoa 1.0.10 | adds ryu 1.0.17 | itoa 1.0.11 |
| `npm` | npm 11.11.0, lockfileVersion 3 | is-number 6.0.0 | adds left-pad 1.3.0 | is-number 7.0.0 |
| `npm2` | npm 11.11.0, `--lockfile-version 2` | is-number 6.0.0 | adds left-pad 1.3.0 | |
| `npm1` | npm 11.11.0, `--lockfile-version 1` | | is-number 6.0.0, left-pad 1.3.0 | |
| `yarn` | yarn 1.22.22 (lockfile v1) | is-number 6.0.0 | adds left-pad 1.3.0 and @sindresorhus/is 4.6.0 | is-number 7.0.0 |
| `go` | go 1.21.6 | golang.org/x/text v0.14.0 | adds github.com/google/uuid v1.6.0 | golang.org/x/text v0.15.0 |
| `poetry` | poetry 1.8.2 (lock-version 2.0) | six 1.16.0 | adds idna 3.6 | six 1.17.0 |

Every file was written on 2026-09-28 by the tool named, with network access to its
registry. The cargo files were captured by the #630 design tracer on the same day.
