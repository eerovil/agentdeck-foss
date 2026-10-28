"""Voice mode's fast agent (#3444): it answers at once and passes the message on.

By the owner's decisions on #3444, voice mode runs two agents. The chat itself (the
slow agent) does the work and knows nothing of this one. The fast agent talks as if
it were the chat: every phrase heard goes to it first, it says a short sentence at
once ("Starting on the login bug."), and the page sends its cleaned-up version of the
phrase to the chat through the composer's own Send. It fixes words speech
recognition misheard and drops only what is plainly not meant for the agent (noise,
a stray "thanks for watching"). The chat shows only the cleaned-up text. The chat's
replies are still heard through their own 🔊 paragraph, as before.

The fast agent's memory is only what was said by voice: the person's words, its own
answers, and the chat's 🔊 lines the page read aloud, which the page passes along
with the next phrase. It is one Haiku process per chat, kept open between phrases
(``claude -p`` with stream-json in and out), so each answer reads the conversation
so far from the prompt cache and costs about a second once warm (measured 0.8-1.4 s).
When a turn misses its timeout the process is closed, and the next phrase starts a
fresh one, given the voice conversation so far, which this module keeps — every
phrase heard, answered or not, and the 🔊 lines sent with it. Voice mode turning
off, ``IDLE_S`` without a phrase, a fourth chat needing one (``MAX_AGENTS``) and a
web restart close the process and forget that conversation too, by the owner's
decision on #3444: after a quiet spell a fresh start is fine, and the words still
reach the chat unchanged; only the fast agent's fixes lose their context.

The page asks as soon as a phrase's words come back, before its one-second send wait
(#3765), and throws the answer away when more words join the phrase. Its next ask, the
whole phrase, names the thrown-away one (``drop``): its lines leave the history, and a
process that answered it is told that answer was never spoken. The process keeps the
turn in its own transcript; closing it would cost a fresh start (about 1 s).

The page asks for the process (``warm``) when voice mode turns on and while each
phrase is transcribed, so a phrase rarely waits for one to start: the first saved
conversations (#3507) showed a fresh process at 2.5 s against 1.5 s warm, most of
them after a web restart. Haiku itself takes 1-2 s of that.

What it says is a bare acknowledgement that may name the topic but never promises an
action or states content, which the chat's own 🔊 reply would then say again
(#3507). Its instructions carry the owner's words (``WORDS``), the projects the deck
knows and the chat's title, so it can tell "Agent Dick" was AgentDeck.

No Claude account with room, a failure or a timeout means no answer: the page sends
the words as heard and says its own "Got it, working on it."

Every phrase is also saved, one JSON line per phrase in a file per chat under
``LOG_DIR`` (#3475), so the owner can read back how well the fast agent works: the
words heard, the 🔊 lines it was given, its reply as written, what it said and
proposed to send, and whether it timed out or failed. The page then reports what it
really sent under the phrase's ``id`` (``sent``): a send by hand meanwhile, an edit
or a disabled Send changes it. It also reports how long after the end of speech the
words came back, the send went and the first voice was heard (``timed``, #3765).
``/sessions/{key}/voice-log`` shows them. The saved file is only read back; the fast
agent's memory is still the one above.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import guess, guess_cli
from .models import Account

log = logging.getLogger(__name__)

MODEL = "haiku"
# A phrase the fast agent has not answered by then goes to the chat as heard.
TURN_TIMEOUT_S = 6.0
IDLE_S = 15 * 60
# Each one is a CLI process on the host that runs every deck-owned agent.
MAX_AGENTS = 3
# The voice conversation a fresh process is given, newest last.
HISTORY_LINES = 40
MAX_HEARD_CHARS = 2000
MAX_ALOUD_CHARS = 600
MAX_SAY_CHARS = 300
# A cleaned-up phrase this far from the words heard was reworded, not corrected.
MIN_RELAY_SHARE = 0.5
MAX_RELAY_SHARE = 1.5
NONE = "NONE"
# The saved voice conversations, one file per chat; past the size, the older half goes.
LOG_DIR = Path("~/.local/share/agentdeck/voice-log")
MAX_LOG_BYTES = 2 * 1024 * 1024
# The page's measured waits for a phrase (#3765), in ms from the end of speech.
TIMINGS = ("words", "sent", "voice")
MAX_TIMING_MS = 120_000
# How a phrase fared: answered, or why the words went to the chat as heard.
ANSWERED, TIMED_OUT, FAILED, NO_ACCOUNT, ENDED = (
    "answered",
    "timed out",
    "failed",
    "no account",
    "ended",
)

SYSTEM_PROMPT = """\
You are the voice of a coding agent, talking with the person it works for while they \
are away from the screen. Speak as that agent, in the first person. Another process \
does the actual work: you never do it, never say it is done, and never answer \
questions about the work yourself.

