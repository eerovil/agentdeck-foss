"""Changed lines without whole-line comments, counted in a local checkout (#4812)."""

from __future__ import annotations

import json
import subprocess

import pytest

from agentdeck import code_lines
from agentdeck.code_lines import Counted, LocalCounter, count_patch, is_comment
from agentdeck.web.work_view import _code_lines_row


def _git(repo, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _commit(repo, files: dict[str, str | None], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "checkout"
    path.mkdir()
    _git(path, "init", "-q", "-b", "master")
    return path


@pytest.mark.parametrize(
    ("path", "line", "comment"),
    [
        ("a.py", "    # why it is so", True),
        ("a.py", "x = 1  # trailing", False),
        ("a.py", "", False),
        ("a.sh", "#!/bin/sh", False),
        ("a.py", "# -*- coding: latin-1 -*-", False),
        ("a.rb", "# frozen_string_literal: true", False),
        ("a.ts", "// note", True),
        ("a.ts", "/// <reference types='x' />", False),
        ("a.go", "//go:build linux", False),
        ("a.c", "/* one line */", True),
        ("a.c", "/* one */ x = 1;", False),
        ("a.c", " * middle of a block", False),
        ("a.c", "*ptr = value;", False),
        ("a.css", "* { margin: 0 }", False),
        ("a.css", "// not a css comment", False),
        ("a.scss", "// a sass comment", True),
        ("w.yml", "    # install deps", False),
        ("a.sql", "-- note", True),
        ("a.html", "<!-- note -->", True),
        ("a.html", "<!-- open", False),
        ("Makefile", "# a make comment", False),
        ("a.md", "# A heading", False),
    ],
)
def test_a_comment_is_only_a_whole_line_plainly_one(path, line, comment):
    assert is_comment(path, line) is comment


def test_a_patch_counts_code_lines_per_file_and_both_names_of_a_rename(repo):
    body = "\n".join(f"value_{n} = {n}" for n in range(20)) + "\n"
    base = _commit(repo, {"a.py": "x = 1\n", "old.py": body, "gone.c": "int x;\n"}, "base")
    head = _commit(
        repo,
        {
            "a.py": "# say why\nx = 2\n# and more\n\n",
            "old.py": None,
            "new name.py": body + "# moved\n",
            "gone.c": None,
            "b.bin": "\0\1\2",
        },
        "head",
    )
    patch = _git(repo, "diff", "-U0", "-M", "--no-color", f"{base}...{head}")
    counted = count_patch(patch)
    assert counted is not None
    assert counted.files == {
        "a.py": (2, 1),  # ``x = 2`` and the blank line added, ``x = 1`` removed
        "new name.py": (0, 0),
        "gone.c": (0, 1),
        "b.bin": (0, 0),
    }
    assert counted.renamed == {"old.py", "new name.py"}
    assert counted.total == 4


def test_a_quoted_name_leaves_the_whole_patch_uncounted():
    patch = 'diff --git "a/t\\tab.py" "b/t\\tab.py"\n--- "a/t\\tab.py"\n+++ "b/t\\tab.py"\n'
    assert count_patch(patch + "@@ -1 +1 @@\n-a\n+b\n") is None


async def test_counts_only_where_a_checkout_holds_both_commits_and_keeps_only_counts(repo):
    base = _commit(repo, {"a.py": "x = 1\n"}, "base")
    head = _commit(repo, {"a.py": "# why\nx = 2\n"}, "head")
    counter = LocalCounter()
    assert await counter.count("o/r", base, head) is None, "no checkout remembered"
    counter.remember("O/R", str(repo.parent / "missing"))
    assert await counter.count("o/r", base, "f" * 40) is None
    assert counter._counted == {}, "an unknown is not kept as a count"
    counter.remember("o/r", str(repo.parent / "missing"))
    calls = []
    real = counter._count_in

    async def counting(*args):
        calls.append(args)
        return await real(*args)

    counter._count_in = counting
    assert await counter.count("o/r", base, "f" * 40) is None
    assert calls == [], "an unknown stands a while for the same checkouts"
    counter.remember("o/r", str(repo))
    counted = await counter.count("o/r", base, head)
    assert counted == Counted({"a.py": (1, 1)})
    assert counter.held("o/r", base, head.upper()) is counted
    assert counter.held("o/r", "e" * 40, head) is None, "another base is another diff"


async def test_a_minor_fix_count_needs_the_old_head_to_be_an_ancestor(repo):
    base = _commit(repo, {"a.py": "x = 1\n"}, "base")
    old = _commit(repo, {"a.py": "x = 2\n"}, "old")
    new = _commit(repo, {"a.py": "x = 3\n# why\n"}, "new")
    _git(repo, "checkout", "-q", "-b", "rewrite", base)
    rewritten = _commit(repo, {"a.py": "y = 9\n"}, "rewritten")
    counter = LocalCounter()
    counter.remember("o/r", str(repo))
    assert await counter.count("o/r", old, new, ancestor=True) == Counted({"a.py": (1, 1)})
    assert await counter.count("o/r", old, rewritten, ancestor=True) is None
    assert counter.held("o/r", old, new) is None, "a fix's own diff is not a pull request's size"


def test_the_work_row_takes_the_local_count_of_its_head_and_leaves_the_rest(monkeypatch):
    counter = LocalCounter()
    counter._keep(("o/r", "b", "h", False), Counted({"a.py": (3, 1)}))
    counter._keep(("o/r", "b2", "h", False), Counted({"a.py": (5, 0)}))
    monkeypatch.setattr(code_lines, "COUNTER", counter)
    row = {
        "headRefOid": "H",
        "baseRefOid": "B",
        "files": [
            {"path": "a.py", "additions": 9, "deletions": 4},
            {"path": "x.png", "additions": 0, "deletions": 0},
        ],
    }
    recounted = _code_lines_row("O/R", row)
    assert recounted["files"] == [
        {"path": "a.py", "additions": 3, "deletions": 1},
        {"path": "x.png", "additions": 0, "deletions": 0},
    ]
    assert row["files"][0]["additions"] == 9, "the cached row keeps GitHub's numbers"
    retargeted = _code_lines_row("o/r", {**row, "baseRefOid": "b2"})
    assert retargeted["files"][0]["additions"] == 5, "one head against two bases: two counts"
    no_base = {k: v for k, v in row.items() if k != "baseRefOid"}
    assert _code_lines_row("o/r", no_base) == no_base
    assert _code_lines_row("o/r", {**row, "headRefOid": "other"}) == {**row, "headRefOid": "other"}


async def test_the_review_counters_take_the_local_count_and_fall_back_to_github(monkeypatch):
    from agentdeck import review_paperwork, review_refs, work_router

    class Fake:
        def __init__(self) -> None:
            self.asked: list[tuple] = []

        async def count(self, repository, base, head, *, ancestor=False):
            self.asked.append((repository, base, head, ancestor))
            if head == "local":
                return Counted({"src/a.py": (2, 1)}, frozenset())
            return None

    fake = Fake()
    monkeypatch.setattr(code_lines, "COUNTER", fake)

    async def run(argv, timeout=None, **kw):
        if argv[1:3] == ["pr", "view"] and "baseRefName,baseRefOid" in argv:
            return 0, b'{"baseRefName": "master", "baseRefOid": "basesha"}'
        if argv[1:3] == ["pr", "view"]:
            head = "local" if argv[3] == "1" else "remote"
            row = {"body": "", "files": [], "baseRefOid": "basesha", "headRefOid": head}
            return 0, json.dumps({**row, "additions": 700, "deletions": 400}).encode()
        if argv[1:3] == ["issue", "view"]:
            return 0, b'{"comments": []}'
        return 0, b'{"files": 1, "lines": 50}'

    monkeypatch.setattr(review_refs.gh_cli, "run", run)
    monkeypatch.setattr(review_paperwork.gh_cli, "run", run)
    assert await review_refs.diff_sizes("o/r", 9, ("local", "local")) == {"local": 3}
    assert await review_refs.diff_sizes("o/r", 9, ("local", "remote")) == {
        "local": 50,
        "remote": 50,
    }, "one head GitHub's: both are, so growth compares like with like"
    assert (await review_paperwork.read("o/r", 1, "o/r", 5)).changed == 3
    assert (await review_paperwork.read("o/r", 2, "o/r", 5)).changed == 1100
    assert ("o/r", "basesha", "local", False) in fake.asked

    compared: list[tuple] = []

    async def github_compare(repository, old, new):
        compared.append((old, new))
        return None

    monkeypatch.setattr(work_router, "compared_changes", github_compare)

    async def files(repository, number):
        return {"src/a.py"}

    monkeypatch.setattr(work_router, "pull_files", files)
    before = frozenset({"src/a.py"})
    why: list[str] = []
    assert not await work_router.no_code_between(
        "o/r", 9, "old", "remote", before, minor=True, why=why
    )
    assert compared == [("old", "remote")], "unknown locally: GitHub's compare decides"
    assert ("o/r", "old", "remote", True) in fake.asked
    assert await work_router.no_code_between("o/r", 9, "old", "local", before, minor=True)
    assert compared == [("old", "remote")], "counted locally: no compare call"
