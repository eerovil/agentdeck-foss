"""Whether the owner's own message asks to merge one Work (#4643).

The deck chat says which Work it took the owner to mean; the deck checks that against the
owner's words, so a pasted text, a notice or the chat's own idea presses nothing. The check
is a short list of words that may stand in such a message: anything else in it (a question, a
"not", an "after", another number, a quote) is not a plain request, and the chat asks again.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .voice_mode import without_voice_line

_VERBS = frozenset(
    "merge mergeä mergeää mergaa mergaas mergee mergetä mergata yhdistä"
    # The button said as one word, and how the speech recognition hears it.
    " mergenappi mergenappia mergenapin merkinappi merkinappia merkinapin".split()
)
_FILLERS = frozenset(
    "ok okay yes yeah joo kyllä hei hey please now nyt go ahead and the it that this work issue"
    " pull request pr number numero työ työn työtä se sen tuo toi hit press push paina"
    " painaisitko button nappi nappia napin".split()
)
# Not a plain spoken request: a question, a quote, code, a pasted block or a second line.
_NOT_PLAIN = re.compile(r"[?\"“”„»«`<>\n\r]")
_NUMBER = re.compile(r"#?\b(\d+)(?::?(?:n|ta|tä))?\b")
_REPO_ENDING = r"(?:i?n|i?a|i?ssa|i?sta)?"


def _heard(name: str) -> str:
    # Letter by letter: speech recognition writes "agentdeck" as two words too.
    return r"[\W_]*".join(re.escape(letter) for letter in name if letter.isalnum())


def asks(message: str, repository: str, number: int, *, others: Iterable[str] = ()) -> bool:
    """Whether ``message`` is the owner's plain request to merge ``repository#number``.

    ``others`` are the other repositories with a Work of that number: then the message must
    name the project, and its owner too when another has the same name.
    """
    text = without_voice_line(message).strip().casefold()
    if _NOT_PLAIN.search(text):
        return False
    owner, _, name = repository.casefold().partition("/")
    project = re.compile(
        rf"(?<!\w)(?P<owner>{_heard(owner)}[\W_]*)?{_heard(name)}{_REPO_ENDING}(?!\w)"
    )
    found = project.search(text)
    text = project.sub(" ", text)
    rivals = [other.casefold().partition("/")[2] for other in others]
    if rivals and (found is None or (name in rivals and not found["owner"])):
        return False
    numbers = _NUMBER.findall(text)
    if not numbers or any(int(each) != number for each in numbers):
        return False
    words = re.findall(r"\w+", _NUMBER.sub(" ", text))
    return any(word in _VERBS for word in words) and all(
        word in _VERBS or word in _FILLERS for word in words
    )
