# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""``python -m cleanporter`` entry point; the CLI itself is in `cli`."""

import sys

from cleanporter import cli

if __name__ == "__main__":
    sys.exit(cli.main())
