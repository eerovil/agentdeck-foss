#!/usr/bin/env python3
"""Create (or complete) the Project board a Poller Project needs, idempotently (#183).

A poller reads and writes one board, and the engine needs a Status column for each role it moves a
card through. Doing that by hand is six clicks and easy to get subtly wrong — a missing `Needs More
Info`, or `In review` spelled `In Review` on one board and not another — so this script creates the
board if it is absent and adds only the options that are missing.

It is safe to re-run: an existing board is reused rather than duplicated, and existing options are
preserved. It prints the number to put in the Manifest's `board.project_number`.

    python3 scripts/create_poller_board.py --owner eerovil --title "AgentDeck Poller"

Requires a non-human credential that may create the board (#2289): an organization board takes
the AgentDeck App's installation token (Organization projects: write), and `eerovil-agent` can
create boards only under its own account, so a new board owned by a person is created by that
person on GitHub, off this host. Pass the credential explicitly:

    GH_TOKEN=<installation token> python3 scripts/create_poller_board.py --owner example-org ...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# The roles the engine moves a card through. Backlog is optional — a board without an intake column
# works, the poller simply parks nothing there — so it is offered but not required.
REQUIRED_OPTIONS = (
    "Todo",
    "In progress",
    "In review",
    "Done",
    "Needs More Info",
    "Blocked",
)
OPTIONAL_OPTIONS = ("Backlog",)
_COLORS = {
    "Backlog": "GRAY",
    "Todo": "BLUE",
    "In progress": "YELLOW",
    "In review": "ORANGE",
    "Done": "GREEN",
    "Needs More Info": "PURPLE",
    "Blocked": "RED",
}


def _gh(*args: str, stdin: str | None = None) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            ["gh", *args], input=stdin, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout or "", done.stderr or ""


def _json(*args: str) -> object | None:
    code, out, err = _gh(*args)
    if code != 0:
        print(err.strip() or f"gh {' '.join(args)} failed", file=sys.stderr)
        return None
    try:
        return json.loads(out or "null")
    except ValueError:
        return None


def find_board(owner: str, title: str) -> dict | None:
    """An existing board with this title, so re-running never makes a second one."""
    listing = _json("project", "list", "--owner", owner, "--format", "json", "--limit", "100")
    if not isinstance(listing, dict):
        return None
    for project in listing.get("projects") or []:
        if isinstance(project, dict) and project.get("title") == title:
            return project
    return None


def board_by_number(owner: str, number: int) -> dict | None:
    """The board with this number. A board's number is what the Manifest stores and is the only
    handle on it that a rename cannot break — matching on title would quietly create a second
    board the moment someone edits the first one's name in the UI."""
    board = _json("project", "view", str(number), "--owner", owner, "--format", "json")
    return board if isinstance(board, dict) and board.get("number") == number else None


def create_board(owner: str, title: str) -> dict | None:
    return _json("project", "create", "--owner", owner, "--title", title, "--format", "json")


def status_field(owner: str, number: int) -> dict | None:
    """The board's Status single-select field, with the options it currently has."""
    fields = _json(
        "project", "field-list", str(number), "--owner", owner, "--format", "json", "--limit", "50"
    )
    if not isinstance(fields, dict):
        return None
    for field in fields.get("fields") or []:
        if isinstance(field, dict) and field.get("name") == "Status":
            return field
    return None


def ensure_options(
    field: dict, *, wanted: tuple[str, ...], keep_optional: bool
) -> tuple[list[dict], list[str]]:
    """The option set to write, and which names are being added.

    Existing options are preserved exactly as they are — including a spelling that differs from the
    role name, since the Manifest maps a column's own spelling onto a role. Only genuinely missing
    ones are appended.
    """
    existing = [
        option for option in field.get("options") or [] if isinstance(option, dict)
    ]
    have = {str(option.get("name", "")).strip().casefold() for option in existing}
    additions = [
        name
        for name in (*wanted, *(OPTIONAL_OPTIONS if keep_optional else ()))
        if name.casefold() not in have
    ]
    options = [{"name": option["name"], "color": option.get("color") or "GRAY", "description": ""}
               for option in existing]
    options += [{"name": name, "color": _COLORS.get(name, "GRAY"), "description": ""}
                for name in additions]
    return options, additions


_ASSIGNMENTS_QUERY = """
query($owner:String!,$number:Int!){user(login:$owner){projectV2(number:$number){
  id
  field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}
  items(first:100){nodes{id fieldValueByName(name:"Status"){
    ... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}
"""

_SET_STATUS = (
    "mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
    "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){"
    "projectV2Item{id}}}"
)


