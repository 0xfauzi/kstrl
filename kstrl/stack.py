"""A project's ``[stack]``: instructions for the models, and the commands kstrl runs (#696).

The owner charter (CLAUDE.md) rules that kstrl holds no language-specific
code or config. A stack is how a project says what it is built with. It is
instruction text the models read, plus commands kstrl runs and judges WITHOUT
knowing what they are: a check passes when it exits 0 and fails on any other
exit. kstrl parses none of their output to decide.

The table in kstrl.toml has four keys, and all four are required::

    [stack]
    instructions = "What the project is built with, and how to work in it."
    setup = "make deps"        # "" states that worktrees need no setup
    env = ["TOOL_HOME"]        # variables the commands may see
    [stack.checks]             # run in this order; each must exit 0
    tests = "make test"
    lint = "make lint"

With no ``[stack]`` table nothing changes: kstrl reads ``[verify]`` as before.
With one, it is the only source of verification commands, and every other
source is refused by name (:data:`OTHER_COMMAND_SOURCES`). Slice 3 of #696
adds the human confirmation of a stack; until then a ``[stack]`` is used as
it is loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.config_numbers import check_numbers
from kstrl.config_toml import ConfigError, load_toml_document, section_table

#: The four keys of ``[stack]``. Every one is required.
STACK_KEYS: tuple[str, ...] = ("instructions", "setup", "checks", "env")

#: A name that holds any of these is never passed to a command, whoever
#: declared it (#696 decision 11). ``verify.scrubbed_subprocess_env`` drops
#: the same names; one tuple, so the refusal and the scrub cannot disagree.
SECRET_NAME_FRAGMENTS: tuple[str, ...] = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)

#: kstrl's own variables: kstrl sets the ones a command needs itself, so a
#: stack may not pass one through (``KSTRL_REPORT`` among them).
KSTRL_ENV_PREFIX = "KSTRL_"

#: A POSIX environment variable name.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Every other place a verification command can come from. With ``[stack]``
#: present each one is refused by name: the stack is the one source.
#: ``(section, key)`` rows are kstrl.toml keys; ``(None, name)`` rows are
#: environment variables. The ``ks factory`` flags are refused in
#: ``cli.factory``, which is the only command that has them.
OTHER_COMMAND_SOURCES: tuple[tuple[str | None, str], ...] = (
    ("verify", "test_command"),
    ("verify", "typecheck_command"),
    ("verify", "lint_command"),
    ("verify", "test_tool"),
    ("verify", "typecheck_tool"),
    ("verify", "lint_tool"),
    ("factory", "worktree_setup_command"),
    ("contract", "test_command"),
    ("breaker", "test_command"),
    (None, "KSTRL_VERIFY_TEST_CMD"),
    (None, "KSTRL_VERIFY_TYPECHECK_CMD"),
    (None, "KSTRL_VERIFY_LINT_CMD"),
    (None, "KSTRL_VERIFY_TEST_TOOL"),
    (None, "KSTRL_VERIFY_TYPECHECK_TOOL"),
    (None, "KSTRL_VERIFY_LINT_TOOL"),
    (None, "KSTRL_FACTORY_WORKTREE_SETUP_COMMAND"),
    (None, "KSTRL_CONTRACT_TEST_CMD"),
    (None, "KSTRL_BREAKER_TEST_CMD"),
)

#: H3: engineer-facing CONTEXT, rendered into every engineer prompt of a run
#: whose project has a ``[stack]`` (``loop.build_project_context``). The
#: calibration suite scores no engineer-context fixture, so this carries the
#: H3 obligation and no H2 obligation the suite can discharge (CLAUDE.md, the
#: DECISIONS_CONTEXT_PROMPT position). Only the template is pinned: the
#: instructions and the checks are the operator's, interpolated at run time.
STACK_PROMPT_VERSION = "1.0.0"

STACK_PROMPT = """\
# Stack (from this project's kstrl.toml [stack])

The operator chose this project's stack. Build in it, and follow these
instructions:

{instructions}

## Checks

kstrl runs these commands on your work, in this order, from the root of the
tree. A check passes only when it exits 0. Run them yourself before you report
a story complete. They are the only verification commands: ignore any other
list, including one written in the project context above.

