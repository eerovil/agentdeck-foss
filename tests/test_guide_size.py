"""How big a repository's guide is, with what it imports (#4724)."""

from agentdeck.guide_size import oversized_guide


def test_a_guide_counts_each_imported_file_once_and_the_bigger_guide_wins(tmp_path):
    (tmp_path / "docs").mkdir()
    # A loop of imports, a mid-line import, a missing file, a folder, a mention and a user
    # nobody has. An import shown as code counts too, once: code is not told from prose.
    (tmp_path / "docs" / "shown.md").write_text("s" * 500)
    (tmp_path / "CLAUDE.md").write_text(
        "See @docs/a.md and @gone.md, @docs, @eero, @~nosuchuser_zz/x.md `@docs/shown.md`\n"
        "```text\n@docs/shown.md\n```\n    @docs/b.md\n@AGENTS.md\n"
    )
    (tmp_path / "docs" / "a.md").write_text("a" * 30 + "\n@../CLAUDE.md\n")
    (tmp_path / "docs" / "b.md").write_text("b" * 100)
    (tmp_path / "AGENTS.md").write_text("c" * 60)
    claude = len((tmp_path / "CLAUDE.md").read_text()) + 45 + 100 + 60 + 500

    assert oversized_guide(tmp_path, limit=claude) == (0, [])
    assert oversized_guide(tmp_path, limit=claude - 1) == (
        claude,
        ["docs/shown.md", "CLAUDE.md", "docs/b.md", "AGENTS.md", "docs/a.md"],
    )
    # Codex reads AGENTS.md alone, imports not followed: named when CLAUDE.md is smaller.
    (tmp_path / "CLAUDE.md").write_text("short")
    (tmp_path / "AGENTS.md").write_text("c" * 49 + "\n@docs/b.md")
    assert oversized_guide(tmp_path, limit=59) == (60, ["AGENTS.md"])
    assert oversized_guide(tmp_path / "nowhere", limit=0) == (0, [])
    # Imports are followed five deep, as Claude Code does: the sixth file is not loaded.
    for hop in range(7):
        (tmp_path / f"h{hop}.md").write_text(f"{hop}\n@h{hop + 1}.md\n")
    (tmp_path / "CLAUDE.md").write_text("@h1.md\n")
    (tmp_path / "AGENTS.md").write_text("x")
    assert sorted(oversized_guide(tmp_path, limit=0)[1]) == [
        "CLAUDE.md",
        *(f"h{n}.md" for n in range(1, 6)),
    ]
    # A file met five deep on one chain and directly on a later line is read at its
    # shortest distance, so what it imports is still counted.
    (tmp_path / "CLAUDE.md").write_text("@h1.md\n@h5.md\n")
    assert "h6.md" in oversized_guide(tmp_path, limit=0)[1]
