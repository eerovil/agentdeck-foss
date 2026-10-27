# Test fixtures — SYNTHETIC DATA ONLY

Everything the tests need is built at runtime by `conftest.py` into a
`tmp_path` (fake `CLAUDE_CONFIG_DIR` + fake `/proc` tree). Nothing real is
stored here.

**Never** copy real transcripts, `sessions/*.json`, `history.jsonl`, or
`.credentials.json` from an actual `~/.claude` into this directory. This repo
is public and lives on a machine with real data — treat that as the primary
leak vector.