def read_assignments(owner: str, number: int) -> dict | None:
    """Every card's current Status, plus the ids needed to put it back.

    ``updateProjectV2Field`` replaces the option *set*; the input type carries no option id, so
    GitHub treats every option as new and **clears the Status of every card on the board**. The
    only way to add a column without losing that data is to record it first and write it back.
    """
    body = json.dumps(
        {"query": _ASSIGNMENTS_QUERY, "variables": {"owner": owner, "number": number}}
    )
    code, out, err = _gh("api", "graphql", "--input", "-", stdin=body)
    if code != 0:
        print(err.strip(), file=sys.stderr)
        return None
    try:
        board = json.loads(out)["data"]["user"]["projectV2"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(board, dict) or not isinstance(board.get("field"), dict):
        return None
    assigned = {}
    for node in (board.get("items") or {}).get("nodes") or []:
        value = node.get("fieldValueByName") if isinstance(node, dict) else None
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            assigned[node["id"]] = value["name"]
    return {
        "project_id": board.get("id"),
        "field_id": board["field"].get("id"),
        "assigned": assigned,
    }


def restore_assignments(state: dict, owner: str, number: int) -> list[str]:
    """Put every recorded Status back, matching the column by its name. Returns what could not
    be restored, so a partial failure is reported rather than left silently on the board."""
    if not state["assigned"]:
        return []
    field = status_field(owner, number) or {}
    by_name = {
        str(option.get("name", "")).casefold(): option.get("id")
        for option in field.get("options") or []
        if isinstance(option, dict)
    }
    failed = []
    for item_id, name in state["assigned"].items():
        option_id = by_name.get(name.casefold())
        if option_id is None:
            failed.append(f"{item_id} -> {name} (no such column after the write)")
            continue
        code, _out, err = _gh(
            "api", "graphql", "-f", f"query={_SET_STATUS}", "-f", f"p={state['project_id']}",
            "-f", f"i={item_id}", "-f", f"f={state['field_id']}", "-f", f"o={option_id}",
        )
        if code != 0:
            failed.append(f"{item_id} -> {name}: {err.strip()}")
    return failed


def write_options(field_id: str, options: list[dict]) -> bool:
    """Replace the option set. Callers must save and restore card assignments around this —
    see ``read_assignments`` for why the replacement is destructive."""
    query = (
        "mutation($f:ID!,$o:[ProjectV2SingleSelectFieldOptionInput!]!){"
        "updateProjectV2Field(input:{fieldId:$f,singleSelectOptions:$o}){"
        "projectV2Field{... on ProjectV2SingleSelectField{id options{name}}}}}"
    )
    # The option set is a list of objects. `gh api -f`/`-F` can only send scalars, so a JSON
    # dump of it arrives as a String and the mutation is rejected; post the whole request body
    # instead, where the variable keeps its real type.
    body = json.dumps({"query": query, "variables": {"f": field_id, "o": options}})
    code, _out, err = _gh("api", "graphql", "--input", "-", stdin=body)
    if code != 0:
        print(err.strip(), file=sys.stderr)
    return code == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="the GitHub user or organization")
    parser.add_argument("--title", default="AgentDeck Poller", help="the board's title")
    parser.add_argument(
        "--project-number",
        type=int,
        help="complete this existing board instead of matching one by title; never creates",
    )
    parser.add_argument("--with-backlog", action="store_true", help="also add a Backlog column")
    parser.add_argument("--dry-run", action="store_true", help="say what would change")
    args = parser.parse_args(argv)
    if not (os.environ.get("GH_TOKEN") or "").strip():
        # Never the host's own `gh` login: the credential is stated explicitly (#2289).
        print(
            "refused: set GH_TOKEN to a non-human credential that may create this board;"
            " the host's own gh login is never used",
            file=sys.stderr,
        )
        return 2

    if args.project_number is not None:
        board = board_by_number(args.owner, args.project_number)
        if board is None:
            print(
                f"no board #{args.project_number} under {args.owner} that this credential can "
                "read — check the number, or that the token has the `project` scope",
                file=sys.stderr,
            )
            return 1
        print(f"completing board #{board.get('number')} — {board.get('url')}")
    else:
        board = _resolve_by_title(args)
        if board is None:
            return 0 if args.dry_run else 77

    number = int(board.get("number") or 0)
    return _complete_status(args, board, number)


def _resolve_by_title(args) -> dict | None:
    """The board named by ``--title``, created if absent. Returns None when there is nothing to
    complete — either the dry run stopped short of creating, or creation failed."""
    board = find_board(args.owner, args.title)
    if board is None and not args.dry_run:
        board = create_board(args.owner, args.title)
        if board is None:
            print(
                "could not create the board — a credential with the `project` scope is needed:\n"
                "  gh auth refresh -s project,read:project",
                file=sys.stderr,
            )
            return None
        print(f"created board #{board.get('number')} — {board.get('url')}")
    elif board is None:
        print(f"would create a board titled {args.title!r} under {args.owner}")
        return None
    else:
        print(f"reusing board #{board.get('number')} — {board.get('url')}")
    return board


def _complete_status(args, board: dict, number: int) -> int:
    """Add only the Status options the engine needs and that the board has not got."""
    field = status_field(args.owner, number)
    if field is None:
        print("the board has no Status field to complete", file=sys.stderr)
        return 1
    options, additions = ensure_options(
        field, wanted=REQUIRED_OPTIONS, keep_optional=args.with_backlog
    )
    if not additions:
        print("Status already has every column the engine needs")
    elif args.dry_run:
        print("would add: " + ", ".join(additions))
    else:
        # Save every card's Status before touching the option set — the write clears them all.
        state = read_assignments(args.owner, number)
        if state is None:
            print(
                "could not read the board's current card statuses, so adding a column would "
                "risk clearing them — refusing rather than writing blind",
                file=sys.stderr,
            )
            return 1
        if state["assigned"]:
            record = Path(f"board-{number}-statuses.json")
            record.write_text(json.dumps(state["assigned"], indent=2))
            print(f"saved {len(state['assigned'])} card status(es) to {record}")
        if not write_options(str(field.get("id")), options):
            return 1
        print("added: " + ", ".join(additions))
        failed = restore_assignments(state, args.owner, number)
        if failed:
            print(
                "the columns were added but these card statuses could not be put back:\n  "
                + "\n  ".join(failed),
                file=sys.stderr,
            )
            return 1
        if state["assigned"]:
            print(f"restored {len(state['assigned'])} card status(es)")

    print(f"\nput this in the Manifest:\n\n[board]\nproject_number = {number}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
