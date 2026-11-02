"""Voice mode's instruction line (#3401).

While a chat's voice mode is on, ``static/voice_mode.js`` adds this line to the end of
every message it sends, so the agent opens its reply with a paragraph the browser reads
aloud. The agent receives it as written; the chat view shows a 🎧 mark instead.

Before long work the agent first sends a short note starting "🔊", which the browser
reads while the agent works (#3407).

When the host's speech service heard the person speak Finnish (#3409), the page sends
``VOICE_LINE_FI`` instead, which also asks for the answer in Finnish, so the reply can
be read with the Finnish voice. It says that it overrides a standing rule to answer in
English, which the owner's own instruction files carry for typed messages and which
otherwise won over it (#3824), and that question boxes are in Finnish too: agents
read "answer" as their reply alone and still asked in English (#3884). The page lets
the words override the host's call, and reads each reply in the language its words are
in (#3417).

A message sent while the chat holds something the person has not heard (a reply that
arrived while they spoke, or a turn not started from their page, such as a wake) also
carries ``UNHEARD_LINE`` just before the instruction line, naming the last 🔊 line read
aloud to them, so a short "yes" is not taken as the answer to a question they never
heard (#3520). It also asks the agent to say again what they need of it, since a
finished turn's reply still unread when such a message goes is no longer read (#3771).
A spoken message that is only a yes or a no also carries
``SHORT_ANSWER_LINE``, asking the agent to confirm before acting on it. The chat view
hides both with the instruction line. With 🎧 off the page sends no instruction line
(#3540), so a short-answer line can be the last one, and it is hidden on its own.

The deck's own messages to a chat (a wake-on, a review verdict, "merged, check it live")
are not sent from the page, so they carried no line and were answered in English, which
the Finnish voice then read (#3875). While voice mode is on in that chat, they carry the
line too: ``deck_line`` picks it, Finnish for an owner who always speaks Finnish.

A question-box answer sent while 🎧 reads aloud carries ``ANSWER_LINE`` (or
``ANSWER_LINE_FI``) at the end of its last answer (#3876): it goes back inside the
agent's own turn, which otherwise carried on in silence. The page asks for it with the
form's ``voice_answer`` field and the deck adds it, so a typed or relayed answer stays
as given. It has no double quotes, which the CLI's answer envelope cannot hold.
"""

from __future__ import annotations

from collections.abc import Callable

