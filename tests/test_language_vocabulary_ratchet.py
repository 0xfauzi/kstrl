"""kstrl/ may only lose target-ecosystem vocabulary, never gain it (#696).

The charter in CLAUDE.md says kstrl holds no language-specific code or
config, Python included. This guard measures how far ``kstrl/`` is from
that and stops the distance growing. It counts, per file and per
ecosystem label, the words of the vocabulary below: tool, ecosystem,
manifest and lockfile names of Python, JavaScript and TypeScript, Rust,
Go, the JVM, Ruby, PHP and .NET. ``PINNED_HITS`` pins every non-zero
count, and a pin may only fall: a count above its pin is red, a count
below it is red until the pin is lowered in the same diff, and a
missing row is a pin of 0, so a new file or a new label with any hit is
red. Pinning per label rather than per file means swapping one
ecosystem's word for another's inside one file is red too.

HOW A FILE IS COUNTED. The whole text of every file under ``kstrl/``,
any suffix, is split into tokens; nothing parses it, so no node type or
token type is enumerated and a string, a comment, a docstring and an
identifier are all counted the same way. A token is a run of ASCII
letters and digits, or such a run with its leading dot when the
character before the dot is not a letter, digit or underscore. So
``parse_pytest_output`` gives ``parse pytest output``, ``"*.py"`` gives
``.py`` while ``verify.py`` gives ``verify py``, and ``go.mod`` gives
``go mod``. A token is split again where a lower-case letter or digit
meets an upper-case one (``PyPI`` gives ``py pi``, ``TypeScript`` gives
``type script``), then lower-cased. A dotted token that is not itself an
entry loses its dot, so ``.venv``, ``.pytest_cache`` and
``.python-version`` count as ``venv``, ``pytest`` and ``python``. A
vocabulary entry is a sequence of one or more tokens; the stream is
matched left to right, longest entry first, and a token that is part of
a match is not matched again. The count is a function of the file's text
alone, so an edit moves it only when it adds or removes a vocabulary
word, and the pin is stable under every other edit.

WHY SOME WORDS ARE ONLY COUNTED QUALIFIED. Four bare words were counted
over ``kstrl/`` at 53829fb with this tokenizer and left out, because
most of their hits are not an ecosystem: ``node`` (125 hits in 14 files:
AST and graph nodes), ``go`` (144 in 40: the English verb), ``coverage``
(499 in 24: spend coverage, check coverage) and ``py`` (480 in 90: the
``verify.py`` in every citation of a kstrl module). Bare ``ast`` (99 in
8) is left out for the same reason, prose about kstrl's own AST guards.
Each is counted only in the forms that name the ecosystem:
``node_modules``, ``go test``, ``coverage json``, ``".coverage"``,
``".py"``, ``__init__.py``, ``import ast``. For the same reason
``coverage run``, ``coverage report`` and a bare ``cov`` are not
entries: the first matched ``BudgetCoverage`` followed by a comment
starting "Run-scoped", and ``cov`` is a loop variable in
``pipeline.py``.

WHAT IS NOT COUNTED. ``ALLOWLIST`` names the lines that are kstrl
running or installing itself, each with the inventory row that put it
out of scope. The needle's own text is cut out before counting and the
rest of its line is still counted, so a word added beside a needle is
red; the row also pins how many times the needle occurs, so a new use
of it, on a new line or on its own line, is red. Two limits are
disclosed and pinned as strict xfails at the bottom: a toolchain
outside the vocabulary is not seen, and neither is a name the
interpreter builds at run time. The guard FLAGS, so a word it
over-matches costs a false positive somebody reads, never a site
cleared.

WHEN IT IS RED. Re-derive the count by running this file and read the
message; do not type a number in. A rise is a new language-specific
site: remove it, or, if it is kstrl's own runtime, add an ``ALLOWLIST``
row with its reason. A fall is the point of the ratchet: lower the pin.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers.astwalk import KSTRL_PACKAGE, blind_spot

#: Ecosystem label -> the token sequences that count as it, written as
#: space-separated tokens in the form the tokenizer emits them.
VOCABULARY: dict[str, tuple[str, ...]] = {
    "python": (
        "python",
        "python3",
        "cpython",
        "pythonpath",
        "pythonwarnings",
        "pythonutf8",
        "pythonhashseed",
        "pythonioencoding",
        "pythondontwritebytecode",
        ".py",
        ".pyi",
        ".pyc",
        "pycache",
        "pep",
        "sys executable",
        "py compile",
        "import ast",
        "init py",
        "py typed",
    ),
    "pytest": ("pytest", "pytestmark", "addopts", "conftest", "unittest", "junitxml"),
    "mypy": ("mypy", "pyright"),
    "ruff": ("ruff", "flake8", "pylint", "pyflakes", "isort"),
    "uv": ("uv", "uvx"),
    "pip": (
        "pip",
        "pipx",
        "pipenv",
        "poetry",
        "conda",
        "setuptools",
        "setup py",
        "setup cfg",
        "requirements txt",
        "venv",
        "virtualenv",
        "virtual env",
    ),
    "pyproject": ("pyproject",),
    "tox": ("tox", "nox"),
    "vulture": ("vulture",),
    "mutmut": ("mutmut",),
    "coverage.py": (
        "coverage py",
        ".coverage",
        "coverage json",
        "coverage xml",
        "coveragerc",
        "pytest cov",
        "cov report",
        "cov config",
    ),
    "pypi": ("pypi", "py pi", "psf"),
    "npm": ("npm", "npx", "npmrc"),
    "node": ("nodejs", "node js", "node modules"),
    "yarn": ("yarn",),
    "pnpm": ("pnpm",),
    "bun": ("bun", "bunx"),
    "deno": ("deno",),
    "tsc": ("tsc", "tsconfig"),
    "typescript": ("typescript", "type script", ".ts", ".tsx", ".mts", ".cts"),
    "javascript": ("javascript", "java script", ".js", ".jsx", ".mjs", ".cjs"),
    "eslint": ("eslint", "eslintrc", "prettier"),
    "jest": ("jest",),
    "vitest": ("vitest",),
    "mocha": ("mocha",),
    "webpack": ("webpack", "vite"),
    "package.json": ("package json", "package lock"),
    "cargo": ("cargo",),
    "rust": ("rust", "rustc", "rustup", "rustfmt", "clippy", ".rs", "crates io"),
    "go": (
        "golang",
        "gofmt",
        "golangci",
        "gopath",
        "goroot",
        "go mod",
        "go sum",
        "go test",
        "go build",
        "go vet",
        "go run",
        ".go",
        "test go",
    ),
    "gradle": ("gradle", "gradlew"),
    "maven": ("maven", "mvn", "pom xml", "surefire"),
    "junit": ("junit",),
    "kotlin": ("kotlin", ".kt", ".kts"),
    "java": ("java", "jvm", ".java"),
    "ruby": ("ruby", "rspec", "gemfile", "bundler", ".rb"),
    "sarif": ("sarif",),
    "dotnet": ("dotnet", "nuget", "msbuild", "csproj"),
    "cmake": ("cmake",),
    "php": ("php", "phpunit", "composer json", "composer lock"),
}


@dataclass(frozen=True)
class AllowedSite:
    """Lines of one file that are kstrl's own runtime, not the target's."""

    path: str
    needle: str
    occurrences: int
    reason: str


#: Every row cites the "Out of scope" table of the #696 inventory.
ALLOWLIST: tuple[AllowedSite, ...] = (
    AllowedSite("serve.py", "sys.executable,", 1, "E28: the daemon starts -m kstrl factory"),
    AllowedSite(
        "serve.py",
        "python or sys.executable",
        1,
        "E27: the launchd job starts -m kstrl serve under kstrl's own interpreter",
    ),
    AllowedSite(
        "serve.py",
        'python: str = ""',
        2,
        "E27: the launchd plist's override for kstrl's own interpreter",
    ),
    AllowedSite("serve.py", "python=python", 1, "E27: the same override passed through"),
    AllowedSite("cli.py", "uv sync --extra sdk", 1, "E29: how to install kstrl's own extra"),
    AllowedSite("agents/sdk_runner.py", "uv sync --extra sdk", 1, "E29: kstrl's own extra"),
    AllowedSite(
        "agents/sdk_runner.py",
        "python -m kstrl.agents.sdk_runner",
        1,
        "E30: kstrl's own agent runner",
    ),
    AllowedSite(
        "agents/claude_sdk.py",
        "python -m kstrl.agents.sdk_runner",
        1,
        "E30: kstrl's own agent runner",
    ),
    AllowedSite(
        "agents/claude_sdk.py",
        "sys.executable",
        1,
        "E30: starts kstrl's own agent runner",
    ),
    AllowedSite("agents/proc.py", "sys.executable", 1, "E30: starts kstrl's own leash"),
    AllowedSite("isolation.py", "sys.executable", 1, "#700: kstrl's own runtime runs the canary"),
    AllowedSite(
        "write_guard.py",
        "sys.executable",
        2,
        "#700: kstrl's own runtime is the PreToolUse hook and the SessionStart hook",
    ),
    AllowedSite("agents/leash.py", "python -I -S leash.py", 1, "E30: kstrl's own leash"),
    AllowedSite("autonomy.py", "python -m", 3, "E31: python -m kstrl.calibration"),
    AllowedSite(
        "calibration.py",
        "python -m kstrl.calibration",
        3,
        "E31: kstrl measuring itself, the compare CLI's own usage line",
    ),
    AllowedSite(
        "calibration_ladder.py",
        "python -m kstrl.calibration",
        1,
        "E31: kstrl measuring itself",
    ),
    AllowedSite(
        "baseline.py",
        '"dead_code_ruff",',
        1,
        "#696 slice 8: a retired check's name, which a baseline written before "
        "the retirement carries and the comparison must recognise",
    ),
)

_TOKEN = re.compile(r"(?<![A-Za-z0-9_])\.[A-Za-z0-9]+|[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _tokens(text: str) -> list[str]:
    return [part.lower() for match in _TOKEN.finditer(text) for part in _CAMEL.split(match.group())]


def count_hits(text: str) -> Counter[str]:
    """Vocabulary hits in ``text``, by ecosystem label."""
    table = {
        tuple(variant.split()): label
        for label, variants in VOCABULARY.items()
        for variant in variants
    }
    longest = max((len(key) for key in table), default=0)
    tokens = [
        token[1:] if token.startswith(".") and (token,) not in table else token
        for token in _tokens(text)
    ]
    hits: Counter[str] = Counter()
    index = 0
    while index < len(tokens):
        for width in range(longest, 0, -1):
            label = table.get(tuple(tokens[index : index + width]))
            if label is not None:
                hits[label] += 1
                index += width
                break
        else:
            index += 1
    return hits


def _kstrl_files() -> list[Path]:
    return sorted(
        path
        for path in KSTRL_PACKAGE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def census() -> dict[str, Counter[str]]:
    """Hits per file under ``kstrl/``, allowlisted lines left out."""
    found: dict[str, Counter[str]] = {}
    for path in _kstrl_files():
        relative = path.relative_to(KSTRL_PACKAGE).as_posix()
        text = _text(path)
        for site in ALLOWLIST:
            if site.path == relative:
                text = text.replace(site.needle, " ")
        hits = count_hits(text)
        if hits:
            found[relative] = hits
    return found


#: Re-derived by running this file on the #696 slice 8 and slice 10 tree; never typed in by hand.
PINNED_HITS: dict[str, dict[str, int]] = {
    "__main__.py": {"python": 1},
    "adequacy.py": {"go": 1, "jest": 1, "python": 5, "ruby": 2, "vitest": 1},
    "agents/leash.py": {"python": 1},
    "agents/liveness.py": {"npm": 1},
    "agents/logging.py": {"python": 3},
    "agents/proc.py": {"python": 3},
    "appendio.py": {"python": 1},
    "autonomy.py": {"python": 1},
    "baseline.py": {"mypy": 1, "python": 1},
    "calibration_score.py": {"mypy": 1},
    "cli.py": {"python": 1},
    "cli_check.py": {"ruff": 4, "vulture": 1},
    "config.py": {"mypy": 1},
    "config_preflight.py": {"pyproject": 1, "python": 2, "ruff": 1},
    "config_report.py": {"pyproject": 1, "python": 1},
    "config_toml.py": {"pyproject": 1, "python": 4},
    "context.py": {"python": 1},
    "decisions.py": {"python": 1},
    "doctor_measure.py": {"pytest": 1},
    "evolution.py": {"mypy": 3, "pytest": 2, "python": 1, "ruff": 6, "vulture": 1},
    "factory.py": {"mypy": 5, "pip": 1, "uv": 2},
    "failure_excerpt.py": {"cargo": 2, "go": 3, "jest": 2, "rust": 5},
    "init_cmd.py": {"package.json": 1, "ruff": 1},
    "jsonread.py": {"pytest": 1},
    "manifest.py": {"python": 1},
    "names.py": {"mypy": 1},
    "operator_context.py": {"python": 1, "ruff": 1},
    "pipeline.py": {"mypy": 1, "python": 1},
    "pipeline_checks.py": {"python": 1},
    "pipeline_knowledge.py": {"mypy": 1},
    "procdispose.py": {"python": 8},
    "procgroup.py": {"python": 1},
    "runstate.py": {"mypy": 1},
    "serve.py": {"python": 2},
    "verify.py": {"mypy": 4, "pytest": 1, "python": 4},
    "workqueue.py": {"python": 1},
    "worktree_setup.py": {"node": 2},
}

#: A probe the counter must read exactly this way. It exercises every
#: tokenizer rule: a snake_case identifier, a CamelCase name, a quoted
#: suffix, a dotted manifest name, a two-token entry, a dotted name whose
#: dot is dropped and an ``__init__.py``. It ends on a one-token entry,
#: so a matcher that stops before the last token reads it short.
CONTROL = (
    'subprocess.run(["cargo", "test"])  # then uv run pytest\n'
    'parse_pytest_output(); PytestReport; PyPI; open("package.json"); glob("*.py"); go.mod\n'
    'Path(".venv") / "__init__.py"  # mypy\n'
)
CONTROL_HITS = Counter(
    {
        "cargo": 1,
        "uv": 1,
        "pytest": 3,
        "pypi": 1,
        "package.json": 1,
        "python": 2,
        "go": 1,
        "pip": 1,
        "mypy": 1,
    }
)


def test_the_counter_reads_the_control() -> None:
    assert count_hits(CONTROL) == CONTROL_HITS


def test_every_allowlisted_needle_occurs_as_pinned() -> None:
    wrong = []
    for site in ALLOWLIST:
        path = KSTRL_PACKAGE / site.path
        seen = _text(path).count(site.needle) if path.is_file() else 0
        if seen != site.occurrences:
            wrong.append(f"{site.path} {site.needle!r}: pinned {site.occurrences}, found {seen}")
    assert not wrong, (
        "An ALLOWLIST needle occurs a different number of times than its row says. "
        "More is a new site hiding under an old reason; fewer is a stale row.\n" + "\n".join(wrong)
    )


def test_kstrl_vocabulary_only_falls() -> None:
    found = census()
    rows = []
    for path in sorted(set(found) | set(PINNED_HITS)):
        counted, pinned = found.get(path, Counter()), PINNED_HITS.get(path, {})
        for label in sorted(set(counted) | set(pinned)):
            was, now = pinned.get(label, 0), counted.get(label, 0)
            if now > was:
                rows.append(
                    f"{path} {label}: pinned {was}, counted {now}. A new language-specific "
                    "word: remove it, or ALLOWLIST the line if it is kstrl's own runtime."
                )
            elif now < was:
                rows.append(f"{path} {label}: pinned {was}, counted {now}. Lower the pin to {now}.")
    assert not rows, "\n".join(rows)


@pytest.mark.xfail(strict=True, raises=AssertionError)
@pytest.mark.parametrize(
    "source",
    [
        pytest.param('subprocess.run(["zig", "build", "test"])\n', id="outside-vocabulary"),
        pytest.param('subprocess.run(["py" + "test", "-q"])\n', id="built-at-run-time"),
    ],
)
def test_disclosed_limit_is_not_counted(source: str) -> None:
    blind_spot(count_hits, source)
