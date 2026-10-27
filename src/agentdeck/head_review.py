"""Reviews the deck starts for a chat's current commit (#1127).

A session's commit chip answers one question — has anything reviewed the commit
this checkout is on? — and when the answer is no, it offers to find out. That
offer is what this module owns: the brief the review chat is started with, and
the rule for whether starting one is worth it at all.

The review runs in its own chat rather than in the session being looked at. The
commit belongs to that checkout, so the review has to run there; but a chat asked
to review its own work mid-turn would be reading its own context, and the verdict
would arrive in the middle of whatever it was doing. A separate chat in the same
directory reads the same commit and leaves the session alone.

The verdict is a word, not an essay: ``pass`` or ``changes-needed``, the same two
the poller's own merge reviewers report, so one commit has one vocabulary however
its review was started. The reasoning stays in the review chat's transcript,
which the chip links to while it runs.
"""

from __future__ import annotations

from .git_context import GitContext

# The two words a review may report. Kept here rather than only in the route's
# request model: the brief and the API have to agree, and they agree by reading
# one list.
REVIEW_VERDICTS = ("pass", "changes-needed")


def review_brief(
    *,
    repo: str,
    sha: str,
    base_url: str,
    branch: str | None = None,
) -> str:
    """What the review chat is told to do.

    Written for an agent that has just been started with no context but its
    working directory: it names the commit, says what a review of it means, and
    ends with the one call that makes the verdict outlive the chat.
    """
    where = f" on branch `{branch}`" if branch else ""
    return (
        f"Review and fix commit `{sha}` of `{repo}`{where}, in this working directory. "
        "Someone pressed the review-and-fix button on this chat's commit in AgentDeck.\n\n"
        "- Before invoking the skill, verify that `git rev-parse HEAD` is exactly the "
        "named commit and that `git status --porcelain` is empty. Stop without "
        "recording a verdict if either check fails; the skill must not absorb "
        "someone else's uncommitted work or review a different commit.\n"
        "- Run the installed `review-fix-pr` skill exactly once. Its procedure is the "
        "work: do not substitute `rate-my-pr`, hand-roll a review, or start a second "
        "full review pass. Scope the pass to the named commit and its base branch.\n"
        "- Follow the repository's root guide (`AGENTS.md`, or `CLAUDE.md` where there "
        "is no `AGENTS.md`). The skill may apply supported findings, commit them, and "
        "push or open/update the pull request. Do not merge or deploy.\n"
        "- Then record the verdict, which is what the deck keeps:\n\n"
        f"    curl -s -X POST {base_url}/api/review-verdicts \\\n"
        "      -H 'content-type: application/json' \\\n"
        f'      -d \'{{"repo": "{repo}", "sha": "{sha}", "verdict": "pass"}}\'\n\n'
        "  Use `pass` only when the skill found nothing to change and you would merge "
        "the named commit as it stands. Use `changes-needed` when the skill changed "
        "or pushed anything, or when the named commit should not merge. Post exactly "
        "one verdict against the original named commit after the skill finishes.\n"
        "- Finish by saying, in a few sentences, what you found and why you "
        "reported that verdict. The deck links this chat from the commit's chip, "
        "so this transcript is the reasoning behind the recorded word."
    )


def reviewable_commit(context: GitContext | None) -> tuple[str, str] | None:
    """The repository and commit a review would be about, or None.

    None means the deck cannot name both, which is every case where a review
    would have no subject: a chat outside a checkout, a directory git could not
    read, or a shared checkout sitting on some other chat's branch — the same
    condition that empties ``GitContext.branch``.
    """
    if context is None or not context.repository or not context.head_sha:
        return None
    return (context.repository, context.head_sha)
