"""Start Forgeyard: `uv run python main.py` (migrations run on boot).

Same as `python -m forgeyard`; `python main.py agent ...` runs the agent (see forgeyard/cli.py).
"""

import sys

from forgeyard.cli import main

if __name__ == "__main__":
    sys.exit(main())
