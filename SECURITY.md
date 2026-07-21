# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security vulnerabilities.

Report privately through GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/0xfauzi/kstrl/security/advisories/new).
This opens a confidential advisory visible only to you and the maintainer.

Please include enough to reproduce: the kstrl version, the agent CLI and model
in use, your configuration (redact secrets), and the observed vs expected
behavior. You will get an initial response as soon as reasonably possible for a
solo-maintained project; please allow time to investigate and prepare a fix
before any public disclosure.

## Supported versions

kstrl is pre-1.0 and ships from `main`. Security fixes target the latest
released version (currently the `0.2.x` line) and `main`. Older versions are
not maintained.

| Version | Supported |
|---|---|
| `0.2.x` / `main` | Yes |
| < `0.2` | No |

## Threat model and scope

kstrl orchestrates AI coding agents that execute code, read and write files,
run subprocesses, and use API tokens. That makes the trust boundaries unusually
important. The following are documented in the codebase and are the right
starting points for any report:

- **Agent sandbox** - what worktree isolation does and does not bound (shell
  reads/writes, network): module docstring in `kstrl/sandbox.py` and the
  `[sandbox]` keys in [`docs/env-vars.md`](docs/env-vars.md).
- **Fixtures sandbox** - PRD-declared fixtures are LLM-emitted and treated as
  untrusted input; commands run without a shell in a scrubbed environment,
  functions run in a sandboxed subprocess, paths cannot escape the worktree.
  See `kstrl/fixtures.py` and
  [`ARCHITECTURE.md`](ARCHITECTURE.md#the-fixtures-sandbox).
- **Known limitations** - correlated-failure risk, self-reported flags treated
  as hints not signals, and the trusted fact-injection prompt: the "Known
  limitations" section of
  [`docs/adversarial-design.md`](docs/adversarial-design.md).

**In scope:** vulnerabilities in the kstrl harness itself - sandbox escapes,
unsafe handling of untrusted agent output, secret/token leakage, path-escape in
fixtures or worktrees, and injection into the mechanical verifier or PR flow.

**Out of scope:** bugs or vulnerabilities in code that agents write inside your
own projects (that is the output kstrl is designed to help you review, not a
flaw in kstrl); vulnerabilities in third-party agent CLIs (Claude Code, Codex)
or their models; and issues that require an already-compromised host or
maliciously configured `kstrl.toml`.

## Handling secrets

kstrl reads provider tokens from the environment (for example
`KSTRL_LINEAR_TOKEN`). Do not paste real tokens into issues, discussions, or
PRs. If you believe a token was exposed through kstrl's logs, events, or PR
bodies, report it privately as above.
