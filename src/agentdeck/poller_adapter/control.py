"""This card's control command, written to disk instead of spelled out in the brief (#1732).

Every dispatch used to restate the whole engine invocation — binary, manifest, overlay, binding,
deck URL, repository, card number and this dispatch's generation — on every command line,
which across one audited day was the single largest repeated block in the corpus. None of it is a
decision the model makes: it is host state the host already holds. So the host writes it once, as
a tiny executable, and the brief carries that path.

What matters more than the size:

* The generation is baked into the wrapper **and into its filename**, so a superseded worker
  keeps running its own older wrapper and is refused with the usual ``stale:`` reason. Sharing
  one wrapper per card would instead hand a stale worker the live generation, which is the one
  thing this must never do. A card's oldest wrappers are eventually pruned, so a long-superseded
  worker can find its own file gone instead; the brief tells it to read that as ``stale:``.
* Writing the wrapper can fail without costing a dispatch. Every caller falls back to the long
  form when this returns ``""``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

# What the directory of these wrappers is called inside a Poller Instance's state directory.
# Named here rather than spelled out by whoever wires it up, so the layout has one owner.
DIR_NAME = "card-control"

# The lifecycle verbs a worker is told about (#2155): the only operations a stale Run could
# corrupt Poller state with. The wrapper still passes any verb through, so `enabled` stays usable
# as a diagnostic.
VERBS = (
    "finish",
    "review",
    # Not lifecycle: offers the head for review while the turn goes on to verify it (#2350).
    "ready-for-review",
    # Takes back this Run's own `resolved` finish when a person asks for more (#2522).
    "reopen",
    # Takes over the build of the spec a person approved in this chat (#2716).
    "start-implementation",
)

# How many of a card's past wrappers survive alongside the current one. A superseded generation
# still finds its own file (and its own refusal); ancient ones are not worth keeping.
KEPT_PER_CARD = 5

# A spec worker's brief for building the spec it wrote, beside its own wrapper (#2716).
BUILD_BRIEF_SUFFIX = ".build.md"

def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in value)


def card_control_dir(state_dir: Path | str) -> Path:
    """Where a Poller Instance keeps its card control wrappers."""
    return Path(state_dir) / DIR_NAME


def write_card_control(root: Path | str, directive: dict, base_command: str) -> str:
    """Write this dispatch's control wrapper and return the command a worker runs."""
    repo = str(directive.get("repo") or "")
    number = str(directive.get("number") or "")
    generation = str(directive.get("generation") or "")
    if not (root and repo and number and generation and base_command):
        return ""
    directory = Path(root)
    stem = f"{_safe(repo)}-{_safe(number)}"
    path = directory / f"{stem}-{_safe(generation)}"
    script = (
        "#!/bin/sh\n"
        f"# AgentDeck poller card control for {repo}#{number}, generation {generation}.\n"
        "# Written by the host at dispatch. The card and this dispatch's generation are already\n"
        "# in it, so a worker never restates them and never chooses them.\n"
        "set -e\n"
        'if [ "$#" -eq 0 ]; then\n'
        f'    echo "usage: $0 <{"|".join(VERBS)}> [arguments]" >&2\n'
        "    exit 2\n"
        "fi\n"
        'verb="$1"\n'
        "shift\n"
        # Quoted like `base_command` already is at the call site: these come from host state, but
        # nothing downstream enforces that a repo id or generation stays free of shell syntax.
        f'exec {base_command} "$verb" {shlex.quote(repo)} {shlex.quote(number)} '
        f'{shlex.quote(generation)} "$@"\n'
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    except OSError:
        return ""
    _prune(directory, stem, keep=path)
    return str(path)


def write_build_brief(card_control: str, text: str) -> str:
    """Write the brief a spec worker follows if asked in its chat to build the spec (#2716).

    It sits beside the wrapper and shares its generation, so it is pruned with it. ``""`` when
    it could not be written: the spec brief then keeps the poller's own build start.
    """
    if not (card_control and text.strip()):
        return ""
    path = Path(f"{card_control}{BUILD_BRIEF_SUFFIX}")
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        return ""
    return str(path)


def _prune(directory: Path, stem: str, *, keep: Path) -> None:
    """Keep this card's recent wrappers and drop the rest. Never fatal — they are tiny."""
    try:
        existing = sorted(
            (
                item
                for item in directory.glob(f"{stem}-*")
                if item.is_file() and not item.name.endswith(BUILD_BRIEF_SUFFIX)
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for item in existing[KEPT_PER_CARD:]:
        if item == keep:
            continue
        for path in (item, Path(f"{item}{BUILD_BRIEF_SUFFIX}")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
