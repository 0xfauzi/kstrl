"""The [evolution] config section, its environment overrides and its paths."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kstrl.config_numbers import check_numbers

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


#: [evolution] keys that did something until #217 deleted the proposal
#: generator. Existing kstrl.toml files still set them, so they are neither
#: read nor refused: EvolutionConfig.load records which are present and
#: `ks evolve` names them.
RETIRED_EVOLUTION_KEYS: tuple[str, ...] = ("auto_propose", "auto_apply_computational")


@dataclass
class EvolutionConfig:
    enabled: bool = True
    journal_path: Path = field(default_factory=lambda: Path(".kstrl/evolution.jsonl"))
    experiments_path: Path = field(default_factory=lambda: Path(".kstrl/experiments.tsv"))
    min_pattern_frequency: int = 2
    lookback_runs: int = 10
    # #217: the [evolution] keys of the deleted proposal generator that
    # load() found in kstrl.toml, so `ks evolve` can name them instead of
    # dropping them silently. Provenance, not a setting: it has no toml
    # key of its own, and metadata["provenance"] keeps the surfaces that
    # sweep dataclass fields (cli._collect_toml_notes, gen_docs) off it.
    retired_keys: tuple[str, ...] = field(default=(), metadata={"provenance": True})

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config from environment variables only.

        Relative journal/experiments paths resolve against ``root_dir``
        (the project root), not the process CWD, matching :meth:`load`.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "evolution")
        if "enabled" in section:
            config.enabled = bool(section["enabled"])
        if "journal_path" in section:
            jp = str(section["journal_path"])
            config.journal_path = Path(jp) if Path(jp).is_absolute() else root_dir / jp
        if "experiments_path" in section:
            ep = str(section["experiments_path"])
            config.experiments_path = Path(ep) if Path(ep).is_absolute() else root_dir / ep
        if "min_pattern_frequency" in section:
            config.min_pattern_frequency = int(section["min_pattern_frequency"])
        if "lookback_runs" in section:
            config.lookback_runs = int(section["lookback_runs"])
        config.retired_keys = tuple(key for key in RETIRED_EVOLUTION_KEYS if key in section)
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return check_numbers(config)

    @classmethod
    def load_or_none(
        cls,
        root_dir: Path,
        warn: Callable[[str], None],
    ) -> EvolutionConfig | None:
        """:meth:`load`, but a config that will not parse returns None.

        The journal is an optional audit trail and every caller loads it
        in the middle of work that has already been paid for, so a typo
        in one of its knobs should cost the journal, not the run.

        Which exceptions that means is stated here rather than at a call
        site, because it is a fact about :meth:`load`: ``ValueError``
        from malformed TOML or a non-integer ``lookback_runs`` (from the
        file or from ``KSTRL_EVOLUTION_LOOKBACK_RUNS``), ``TypeError``
        from a toml array where a number belongs, and ``OSError`` from
        an unreadable ``kstrl.toml``. A future coercion added to
        ``load`` is then covered here instead of silently escaping a
        guard somebody wrote around a call.

        ``TypeError`` was added when #272 gave the same section an entry
        check: ``config_preflight.REJECTIONS`` treats it as operator
        input, and two lists that disagree would degrade the same value
        at startup and then raise on it mid-run.

        It is deliberately NOT
        ``config_preflight.SURFACE_REJECTIONS``, which is this tuple
        plus ``RuntimeError``. #289 tried importing that instead, on
        the reasoning above, and
        ``test_decompose.py::test_the_artifact_is_written_before_any_journal_work``
        failed: that test raises ``RuntimeError`` from :meth:`load` on
        purpose, to assert that an error the guard does NOT catch still
        leaves the halt artifact on disk. No coercion in :meth:`load`
        produces one, so widening to it could only ever swallow a
        defect, and the entry check degrading where this raises is the
        price of keeping that defect visible mid-run.

        The cost of that widening, stated because it is real: a
        ``TypeError`` from a DEFECT inside :meth:`load` - a None where a
        path belongs, a signature that stopped matching - now reads as
        "config unreadable, skipping journal" rather than surfacing. It
        cannot be narrowed to the toml-array case without inspecting the
        message, which would be guessing. The journal going quiet is the
        signal that a defect is hiding here, so treat a "skipping
        journal" warning on a config that looks correct as a bug report
        about this method rather than about the operator's file.

        Degrades loudly: ``warn`` is called with the parse failure.
        """
        try:
            return cls.load(root_dir)
        except (ValueError, TypeError, OSError) as exc:
            warn(f"Evolution config unreadable, skipping journal: {exc}")
            return None


def _apply_env_overrides(config: EvolutionConfig, root_dir: Path) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay)."""
    if "KSTRL_EVOLUTION_ENABLED" in os.environ:
        config.enabled = os.environ["KSTRL_EVOLUTION_ENABLED"].lower() in {
            "1",
            "true",
            "yes",
        }
    if "KSTRL_EVOLUTION_JOURNAL_PATH" in os.environ:
        raw = os.environ["KSTRL_EVOLUTION_JOURNAL_PATH"]
        config.journal_path = Path(raw) if Path(raw).is_absolute() else root_dir / raw
    if "KSTRL_EVOLUTION_LOOKBACK_RUNS" in os.environ:
        config.lookback_runs = int(os.environ["KSTRL_EVOLUTION_LOOKBACK_RUNS"])


def _resolve_relative_paths(config: EvolutionConfig, root_dir: Path) -> None:
    """Anchor relative journal/experiments paths to the project root.

    The bare ``EvolutionConfig()`` constructor keeps its historical
    CWD-relative defaults; the load/from_env paths always hand back
    absolute paths so ``ks factory --root X`` run from elsewhere
    cannot scatter ``.kstrl/`` state into the operator's CWD.
    """
    if not config.journal_path.is_absolute():
        config.journal_path = root_dir / config.journal_path
    if not config.experiments_path.is_absolute():
        config.experiments_path = root_dir / config.experiments_path
