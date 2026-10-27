"""A caching, rate-limited ``gh`` for deck-owned workers (#814).

Workers are told not to poll GitHub, and prose has no teeth: a worker that
loops ``gh pr view`` spends the very quota ``poller_engine/github.py`` reserves,
so the poller throttles itself to leave room for polling it forbids.  This
package makes the cheap path the only read path — reads resolve through the
shared ``GitHubMetadataCache``, writes pass straight through, and both a shared
quota floor and a per-session bucket bound what is left.

``install`` puts it on a worker's PATH; ``classify`` decides what an invocation
is; ``limits`` holds the two gates; ``main`` is the entry point.
"""

from .install import ensure_shim, real_gh, shim_dir, worker_path

__all__ = ["ensure_shim", "real_gh", "shim_dir", "worker_path"]
