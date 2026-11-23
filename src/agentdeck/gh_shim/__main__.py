"""``python -m agentdeck.gh_shim`` — what the generated ``gh`` script execs."""

from __future__ import annotations

import os
import sys

from .install import MAX_NESTING, nesting, refuse_loop

if __name__ == "__main__":
    # A shim nested this deep is in a loop (#4505): refuse before importing the rest.
    arguments = sys.argv[1:]
    depth = nesting(dict(os.environ))
    if depth >= MAX_NESTING:
        sys.exit(refuse_loop(arguments, dict(os.environ), depth))
    from .main import main

    sys.exit(main())