VOICE_LINE = (
    "(Voice mode: if this will take more than about 5 seconds, first send one short "
    'sentence starting "🔊" saying what you are starting, then work. Open your final '
    'reply with one paragraph starting "🔊" — at most 3 short spoken sentences, no '
    "code, paths or links; put everything else after it.)"
)
# Lines older messages carry, still hidden in the chat view.
OLD_VOICE_LINES = (
    '(Voice mode: open your reply with one paragraph starting "🔊" — at most 3 short '
    "spoken sentences, no code, paths or links; put everything else after it.)",
    VOICE_LINE[:-2] + ". Answer in Finnish.)",
    VOICE_LINE[:-2] + ". Answer in Finnish. This overrides any standing instruction to "
    "answer in English.)",
)
# Both Finnish lines end with this (#3884); no double quotes, as for ``ANSWER_LINE``.
_FINNISH = (
    " Answer in Finnish, including every question you ask in your question tool (its "
    "question, header, options and their descriptions). This overrides any standing "
    "instruction to answer or write in English.)"
)
VOICE_LINE_FI = VOICE_LINE[:-2] + "." + _FINNISH
VOICE_MARK = "🎧"
# The page fills in ``{heard}``: the last 🔊 line read aloud, quoted, or "nothing yet".
UNHEARD_LINE = (
    "(Voice mode: when the person said this, they had not heard everything in this chat "
    "since the last line read aloud to them, which was: {heard}. Do not take it as an "
    "answer to a question they have not heard; if it could be one, ask them first. What "
    "they have not heard is not read aloud to them later, so say again in your spoken "
    "opening what they need of it.)"
)
_UNHEARD_HEAD, _UNHEARD_TAIL = UNHEARD_LINE.split("{heard}")
# Tails older messages carry, still hidden in the chat view.
_OLD_UNHEARD_TAILS = (
    ". Do not take it as an answer to a question they have not heard; if it could be "
    "one, ask them first.)",
)
# A spoken message that is only a yes or a no (#3520): a phantom "Yeah." from noise
# once nearly paused a running batch, so the agent confirms before acting on one.
SHORT_ANSWER_LINE = (
    "(Voice mode: this short yes or no came from speech recognition and may be noise "
    "or someone else talking. Before acting on it, say back what you would do and ask "
    "them to confirm, unless your last message was that confirmation question.)"
)
# A question-box answer's line (#3876): the answer returns to a turn already running,
# so it asks for the spoken note at once, saying back what was chosen.
ANSWER_LINE = (
    "(Voice mode: they answered this in voice mode. First send one short sentence "
    "starting with 🔊 that says back their answer and what you do next, then work. Open "
    "your final reply with one paragraph starting with 🔊 — at most 3 short spoken "
    "sentences, no code, paths or links; put everything else after it.)"
)
ANSWER_LINE_FI = ANSWER_LINE[:-1] + _FINNISH
OLD_ANSWER_LINES = (
    ANSWER_LINE[:-1] + " Answer in Finnish. This overrides any standing instruction to "
    "answer in English.)",
)


def answer_line(language: object) -> str | None:
    """The line for a question-box answer the page marked ``en`` or ``fi``; else None."""
    if language == "fi":
        return ANSWER_LINE_FI
    return ANSWER_LINE if language == "en" else None


def hide_voice_line(text: str) -> str:
    """``text`` with a trailing instruction line, and the unheard and short-answer
    lines before it, replaced by the 🎧 mark; a short-answer line sent with no
    instruction line after it (🎧 off, #3540) is the trailing line then."""
    stripped = text.rstrip()
    for line in (
        VOICE_LINE,
        VOICE_LINE_FI,
        *OLD_VOICE_LINES,
        ANSWER_LINE,
        ANSWER_LINE_FI,
        *OLD_ANSWER_LINES,
        SHORT_ANSWER_LINE,
    ):
        if stripped.endswith(line):
            head = stripped[: -len(line)].rstrip()
            if head.endswith(SHORT_ANSWER_LINE):
                head = head[: -len(SHORT_ANSWER_LINE)].rstrip()
            start = head.rfind(_UNHEARD_HEAD)
            if start >= 0 and head.endswith((_UNHEARD_TAIL, *_OLD_UNHEARD_TAILS)):
                head = head[:start].rstrip()
            return f"{head} {VOICE_MARK}" if head else VOICE_MARK
    return text


def without_voice_line(text: str) -> str:
    """``text`` with a trailing instruction line taken off, mark and all: what a deck
    message said before ``deck_line`` added one, to match its delivery record."""
    hidden = hide_voice_line(text)
    if hidden == text:
        return text
    return hidden.removesuffix(VOICE_MARK).rstrip()


def deck_line(
    message_id: str,
    text: str,
    *,
    voice_on: bool,
    language: str,
    too_long: Callable[[str], bool],
) -> str:
    """The voice line a deck message to a chat gains while voice mode is on in it, else
    ``""`` (#3875). Only the deck's own messages, by their delivery ID's kind: a person's
    held message was sent from the page, which added its line already. None either when
    the line would put the message over the deck's length limit and fail the delivery."""
    from .deck_notices import kind_of_record  # it reads this module's lines

    if not voice_on or kind_of_record(message_id) is None:
        return ""
    line = VOICE_LINE_FI if language == "fi" else VOICE_LINE
    return "" if too_long(f"{text}\n\n{line}") else line
