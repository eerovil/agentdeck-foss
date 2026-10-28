"""Voice mode's instruction line (#3401).

While a chat's voice mode is on, ``static/voice_mode.js`` adds this line to the end of
every message it sends, so the agent opens its reply with a paragraph the browser reads
aloud. The agent receives it as written; the chat view shows a 🎧 mark instead.

Before long work the agent first sends a short note starting "🔊", which the browser
reads while the agent works (#3407).

When the host's speech service heard the person speak Finnish (#3409), the page sends
``VOICE_LINE_FI`` instead, which also asks for the answer in Finnish, so the reply can
be read with the Finnish voice. The page lets the words override the host's call, and
reads each reply in the language its words are in (#3417).

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
"""

from __future__ import annotations

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
)
VOICE_LINE_FI = VOICE_LINE[:-2] + ". Answer in Finnish.)"
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


def hide_voice_line(text: str) -> str:
    """``text`` with a trailing instruction line, and the unheard and short-answer
    lines before it, replaced by the 🎧 mark; a short-answer line sent with no
    instruction line after it (🎧 off, #3540) is the trailing line then."""
    stripped = text.rstrip()
    for line in (VOICE_LINE, VOICE_LINE_FI, *OLD_VOICE_LINES, SHORT_ANSWER_LINE):
        if stripped.endswith(line):
            head = stripped[: -len(line)].rstrip()
            if head.endswith(SHORT_ANSWER_LINE):
                head = head[: -len(SHORT_ANSWER_LINE)].rstrip()
            start = head.rfind(_UNHEARD_HEAD)
            if start >= 0 and head.endswith((_UNHEARD_TAIL, *_OLD_UNHEARD_TAILS)):
                head = head[:start].rstrip()
            return f"{head} {VOICE_MARK}" if head else VOICE_MARK
    return text
