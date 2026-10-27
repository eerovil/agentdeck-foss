"""``python -m agentdeck.heavy_shim`` — what a generated launcher execs."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
