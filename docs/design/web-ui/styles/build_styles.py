"""Build the visual-direction prototypes: three directions, two screens each.

python3 docs/design/web-ui/styles/build_styles.py
sh docs/design/web-ui/styles/render.sh
"""

from __future__ import annotations

import theme_command
import theme_list


def main() -> None:
    theme_list.build_all()
    theme_command.build_all()


if __name__ == "__main__":
    main()
