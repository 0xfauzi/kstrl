# Contributing to kstrl

Thank you for considering a contribution. kstrl is run as a formal project:
the process rules below are the mechanism that keeps an AI-assisted codebase
trustworthy, so they are requirements, not suggestions.

## Ways to help

- **Report a bug** or **request a feature** through the issue templates.
- **Ask a question** or **float an idea** in
  [Discussions](https://github.com/0xfauzi/kstrl/discussions).
- **Pick up roadmap work**: the current cycle is the Dark Factory roadmap
  ([`docs/dark-factory-roadmap.md`](docs/dark-factory-roadmap.md), tracking
  issue [#156](https://github.com/0xfauzi/kstrl/issues/156)). Wave-1 items
  (policy envelope, health trending, autonomy ladder, exception inbox) are the
  most self-contained entry points. Comment on an issue before starting so work
  is not duplicated.
- **Report a security vulnerability**: do *not* open a public issue. See
  [SECURITY.md](SECURITY.md).

## AI-assisted contributions

AI-generated pull requests are welcome - this is a tool for building software
with agents, after all. Two rules apply:

1. **Declare it.** The PR template asks whether an agent wrote the change. Say
   so honestly.
2. **It must be human-reviewed.** Per the project's H1 rule, AI-generated code
   is never gated by AI self-review. A human (you, and then the maintainer)
   reviews every change. Do not run `/code-review` on your own AI-generated
   code and present the result as review.

## Development setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). POSIX (macOS or
Linux); Windows is unsupported for concurrent worktrees.

```bash
git clone https://github.com/0xfauzi/kstrl.git
cd kstrl
uv sync --all-extras --dev
uv tool install -e .
```

There is a runnable end-to-end example under
[`examples/uv-python/`](examples/uv-python/) - its README shows dry-run and
fake-agent invocations you can use to exercise the loop without spending
tokens.

## Verification (run before every PR)

The canonical commands live in [CLAUDE.md](CLAUDE.md); the short version:

```bash
uv run pytest tests/ -v                 # tests
uv run mypy kstrl/ --strict             # typecheck
uv run ruff check kstrl/ tests/         # lint
uv run python scripts/gen_docs.py --check   # README generated sections in sync
```

CI runs these plus a coverage ratchet; the test suite must be green. The
CLI-reference and Configuration sections of `README.md` are generated - edit
the source (click commands / config dataclasses) or `scripts/gen_docs.py` and
run `uv run python scripts/gen_docs.py`, never those regions by hand.

## The process rules (H1-H4)

These are the project's standing rules. Full statements are in
[`docs/adversarial-roadmap.md`](docs/adversarial-roadmap.md) (Phase H) and
[CLAUDE.md](CLAUDE.md):

- **H1 - No self-review.** AI-generated code is gated by human review, never AI
  self-review.
- **H2 - Calibration on prompt change.** Any edit to an adversarial prompt body
  re-runs the calibration suite and records the detection delta.
- **H3 - Prompt versioning.** A prompt edit bumps its `*_PROMPT_VERSION` and the
  snapshot tuple in `tests/test_prompt_versions.py` in the same diff.
- **H4 - Tested vs assumed.** Every "done" claim states what was actually
  exercised versus assumed.

## Coding standards

Summarized from [CLAUDE.md](CLAUDE.md):

- `from __future__ import annotations` at the top of every file; type hints on
  all signatures; `T | None` over `Optional[T]`.
- `@dataclass` for data containers (`frozen=True` when immutable); `Protocol`
  for interfaces.
- snake_case functions, PascalCase classes, UPPER_SNAKE constants; absolute
  imports grouped stdlib / third-party / local.
- No bare `except:`; no mutable default arguments; no `pickle` on untrusted
  data.
- Match the surrounding code's idiom and comment density. Comments state
  constraints the code cannot show, nothing else.

## Pull request expectations

- One coherent change per PR. If it maps to a roadmap item, update the tracker
  doc's status in the same diff (audit-trail doctrine).
- Fill in the PR template: what changed, what was tested vs assumed (H4),
  whether an agent wrote it (H1), and - if a prompt changed - the calibration
  and version-bump boxes (H2/H3).
- Keep the adversarial mindset: when touching a role or its prompt, ask whether
  the change makes the role more skeptical or more eager to please. Prefer the
  former.

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE) and that you will uphold the
[Code of Conduct](CODE_OF_CONDUCT.md).
