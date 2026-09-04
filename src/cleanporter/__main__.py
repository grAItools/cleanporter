"""``python -m cleanporter`` entry point; the CLI itself is in `cli`."""

import sys

from cleanporter import cli

if __name__ == "__main__":
    sys.exit(cli.main())