Each message gives you what speech recognition heard the person say, and sometimes \
what the agent said aloud since. Answer with exactly two lines:
SAY: one short spoken acknowledgement, {say_language}. You may name what \
it is about in a few words, such as "The login bug, got it." or "Passing that on." \
Never promise or describe an action, never say what you or the agent will do, and \
never give an answer, plan or opinion: the agent's own reply says all that. Vary the \
wording, and do not open with "Okay" or other filler. No code, file paths or links.
RELAY: their words with speech-recognition mistakes fixed, using the conversation and \
the words below to tell what they meant. Change only words that were clearly \
misheard; keep everything else exactly as heard, in the language heard. Write NONE \
only when it is plainly not speech meant for the agent: noise, a stray "thanks for \
watching", a half word. Then write "SAY:" with nothing after it. A one-word answer \
such as "juu", "joo", "yes", "no" or "ei" is never NONE, and a lone "you" right after \
the agent asked a question is the Finnish "juu".

Never add, guess or promise anything beyond what was heard.

Words this person often uses, which speech recognition mishears: {words}. A word that \
makes no sense where it was heard but sounds like one of these is that word: "Agent \
Dick" is AgentDeck, "high go" is Haiku, "Xing Xiong" is sing song, "Finish" said of \
a language is Finnish.\
{title}"""
# The owner's own words (#3507); the projects the deck knows and the chat's title
# are added to them when a process starts.
WORDS = (
    "AgentDeck",
    "Deckhand",
    "Work",
    "Works",
    "poller",
    "Claude",
    "Codex",
    "Haiku",
    "Sonnet",
    "Opus",
    "Jarvis",
    "Whisper",
    "Parakeet",
    "sing song",
    "Finnish",
    "voice mode",
    "spec-only",
    "pull request",
    "MuseScore",
)
MAX_PROJECT_WORDS = 30
# A spoken line opening with this filler loses it (#3507): heard every time, it grates.
_FILLER = re.compile(r"^(?:okay|ok)\b[\s,.!-]*", re.IGNORECASE)
# A phrase of only these words is a real answer, never noise, however the fast
# agent took it (#3521). Sounds such as "uh-huh" and "mm" are not here: the owner
# never answers with them.
ANSWER_WORDS = frozenset("juu joo jep jees kyllä ei en yes yeah yep yup no nope".split())
MAX_ANSWER_WORDS = 3
# Speech recognition writes the owner's Finnish "juu" as these: they are never
# dropped either, and right after a question they go on as "Juu.".
MISHEARD_JUU = frozenset({"you", "jo", "yo"})


@dataclass(frozen=True)
class Answer:
    """What to say now, and what to send the chat (None: send nothing)."""

    say: str
    relay: str | None


@dataclass
class _Agent:
    key: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    proc: asyncio.subprocess.Process | None = None
    history: list[str] = field(default_factory=list)
    used: float = field(default_factory=time.monotonic)
    # Closes it ``IDLE_S`` after its last phrase, whether or not another one comes.
    timer: asyncio.TimerHandle | None = None
    # Forgotten (voice mode off, idle, evicted): a process started for it after
    # that is closed at once rather than left running untracked.
    gone: bool = False
    # The process has not been given a phrase yet, so the next one carries the past.
    new: bool = True
    # The last phrase asked and how many lines it added to ``history``, taken out
    # again when the page throws its answer away (#3765).
    last_id: str = ""
    last_lines: int = 0


_agents: dict[str, _Agent] = {}
# Idle closes under way, kept so they are not collected before they finish.
_closing: set[asyncio.Task] = set()
# Each voice mode spell the page has ended, newest last: a ``warm`` for one of them
# arrived after its ``end`` and starts nothing (#3510 review).
_ended_runs: dict[str, None] = {}
MAX_ENDED_RUNS = 100


def system_prompt(projects=(), title: str = "", finnish: bool = False) -> str:
    """The instructions with the owner's words, ``projects`` and the chat's ``title``.

    ``finnish``: the owner always speaks Finnish, English words mixed in (#3525), so
    the acknowledgement is always Finnish."""
    words = dict.fromkeys((*WORDS, *list(projects)[:MAX_PROJECT_WORDS]))
    title = _clip(title, 200)
    return SYSTEM_PROMPT.format(
        words=", ".join(words),
        say_language="in Finnish" if finnish else "in the language they spoke",
        title=f"\n\nThe chat you speak for is titled: {title}" if title else "",
    )


def fast_command(binary: str, prompt: str | None = None) -> list[str]:
    """One Haiku process that takes phrase after phrase on stdin, with nothing loaded."""
    return [
        binary,
        "-p",
        "--model",
        MODEL,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt",
        prompt or system_prompt(),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


async def start_process(
    account: Account, prompt: str | None = None
) -> asyncio.subprocess.Process | None:
    """A fresh fast-agent process on ``account``, or None when it cannot start."""
    binary = shutil.which("claude")
    if binary is None:
        log.info("voice agent skipped: claude is not on PATH")
        return None
    try:
        return await asyncio.create_subprocess_exec(
            *fast_command(binary, prompt),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd="/",
            env=guess_cli._guess_env(account),
            limit=4 * 1024 * 1024,
        )
    except OSError as exc:
        log.info("voice agent skipped: %s", exc)
        return None


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


UNSPOKEN = (
    "Your last answer was never spoken: the person went on talking. Below is all they "
    "said; answer it as one."
)


def message(
    heard: str, aloud: list[str], history: list[str] | None = None, unspoken: bool = False
) -> str:
    """One phrase as the fast agent is told it; a fresh process also gets the past.
    ``unspoken``: its last answer was thrown away, as more words joined (#3765)."""
    parts = []
    if history:
        parts.append("Earlier in this voice conversation:\n" + "\n".join(history))
    if unspoken:
        parts.append(UNSPOKEN)
    if aloud:
        parts.append("Said aloud by the agent since:\n" + "\n".join(aloud))
    parts.append(f"Heard: {heard}")
    return "\n\n".join(parts)


def parse(reply: str, heard: str, question: bool = False) -> Answer:
    """The two lines of a reply. A missing or reworded RELAY sends the words heard;
    a short yes or no is always sent, a lone "you" after a ``question`` as "Juu."."""
    answer = _parse(reply, heard)
    words = set(re.findall(r"[^\W\d_]+", heard.casefold()))
    short = 0 < len(re.findall(r"[^\W\d_]+", heard)) <= MAX_ANSWER_WORDS
    if short and question and words <= MISHEARD_JUU:
        return Answer(answer.say, "Juu.")
    if short and words <= ANSWER_WORDS | MISHEARD_JUU and answer.relay is None:
        return Answer(answer.say, heard)
    return answer


def _parse(reply: str, heard: str) -> Answer:
    say, relay = "", None
    found = False
    for line in reply.splitlines():
        head, _, rest = line.strip().partition(":")
        if head.strip().upper() == "SAY":
            say = _clip(rest.strip().strip("\"'“”"), MAX_SAY_CHARS)
            unfilled = _FILLER.sub("", say)
            if unfilled:
                say = unfilled[0].upper() + unfilled[1:]
            # "(no reply)" and the like are stage directions, not words to speak.
            if say.startswith(("(", "[")):
                say = ""
        elif head.strip().upper() == "RELAY":
            found = True
            relay = rest.strip().strip("\"'“”") or None
    if not found:
        return Answer(say, heard)
    if relay is not None and relay.upper().rstrip(".") == NONE:
        # Someone who said "none" meant it; noise gets no answer either.
        if heard.upper().rstrip(".") == NONE:
            return Answer(say, heard)
        return Answer("", None)
    if relay is None:
        return Answer(say, heard)
    if not MIN_RELAY_SHARE * len(heard) <= len(relay) <= MAX_RELAY_SHARE * len(heard) + 20:
        return Answer(say, heard)
    return Answer(say, relay)


async def _ask(proc: asyncio.subprocess.Process, text: str) -> str | None:
    """One turn: the phrase in, the result's text out."""
    line = {"type": "user", "message": {"role": "user", "content": text}}
    proc.stdin.write((json.dumps(line) + "\n").encode())
    await proc.stdin.drain()
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            return None
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            if event.get("is_error"):
                return None
            usage = event.get("usage") or {}
            log.info(
                "voice agent turn: %s ms, cache read %s, write %s",
                event.get("duration_ms"),
                usage.get("cache_read_input_tokens"),
                usage.get("cache_creation_input_tokens"),
            )
            return str(event.get("result") or "")


async def _close(agent: _Agent) -> None:
    proc, agent.proc = agent.proc, None
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def _forget(agent: _Agent) -> None:
    """``agent`` gone: its timer, its process and its memory."""
    agent.gone = True
    if agent.timer is not None:
        agent.timer.cancel()
        agent.timer = None
    if _agents.get(agent.key) is agent:
        _agents.pop(agent.key)
    await _close(agent)


def _idle(agent: _Agent) -> None:
    """The idle timer fired: close ``agent``, unless a phrase is being answered."""
    agent.timer = None
    if _agents.get(agent.key) is not agent:
        return
    if agent.lock.locked():
        _arm(agent)
        return
    task = asyncio.get_running_loop().create_task(_forget(agent))
    _closing.add(task)
    task.add_done_callback(_closing.discard)


def _arm(agent: _Agent) -> None:
    if agent.timer is not None:
        agent.timer.cancel()
    agent.timer = asyncio.get_running_loop().call_later(IDLE_S, _idle, agent)


async def _tidy(keep: str) -> None:
    """Close the least recently used agents past ``MAX_AGENTS``."""
    while len(_agents) > MAX_AGENTS:
        oldest = min((a for a in _agents.values() if a.key != keep), key=lambda a: a.used)
        await _forget(oldest)


async def heard(
    app,
    key: str,
    text: str,
    aloud: list[str],
    phrase_id: str = "",
    run: str = "",
    drop: str = "",
) -> Answer | None:
    """The fast agent's answer to ``text`` heard in chat ``key``, or None for none:
    also for a phrase of a voice mode spell (``run``) already ended. ``drop`` names
    the phrase asked before whose answer the page threw away (#3765)."""
    text = _clip(text, MAX_HEARD_CHARS)
    if not text or (run and run in _ended_runs):
        return None
    aloud = [str(line) for line in aloud if str(line or "").strip()]
    agent = _agents.setdefault(key, _Agent(key))
    # Before clipping, which can cut a long line's closing question mark.
    question = _asked(agent, aloud)
    aloud = [_clip(line, MAX_ALOUD_CHARS) for line in aloud]
    agent.used = time.monotonic()
    _arm(agent)
    await _tidy(key)
    # Waiting on a start ``warm`` began is part of the wait the person hears.
    started = time.monotonic()
    async with agent.lock:
        # The thrown-away ask ran first, under this lock: its lines are the last ones.
        dropped = bool(drop) and drop == agent.last_id and agent.last_lines > 0
        if dropped:
            del agent.history[-agent.last_lines :]
        agent.last_id, agent.last_lines = "", 0
        fresh = not _running(agent)
        answer, outcome, reply = await _turn(app, agent, text, aloud, question, dropped)
        await asyncio.to_thread(
            _save,
            key,
            {
                "at": time.time(),
                "id": phrase_id,
                "heard": text,
                "aloud": aloud,
                "outcome": outcome,
                "reply": reply,
                "say": answer.say if answer else None,
                "relay": answer.relay if answer else text,
                "fresh": fresh,
                "ms": round((time.monotonic() - started) * 1000),
                **({"replaces": drop} if dropped else {}),
            },
        )
        if agent.gone:
            # Forgotten while this turn ran, its process maybe started meanwhile.
            await _close(agent)
        # Every phrase heard is the voice conversation, answered or not: the page
        # sent it to the chat either way, and a fresh process needs it for what follows.
        added = [f"Agent said aloud: {line}" for line in aloud]
        added.append(f"Person: {text}")
        if answer is not None and answer.say:
            added.append(f"You: {answer.say}")
        agent.history = (agent.history + added)[-HISTORY_LINES:]
        if phrase_id:
            agent.last_id, agent.last_lines = phrase_id, min(len(added), len(agent.history))
        return answer


async def warm(app, key: str, run: str = "") -> None:
    """Voice mode is listening in chat ``key``: start its fast agent now, so a phrase
    does not wait for a process to start (#3507). A web restart closes it, and the
    page asks again with its next phrase, while that phrase is transcribed. ``run``
    names the voice mode spell; one already ended starts nothing."""
    if run and run in _ended_runs:
        return
    agent = _agents.setdefault(key, _Agent(key))
    agent.used = time.monotonic()
    _arm(agent)
    await _tidy(key)
    if agent.lock.locked():
        return  # a phrase is being answered, or a start is under way
    async with agent.lock:
        if not _running(agent) and await _start(app, agent) == ENDED:
            await _close(agent)


def _running(agent: _Agent) -> bool:
    return agent.proc is not None and agent.proc.returncode is None


def _prompt(app, key: str) -> str:
    """The instructions for chat ``key``: the projects the deck knows and its title."""
    sessions = getattr(app.state.app_state, "sessions", None) or {}
    projects = sorted(
        {
            session.project_name
            for session in sessions.values()
            if session.project.authoritative and session.project_name
        }
    )
    session = sessions.get(key)
    title = session.display_title if session is not None else ""
    return system_prompt(projects, title, finnish=app.state.config.speech.language == "fi")


async def _start(app, agent: _Agent) -> str | None:
    """A process for ``agent``: None once started, else why not."""
    account = guess.guess_account(app.state.accounts, app.state.app_state.usage)
    if account is None:
        return NO_ACCOUNT
    agent.proc = await start_process(account, _prompt(app, agent.key))
    if agent.proc is None:
        return FAILED
    agent.new = True
    return ENDED if agent.gone else None


def _asked(agent: _Agent, aloud: list[str]) -> bool:
    """Whether the agent's last line read aloud was a question."""
    lines = aloud or [line for line in agent.history if line.startswith("Agent said aloud: ")][-1:]
    return bool(lines) and lines[-1].rstrip().endswith("?")


async def _turn(
    app, agent: _Agent, text: str, aloud: list[str], question: bool = False, dropped: bool = False
) -> tuple[Answer | None, str, str | None]:
    """The answer, how the phrase fared (``ANSWERED`` and the rest) and the raw reply.
    ``dropped``: the page threw the last answer away; a process that gave it is told."""
    if not _running(agent):
        why = await _start(app, agent)
        if why is not None:
            return None, why, None
    history = agent.history[-HISTORY_LINES:] if agent.new else None
    said = message(text, aloud, history, unspoken=dropped and not agent.new)
    agent.new = False
    outcome = FAILED
    try:
        reply = await asyncio.wait_for(_ask(agent.proc, said), timeout=TURN_TIMEOUT_S)
    except TimeoutError:
        reply, outcome = None, TIMED_OUT
    except (OSError, ConnectionError):
        reply = None
    if reply is None:
        # Its answer may still come; a fresh process is told the conversation instead.
        await _close(agent)
        return None, outcome, None
    return parse(reply, text, question), ANSWERED, reply


def log_path(key: str) -> Path:
    return LOG_DIR.expanduser() / f"{quote(key, safe='')}.jsonl"


def _save(key: str, record: dict) -> None:
    """Add one phrase to chat ``key``'s saved voice conversation; never raises."""
    path = log_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
        if path.stat().st_size > MAX_LOG_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            kept = path.with_suffix(".tmp")
            kept.write_text("".join(lines[len(lines) // 2 :]), encoding="utf-8")
            kept.replace(path)
    except OSError as exc:
        log.warning("voice log not saved for %s: %s", key, exc)


async def sent(key: str, phrase_id: str, text: str | None, how: str) -> None:
    """The page's report of what it really sent for phrase ``phrase_id``."""
    record = {"at": time.time(), "sent_for": phrase_id, "sent": text, "how": how}
    await asyncio.to_thread(_save, key, record)


async def timed(key: str, phrase_id: str, timing: dict) -> None:
    """The page's measured waits for phrase ``phrase_id`` (#3765): ms from the end of
    speech to its words, its send and the first voice heard. Unknown keys go."""
    kept = {
        name: round(value)
        for name, value in timing.items()
        if name in TIMINGS
        and isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_TIMING_MS
    }
    if kept:
        record = {"at": time.time(), "timing_for": phrase_id, "timing": kept}
        await asyncio.to_thread(_save, key, record)


def saved(keys) -> list[dict]:
    """Every saved phrase of the chats ``keys``, newest first, each with the page's
    report of what it sent as ``report`` (None when the page never said) and its
    measured waits as ``timing`` (None when not measured)."""
    records, reports, timings = [], {}, {}
    for key in keys:
        try:
            lines = log_path(key).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("at"), (int, float)):
                continue
            if record.get("sent_for"):
                reports[record["sent_for"]] = record
            elif record.get("timing_for"):
                timings[record["timing_for"]] = record.get("timing")
            elif "heard" in record:
                records.append(record)
    for record in records:
        record["report"] = reports.get(record.get("id")) if record.get("id") else None
        record["timing"] = timings.get(record.get("id")) if record.get("id") else None
    return sorted(records, key=lambda record: record["at"], reverse=True)


def has_saved(keys) -> bool:
    return any(log_path(key).is_file() for key in keys)


async def end(key: str, run: str = "") -> None:
    """Voice mode turned off in chat ``key``: its fast agent and memory go."""
    if run:
        _ended_runs[run] = None
        while len(_ended_runs) > MAX_ENDED_RUNS:
            _ended_runs.pop(next(iter(_ended_runs)))
    agent = _agents.get(key)
    if agent is not None:
        await _forget(agent)
