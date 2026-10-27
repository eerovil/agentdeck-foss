"""``python -m agentdeck.gh_shim`` — what the generated ``gh`` script execs."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
