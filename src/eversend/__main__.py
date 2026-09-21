"""``python -m eversend`` entry point."""

from __future__ import annotations

import sys

from .desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
