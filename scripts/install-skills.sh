#!/usr/bin/env bash
# Install AgentDeck's provider-agnostic skills into each coding agent's skill dir.
#
# Single source lives in this repo under skills/<name>/ as:
#   skills/<name>/claude/SKILL.md  Claude flavour (Agent-tool fan-out)
#   skills/<name>/codex/SKILL.md   Codex flavour (agentdeck-delegate fan-out)
#   skills/<name>/cursor/SKILL.md  Cursor flavour (Task-tool fan-out)
#   skills/<name>/<asset>          optional shared files beside the flavours
#
# Each provider gets a flat skill dir: its SKILL.md plus every shared file from
# the skill root, so a SKILL.md's "read <file> in this folder" resolves. A skill
# whose SKILL.md is self-contained needs no shared files; a skill split into a
# provider-neutral procedure keeps it as REVIEW.md and every flavour defers to it.
# A skill missing a flavour installs for the providers it does have and is skipped
# for the rest, so a half-finished working tree still installs — but every skill
# tracked here is required to ship all three, and a test fails one that does not.
# A provider left without its own adapter does not fall back to a sibling's; it
# gets nothing, and the agent improvises (#768).
#
# Claude targets default to ~/.claude and ~/.claude2 (a two-login
# setup); override with CLAUDE_SKILL_DIRS (space-separated). Codex target
# defaults to ~/.codex; override with CODEX_HOME. Cursor's user skills live in
# ~/.cursor/skills; override with CURSOR_SKILL_DIRS (space-separated).
#
# ~/.cursor/skills-cursor is NOT a target: it is Cursor's own built-in skill
# directory, synced and pruned against its .sync-manifest.json, and Cursor's
# bundled create-skill skill says never to write there. ~/.cursor/skills is the
# personal skill root, and unlike ~/.claude/skills and ~/.codex/skills — which
# cursor-agent also scans — it needs no third-party-extensibility opt-in.
set -euo pipefail

SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../skills" && pwd)"

CLAUDE_SKILL_DIRS="${CLAUDE_SKILL_DIRS:-$HOME/.claude/skills $HOME/.claude2/skills}"
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
CURSOR_SKILL_DIRS="${CURSOR_SKILL_DIRS:-$HOME/.cursor/skills}"





install_flavour() {  # <skill> <flavour-subdir> <dest-skills-root>
  local skill="$1" flavour="$2" dest_root="$3"
  local src="$SRC_ROOT/$skill/$flavour/SKILL.md"
  [ -f "$src" ] || { echo "  skip: no $flavour flavour for $skill"; return; }
  # A provider is "set up on this host" when its home exists; the skills root
  # itself may not, because Cursor only creates ~/.cursor/skills once something
  # installs into it. Skipping on the missing leaf would never install for Cursor
  # at all.
  [ -d "$dest_root" ] || [ -d "$(dirname "$dest_root")" ] || {
    echo "  skip: $dest_root missing"; return
  }
  local dest="$dest_root/$skill"
  mkdir -p "$dest"
  cp "$src" "$dest/SKILL.md"
  # Shared files live in the skill root beside the flavour dirs. Copy whatever is
  # there rather than a fixed name, so a self-contained skill installs cleanly and
  # a skill that ships an asset (a procedure doc, a helper script) gets it too.
  find "$SRC_ROOT/$skill" -maxdepth 1 -type f -exec cp {} "$dest/" \;
  echo "  -> $dest"
}

for skill_dir in "$SRC_ROOT"/*/; do
  skill="$(basename "$skill_dir")"
  echo "$skill:"
  for cdir in $CLAUDE_SKILL_DIRS; do
    install_flavour "$skill" claude "$cdir"
  done
  install_flavour "$skill" codex "$CODEX_SKILLS_DIR"
  for udir in $CURSOR_SKILL_DIRS; do
    install_flavour "$skill" cursor "$udir"
  done
done

# The general lessons /dream keeps in docs/agents/lessons.md reach every agent on
# this host through a managed block in each Claude home's CLAUDE.md and in Codex's
# AGENTS.md. A copy, not an import: Codex has no @import. Only the text between the
# markers is ever rewritten; a person's own rules around it are left alone.
LESSONS="$SRC_ROOT/../docs/agents/lessons.md"
sync_lessons() {  # <instruction file>
  local target="$1"
  [ -d "$(dirname "$target")" ] || { echo "  skip: $(dirname "$target") missing"; return; }
  python3 - "$LESSONS" "$target" <<'PY'
import re, sys
from pathlib import Path

source, target = Path(sys.argv[1]), Path(sys.argv[2])
start, end = "<!-- agentdeck-lessons:start -->", "<!-- agentdeck-lessons:end -->"
block = f"{start}\n{source.read_text().strip()}\n{end}"
text = target.read_text() if target.exists() else ""
pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
if pattern.search(text):
    updated = pattern.sub(lambda _: block, text, count=1)
else:
    updated = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
if updated != text:
    # Writing in place keeps the file's own permissions.
    with target.open("w") as handle:
        handle.write(updated)
PY
  echo "  lessons -> $target"
}

if [ -f "$LESSONS" ]; then
  echo "lessons:"
  for cdir in $CLAUDE_SKILL_DIRS; do
    sync_lessons "$(dirname "$cdir")/CLAUDE.md"
  done
  sync_lessons "$(dirname "$CODEX_SKILLS_DIR")/AGENTS.md"
fi

echo "done."
