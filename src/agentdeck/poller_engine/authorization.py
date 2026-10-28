"""Who may steer: two ordered controller tiers, three outcomes (#180, designed in #110).

Authorization is an engine invariant, not an adapter's judgment call. The host supplies one
raw fact — what permission level GitHub reports for a login — and every decision about what
that means is made here, so no adapter can accidentally authorize something.

Two tiers, ordered: **Work ⊂ Release**. Release is a strict superset, so a Release Controller
always holds Work authority and a maintainer is never blocked from a work command. A project
states each tier's bar and allowlist in the Manifest's controller slot, and the two combine by
**union, grant-only** — an allowlist can add a person, never subtract one.

Three outcomes, and the third is the important one:

* **Authorized** — positively confirmed: met the role bar, or on the allowlist.
* **Denied** — positively confirmed insufficient.
* **Uncertain** — could not be confirmed either way. The command defers and is retried on later
  wakes: never granted, never dropped. A GitHub blip must not silently discard a legitimate
  ``/merge``, and an outage must not authorize one.

The invariant is *no positive confirmation, no action*. That is fail-closed in effect while
keeping today's behavior that transient failures retry rather than deny.

Two further rules that are easy to get wrong: ``author_association`` is only a cheap
pre-filter, never a grant on its own — only the live permission lookup separates Work from
Release — and the engine's own bot account is never a controller, whatever role it holds, so it
cannot authorize its own comments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .intake import CommandKind


class Tier(Enum):
    WORK = "work"        # request work, stop work, hand ownership to the engine
    RELEASE = "release"  # publish: irreversible, and everything Work can do


class Verdict(Enum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    UNCERTAIN = "uncertain"  # no positive confirmation → no action, but never dropped


# GitHub's collaborator permission levels, weakest first.
LEVELS = ("none", "read", "triage", "write", "maintain", "admin")

# #110's defaults, expressed as ordered bars: Work accepts triage and above (its "write, triage
# also accepted"), Release requires maintain or admin. Both are per-project settings.
DEFAULT_BARS = {Tier.WORK: "triage", Tier.RELEASE: "maintain"}

# An author with no relationship to the repository cannot qualify by role, so the lookup can be
# skipped for them — the pre-filter, never a grant.
_NO_RELATIONSHIP = frozenset({"NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"})

# Which tier each command needs. `/merge review` sits at Release because it opens the guarded,
# auto-progressing publication pipeline: the authority to start it equals the authority to merge.
# `/reviewed` sits there for the same reason one step removed (#2108): it writes one of the three
# bits that let the poller merge with nobody in the loop, so whoever can write it can ship code,
# and the authority to say so has to equal the authority to merge.
REQUIRED_TIERS = {
    CommandKind.CLAUDE: Tier.WORK,
    CommandKind.STOP: Tier.WORK,
    CommandKind.MERGE: Tier.RELEASE,
    CommandKind.MERGE_REVIEW: Tier.RELEASE,
    CommandKind.REVIEWED: Tier.RELEASE,
}

# Handing the engine ownership by hand (adding or removing the label) is a work-level act.
OWNERSHIP_TIER = Tier.WORK

# An outside reviewer's account, a Work controller in every project whatever its Manifest says
# (#2441). Every review chat AgentDeck opens posts its findings as one `/claude` comment from the
# reviewer's App, and a project that had not named it dropped them unheard — three projects never
# did. Work only: it may ask for a turn and, where ``auto_release`` is off, report a `/reviewed`
# pass, but `/merge` and a merging `/reviewed` still need Release, which only a Manifest can
# grant. It stays an app, so it may steer a card and never start one (#2056), and the engine's
# own account is still never a controller even if it is this one.
#
# Which accounts those are is the host's to say, in ``github-authority.toml`` (#3623)::
#
#     [reviewers]
#     logins = ["my-review-app[bot]"]
#
# With none listed no account holds this grant: it is not a default anyone gets.
def is_bot_login(login: object) -> bool:
    """True for a login GitHub only ever gives an app: the ``[bot]`` suffix.

    The suffix is all some reads carry — there is no user object to read a type off — so this
    is the one question askable everywhere a login is known.
    """
    return str(login or "").endswith("[bot]")


def reviewer_logins() -> frozenset[str]:
    from . import github_authority

    return github_authority.reviewer_logins()


def _rank(level: str | None) -> int:
    try:
        return LEVELS.index(str(level or "none").lower())
    except ValueError:
        return -1  # a level this engine does not know is not a grant


@dataclass(frozen=True)
class TierPolicy:
    bar: str
    allow: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Policy:
    """One project's controller policy, from the Manifest's ``[authorization]`` slot."""

    tiers: dict[Tier, TierPolicy] = field(default_factory=dict)
    bot: str = ""

    def tier(self, tier: Tier) -> TierPolicy:
        return self.tiers.get(tier, TierPolicy(bar=DEFAULT_BARS[tier]))

    def allows(self, tier: Tier, login: str) -> bool:
        """Is this login named for this tier, or for the tier above it? (Or the reviewer, #2441.)

        Release is a superset, so someone allowlisted for Release also holds Work — otherwise a
        maintainer could be refused the work command they are trusted to publish.
        """
        target = login.lower()
        if target in self.tier(tier).allow:
            return True
        return tier is Tier.WORK and (
            target in self.tier(Tier.RELEASE).allow or target in reviewer_logins()
        )

    def names(self, login: str) -> bool:
        """Is this login allowlisted for any tier at all? (#2033)

        A bot-authored comment is dropped before the grammar is even read, because a bot is not
        a controller. That is right for every bot a project did not ask for, and wrong for the
        one it did: an app a person runs deliberately — a review integration, a linter that
        knows how to say ``/claude`` — is a controller they chose, and its commands were
        vanishing with no ledger record and no refusal. Naming it in the controller allowlist
        is that choice, and this is the only question the scan has to ask to honour it. The
        engine's own account is never it, whatever the allowlist says, so an engine that
        somehow named itself still cannot smuggle its own comments back in as commands.
        """
        target = (login or "").lower()
        if not target or (self.bot and target == self.bot):
            return False
        return target in reviewer_logins() or any(target in self.tier(tier).allow for tier in Tier)


def parse_policy(raw: dict) -> Policy:
    """Read the controller slot. #177 validates its shape; this reads what is there."""
    tiers: dict[Tier, TierPolicy] = {}
    for tier in Tier:
        block = raw.get(tier.value)
        if not isinstance(block, dict):
            block = {}
        bar = str(block.get("role") or DEFAULT_BARS[tier]).lower()
        allow = block.get("allow") or []
        tiers[tier] = TierPolicy(
            bar=bar if bar in LEVELS else DEFAULT_BARS[tier],
            allow=frozenset(
                str(login).lower() for login in allow if isinstance(login, str) and login
            ),
        )
    return Policy(tiers=tiers, bot=str(raw.get("bot") or "").lower())


def required_tier(
    kind: str,
    *,
    extension_tiers: dict[str, str] | None = None,
    reviewed_merges: bool = True,
) -> Tier | None:
    """The tier a command kind needs, or None when no commenter authority applies.

    An extension-declared command carries its own tier (#112 declares it, the engine enforces
    it); an unknown tier is read as Release, because guessing *down* would be a privilege
    escalation and guessing up only ever asks for more proof.

    ``/reviewed`` sits at Release only because its pass can merge with nobody in the loop, and
    it can do that only where the project merges on its release bits (``auto_release``).
    Everywhere else (``reviewed_merges`` false, #2375) a pass asks for at most a verify turn —
    the same thing a Work-tier ``/claude`` asks for — so it needs Work. That is what lets an
    outside reviewer that holds only a bot account report a clean pass at all.
    """
    if kind == CommandKind.REVIEWED.value and not reviewed_merges:
        return Tier.WORK
    for command_kind, tier in REQUIRED_TIERS.items():
        if command_kind.value == kind:
            return tier
    declared = (extension_tiers or {}).get(kind)
    if declared is None:
        return None
    return Tier.WORK if str(declared).lower() == Tier.WORK.value else Tier.RELEASE


def needs_lookup(policy: Policy, tier: Tier, *, login: str, association: str | None) -> bool:
    """Is a live permission lookup needed at all? (The allowlist and pre-filter are free.)"""
    return _prefilter(policy, tier, login=login, association=association) is None


def _prefilter(
    policy: Policy, tier: Tier, *, login: str, association: str | None
) -> Verdict | None:
    """Everything decidable without asking GitHub. None means: a lookup is required."""
    target = (login or "").lower()
    if not target:
        return Verdict.DENIED
    if policy.bot and target == policy.bot:
        # No self-authorization: the engine's own account is never a controller.
        return Verdict.DENIED
    if policy.allows(tier, target):
        return Verdict.AUTHORIZED
    if association is not None and str(association).upper() in _NO_RELATIONSHIP:
        # No relationship to the repository, so no role can qualify. Cheap, and a deny only —
        # an association is never a grant on its own. An *absent* association is unknown, not
        # "none": it means no shortcut is available, so the live lookup decides.
        return Verdict.DENIED
    return None


def decide(
    policy: Policy,
    tier: Tier,
    *,
    login: str,
    association: str | None = None,
    permission: str | None = None,
) -> Verdict:
    """Decide one command's authorization (see module docstring).

    ``permission`` is the live collaborator level the host looked up, or ``None`` when it could
    not be determined — which is uncertainty, not a denial.
    """
    early = _prefilter(policy, tier, login=login, association=association)
    if early is not None:
        return early
    if permission is None:
        return Verdict.UNCERTAIN
    if _rank(permission) < 0:
        # A level this engine does not recognize is not something to interpret either way.
        return Verdict.UNCERTAIN
    return (
        Verdict.AUTHORIZED
        if _rank(permission) >= _rank(policy.tier(tier).bar)
        else Verdict.DENIED
    )


def holds(policy: Policy, tier: Tier, *, login: str, permission: str | None) -> bool:
    """Does this login positively hold this tier? Used for the activation approval gate (#109)."""
    return decide(policy, tier, login=login, association="COLLABORATOR", permission=permission) is (
        Verdict.AUTHORIZED
    )
