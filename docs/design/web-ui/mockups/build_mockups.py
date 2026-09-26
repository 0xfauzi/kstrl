"""Generate the static mock-ups of the kstrl operator web UI.

    python3 docs/design/web-ui/mockups/build_mockups.py

Writes one self-contained HTML file per screen next to this script. Every
screen shares one stylesheet (inlined) and one page shell, so a change to a
token or a component lands on every screen at once. The content is the real
fixture content of the snippetvault build and the e3root integration-loop
run: component names, run ids, costs, findings and gate output are copied,
not invented. The only external request a page makes is the web font.
"""

from __future__ import annotations

import mock_decide
import mock_home
import mock_ops
import mock_runs


def main() -> None:
    mock_home.build_home()
    mock_home.build_home_empty()
    mock_home.build_error_config()
    mock_runs.build_run_board()
    mock_runs.build_component_detail()
    mock_runs.build_gate_output()
    mock_runs.build_decompose()
    mock_runs.build_spec_issues()
    mock_runs.build_integration()
    mock_decide.build_failures()
    mock_decide.build_failures_empty()
    mock_decide.build_retry_confirm()
    mock_decide.build_retry_blocked()
    mock_decide.build_inbox()
    mock_decide.build_inbox_halted()
    mock_decide.build_inbox_empty()
    mock_decide.build_checkpoint()
    mock_ops.build_config()
    mock_ops.build_learning()
    mock_ops.build_start()
    mock_ops.build_start_init()
    mock_ops.build_serve()
    mock_ops.build_safe_mode()


if __name__ == "__main__":
    main()
