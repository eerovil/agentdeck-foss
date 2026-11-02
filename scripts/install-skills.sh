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

# Every program this script starts costs a few milliseconds, more on a test pod, and
# the tests run it whole under a one-second cap (#3888). So retired paths are
# collected and removed by one rm, each skill is copied by one cp per account, and
# the lessons go to every file from one Python.
RETIRED=()

# dirname without starting a program; sets PARENT. A bare name such as `skills` has
# the parent `.`, so a relative override still lands where dirname put it.
parent_of() {
  local path="$1"
  while [ "$path" != "/" ] && [ "${path%/}" != "$path" ]; do path="${path%/}"; done
  case "$path" in
    /) PARENT=/ ;;
    */*) path="${path%/*}"; PARENT="${path:-/}" ;;
    *) PARENT=. ;;
  esac
}





if [ "${#RETIRED[@]}" -gt 0 ]; then
  rm -rf -- "${RETIRED[@]}"
fi

install_flavour() {  # <skill> <flavour-subdir> <dest-skills-root>
  local skill="$1" flavour="$2" dest_root="$3"
  local src="$SRC_ROOT/$skill/$flavour/SKILL.md"
  [ -f "$src" ] || { echo "  skip: no $flavour flavour for $skill"; return; }
  # A provider is "set up on this host" when its home exists; the skills root
  # itself may not, because Cursor only creates ~/.cursor/skills once something
  # installs into it. Skipping on the missing leaf would never install for Cursor
  # at all.
  parent_of "$dest_root"
  [ -d "$dest_root" ] || [ -d "$PARENT" ] || {
    echo "  skip: $dest_root missing"; return
  }
  local dest="$dest_root/$skill"
  [ -d "$dest" ] || mkdir -p "$dest"
  # Shared files live in the skill root beside the flavour dirs. Copy whatever is
  # there rather than a fixed name, so a self-contained skill installs cleanly and
  # a skill that ships an asset (a procedure doc, a helper script) gets it too.
  # The flavour's SKILL.md goes last, so it wins over a shared file of that name.
  local shared=() file
  for file in "$SRC_ROOT/$skill"/* "$SRC_ROOT/$skill"/.[!.]*; do
    if [ -f "$file" ]; then shared+=("$file"); fi
  done
  if [ "${#shared[@]}" -gt 0 ]; then
    cp -- "${shared[@]}" "$dest/"
  fi
  cp -- "$src" "$dest/SKILL.md"
  echo "  -> $dest"
}

for skill_dir in "$SRC_ROOT"/*/; do
  skill_dir="${skill_dir%/}"
  skill="${skill_dir##*/}"
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
if [ -f "$LESSONS" ]; then
  echo "lessons:"
  targets=()
  for cdir in $CLAUDE_SKILL_DIRS; do
    parent_of "$cdir"
    targets+=("$PARENT/CLAUDE.md")
  done
  parent_of "$CODEX_SKILLS_DIR"
  targets+=("$PARENT/AGENTS.md")
  python3 - "$LESSONS" "${targets[@]}" <<'PY'
import re, sys
from pathlib import Path

source = Path(sys.argv[1])
start, end = "<!-- agentdeck-lessons:start -->", "<!-- agentdeck-lessons:end -->"
block = f"{start}\n{source.read_text().strip()}\n{end}"
pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
for target in map(Path, sys.argv[2:]):
    if not target.parent.is_dir():
        print(f"  skip: {target.parent} missing")
        continue
    text = target.read_text() if target.exists() else ""
    if pattern.search(text):
        updated = pattern.sub(lambda _: block, text, count=1)
    else:
        updated = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    if updated != text:
        # Writing in place keeps the file's own permissions.
        with target.open("w") as handle:
            handle.write(updated)
    print(f"  lessons -> {target}")
PY
fi

echo "done."