{checks}"""


class StackError(ConfigError):
    """A ``[stack]`` table kstrl will not use, with every reason indexed.

    A :class:`ConfigError`, so every surface that renders one renders this.
    Its own class because three loaders read the stack (``VerifyConfig``,
    ``FactoryConfig`` and ``ContractConfig``) besides :class:`StackConfig`,
    and the entry check (``config_preflight.collect_config_problems``) says a
    stack's fault once, not once per loader.
    """


@dataclass(frozen=True)
class Stack:
    """One loaded ``[stack]``: never partial, see :func:`load_stack`."""

    instructions: str
    setup: str
    #: ``(name, command)`` in the order kstrl.toml lists them.
    checks: tuple[tuple[str, str], ...]
    env: tuple[str, ...]

    @property
    def check_names(self) -> tuple[str, ...]:
        return tuple(name for name, _command in self.checks)

    @property
    def digest(self) -> str:
        """SHA-256 of the table's canonical JSON: what the stack IS.

        Key order and check order are part of the text: checks run in the
        order they are listed, so reordering them is a different stack.
        """
        canonical = json.dumps(
            {
                "instructions": self.instructions,
                "setup": self.setup,
                "checks": [list(check) for check in self.checks],
                "env": list(self.env),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def format_for_prompt(self) -> str:
        """The block ``loop.build_project_context`` hands the engineer."""
        checks = "\n".join(f"- {name}: `{command}`" for name, command in self.checks)
        return STACK_PROMPT.format(instructions=self.instructions.strip(), checks=checks)


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _env_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return [f"env must be a list of variable names, got {value!r}"]
    errors: list[str] = []
    for index, name in enumerate(value):
        where = f"env[{index}]"
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            errors.append(f"{where} must be a variable name, got {name!r}")
        elif any(fragment in name.upper() for fragment in SECRET_NAME_FRAGMENTS):
            errors.append(
                f"{where} {name} looks like a secret ({', '.join(SECRET_NAME_FRAGMENTS)}); "
                "a check never sees a secret"
            )
        elif name.startswith(KSTRL_ENV_PREFIX):
            errors.append(f"{where} {name} is kstrl's own; kstrl sets what a check needs")
    return errors


def _checks_errors(value: object) -> list[str]:
    if not isinstance(value, dict):
        return [f"checks must be a table of name = command, got {value!r}"]
    if not value:
        return ["checks is empty; a stack runs at least one check"]
    return [
        f"checks.{name} must be a non-empty command string, got {command!r}"
        for name, command in value.items()
        if not _nonempty_text(command)
    ]


def stack_errors(raw: dict[str, Any]) -> list[str]:
    """Every reason ``raw`` is not a usable ``[stack]``, indexed; [] when it is.

    The one vocabulary of a valid stack. A bad entry is refused, never
    dropped: a dropped check is a check that silently stopped running.
    """
    errors = [
        f"{key} is not a [stack] key; the keys are {', '.join(STACK_KEYS)}"
        for key in raw
        if key not in STACK_KEYS
    ]
    errors += [f"{key} is required" for key in STACK_KEYS if key not in raw]
    if "instructions" in raw and not _nonempty_text(raw["instructions"]):
        errors.append(f"instructions must be non-empty text, got {raw['instructions']!r}")
    if "setup" in raw and not isinstance(raw["setup"], str):
        errors.append(f'setup must be a command string, or "" for none, got {raw["setup"]!r}')
    if "checks" in raw:
        errors += _checks_errors(raw["checks"])
    if "env" in raw:
        errors += _env_errors(raw["env"])
    return errors


def _second_sources(document: Mapping[str, Any]) -> list[str]:
    """Every other command source in play beside ``[stack]``."""
    found: list[str] = []
    for section, key in OTHER_COMMAND_SOURCES:
        if section is None:
            if key in os.environ:
                found.append(f"the environment sets {key}")
            continue
        table = document.get(section)
        if isinstance(table, Mapping) and key in table:
            found.append(f"[{section}] {key} is set")
    return [
        f"{source}; with [stack] present the stack is the only source of "
        "verification commands, so remove it"
        for source in found
    ]


def load_stack(root_dir: Path) -> Stack | None:
    """The project's ``[stack]``, or None when kstrl.toml has none.

    Raises :class:`StackError` (exit 2 at every surface) for a table with any
    error, and for any other command source set beside it. Never returns a
    partial stack. Read through ``config_toml.load_toml_document``, so a parse
    fault is the same ``ConfigError`` every loader raises.
    """
    from kstrl.config import resolve_config_file

    path = resolve_config_file(root_dir)
    if not path.exists():
        return None
    document = load_toml_document(path)
    if "stack" not in document:
        return None
    raw = section_table(document, "stack", path)
    errors = stack_errors(raw) + _second_sources(document)
    if errors:
        raise StackError(f"[stack] in {path} is refused:\n    " + "\n    ".join(errors))
    return Stack(
        instructions=str(raw["instructions"]),
        setup=str(raw["setup"]),
        checks=tuple((str(name), str(command)) for name, command in raw["checks"].items()),
        env=tuple(str(name) for name in raw["env"]),
    )


@dataclass
class StackConfig:
    """The entry check's handle on ``[stack]`` (``config_preflight``).

    It loads the stack only to report a bad one, once and under ``[stack]``,
    before the three loaders that also read it for their own sections
    (``FactoryConfig``, ``VerifyConfig``, ``ContractConfig``), which are the
    ones that hold it. No field, so ``ks config show`` renders no row for it.
    """

    @classmethod
    def load(cls, root_dir: Path | None = None) -> StackConfig:
        load_stack(Path.cwd() if root_dir is None else root_dir)
        return check_numbers(cls())
