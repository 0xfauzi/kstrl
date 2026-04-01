"""Model selector widget - agent type + model dropdown."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, Select

from ralph.models import (
    KNOWN_AGENTS,
    detect_installed_agents,
    get_default_model,
    get_models_for_agent,
)


class ModelSelector(Widget):
    """Compound widget for selecting agent type and model."""

    DEFAULT_CSS = """
    ModelSelector {
        height: auto;
    }
    ModelSelector Vertical {
        height: auto;
    }
    ModelSelector Horizontal {
        height: auto;
    }
    """

    class Changed(Message):
        """Emitted when the agent or model selection changes."""

        def __init__(self, agent_type: str, model: str) -> None:
            self.agent_type = agent_type
            self.model = model
            super().__init__()

    def compose(self) -> ComposeResult:
        installed = detect_installed_agents()

        # Build agent options
        agent_options: list[tuple[str, str]] = []
        for aid, info in KNOWN_AGENTS.items():
            suffix = "" if aid in installed else " (not installed)"
            agent_options.append((f"{info.name}{suffix}", aid))
        agent_options.append(("Custom command", "custom"))

        with Vertical():
            with Horizontal():
                yield Label("Agent:", classes="selector-label")
                yield Select(
                    agent_options,
                    id="agent-select",
                    value=installed[0] if installed else "custom",
                )
            with Horizontal():
                yield Label("Model:", classes="selector-label")
                yield Select(
                    self._model_options(installed[0] if installed else ""),
                    id="model-select",
                )

    def _model_options(self, agent_type: str) -> list[tuple[str, str]]:
        models = get_models_for_agent(agent_type)
        if not models:
            return [("(no models)", "")]
        default = get_default_model(agent_type)
        options = []
        for m in models:
            label = f"{m.name} - {m.description}"
            if m.id == default:
                label = f"{m.name} (default) - {m.description}"
            options.append((label, m.id))
        return options

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "agent-select":
            agent_type = str(event.value) if event.value != Select.BLANK else ""
            # Update model dropdown
            model_select = self.query_one("#model-select", Select)
            model_select.set_options(self._model_options(agent_type))
            self.post_message(
                self.Changed(agent_type, get_default_model(agent_type))
            )
        elif event.select.id == "model-select":
            agent_select = self.query_one("#agent-select", Select)
            agent_type = str(agent_select.value) if agent_select.value != Select.BLANK else ""
            model = str(event.value) if event.value != Select.BLANK else ""
            self.post_message(self.Changed(agent_type, model))

    @property
    def agent_type(self) -> str:
        select = self.query_one("#agent-select", Select)
        return str(select.value) if select.value != Select.BLANK else ""

    @property
    def model(self) -> str:
        select = self.query_one("#model-select", Select)
        return str(select.value) if select.value != Select.BLANK else ""
