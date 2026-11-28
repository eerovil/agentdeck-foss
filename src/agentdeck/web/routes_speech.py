"""Voice mode's speech routes (#3409) and its fast agent's routes (#3444).

The page posts a recorded phrase here and gets its words back, and posts the text it
will read and gets the audio back. Both are passed on to the host's speech service
(``scripts/speech_server.py``) over its Unix socket, ``[speech] socket``; audio never
leaves the host. A 503 means the service is down or restarting: after a few retries
voice mode turns off (#3470), and 🎤 dictation keeps the phrase's audio for a Retry
and falls back to the browser's recognizer (#3722).

While voice mode waits for its wake word, the page posts each phrase to
``/api/speech/wake`` first, passed on to the wake word detector
(``scripts/wake_server.py``) over ``[speech] wake_socket`` (#3476), and reports what
Whisper made of a waiting phrase to ``/api/speech/wake-log``. Both write a line to
the wake log, joined by the page's phrase id, which ``scripts/wake_report.py`` reads
to compare the two checks; a phrase either check woke on, or the detector nearly
did, keeps its audio there for 30 days. A kiosk whose sound starts late has ``carry``
lines there (#4684).

With ``[speech] keep_audio_days`` set, every transcribed phrase also keeps its
audio, words and the service's noise reading in ``keep_audio_dir`` for that many
days, which ``scripts/speech_bench.py`` runs other speech-to-text models on, quiet
and noisy apart (#3499, back in #3934). A voice-mode probe (``source=probe``, the
phrase so far, sent again whole once it ends) is not kept. The phrase is kept after
the reply goes back (#4540). A voice-mode phrase whose early pass's words were
reused (no new speech past its clip) is kept in ``reused.jsonl`` instead, naming
the early pass's line; one heard whole after its early pass ``replaces`` that line.

``/api/voice/heard`` gives a phrase heard in voice mode to the chat's fast agent
(``voice_helper``) and returns what to say and what to send; a 503 there means the
page sends the words as heard and says its own acknowledgement.
The page asks it as soon as a phrase's words come back (#3765), and names an ask whose
answer it threw away, as more words joined the phrase, in the next one's ``drop``.
The short "Mm." the page played while the fast agent answered (#3765) and its
``/api/voice/filler`` route were removed by the owner's decision on #3780.
In any chat but the deck chat its answer can instead be the ask to go back there (#3816):
the reply then names the deck chat's ``jump`` and the page opens it, sending nothing.
``/api/voice/warm`` starts that agent before a phrase needs it (#3507), has the
speech service load its speech-to-text model (#3766).

``/api/speech/say`` reads with Piper in the speech service. Chatterbox, tried as a
second voice in #3519, was removed in #3796: the owner kept Piper everywhere.
``/api/voice/narrate`` (#3799) is asked when the chat has said nothing aloud for a while
after a send, or during a long quiet spell of work: the fast agent reads what the
agent did since the person last spoke, from the transcript, and returns one sentence
saying what it is doing, or 204 for nothing to say.
``/sessions/{key}/voice-log`` reads back what the fast agent heard and said (#3475).

While it waits, a page whose detector decides also streams its microphone to
``/api/speech/wake-stream`` (a WebSocket, #4539), passed on to the detector's
``/stream``, so the beep comes on the 80 ms frame the name ends in rather than after
the page's 0.8 s pause. It answers each phrase once with ``done``; a phrase that got
no ``done`` is sent as a clip to ``/api/speech/wake`` as before, under the same id,
and the wake log keeps one line per id whichever answered.

``GET /api/speech/status`` says whether the service has its model loaded yet: 🎤 asks
when a phrase is slow to come back, so its bubble can say the service is starting
(#3722).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import threading
import time
import uuid
import wave
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from socket import SO_SNDBUF, SOL_SOCKET
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import meta_chat, voice_helper
from .deps import get_config, get_db, get_state, get_templates, require_access, resolve_current_leg
from .routes_actions import _require_same_origin

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_access)])

# Thirty seconds of 16 kHz 16-bit mono is under 1 MB; the page cuts a phrase there.
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_SAY_CHARS = 2000
# The first phrase after an idle spell starts the service and loads the model: on
# 2026-10-28, with the CPU busy, that took 63 s and a 60 s wait lost the phrase (#3722).
TIMEOUT = httpx.Timeout(180.0, connect=5.0)
LANGUAGES = ("en", "fi")
# A status read answers while a phrase loads the model; a cold start is ~1 s of it.
STATUS_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# The wake log (#3476): the page names each waiting phrase with an id like this.
PHRASE_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
WAKE_LOG = "wake-log.jsonl"
# A detector score over this keeps the phrase's audio even when it did not wake.
NEAR_MISS = 0.2
KEEP_AUDIO_S = 30 * 86400
# Whisper's answer on a waiting phrase arrives after the detector's: the last few
# phrases' audio waits here, so one only Whisper woke on is kept too.
RECENT_PHRASES = 20
_recent_audio: OrderedDict[str, bytes] = OrderedDict()
# Phrase ids the wake log has a detector line for (#4539): a phrase the stream
# answered and the page also sent as a clip (its ``done`` came after the page gave
# up waiting) is logged once, by whichever answered first. Both run in the one event
# loop, so checking and adding is one step.
LOGGED_IDS = 500
_logged_ids: OrderedDict[str, None] = OrderedDict()
MAX_ID_CHARS = 40
# A chat's session key, as the page names the side chat a phrase went to (#4360).
MAX_KEY_CHARS = 200
# Phrases kept for comparing speech-to-text models (#3499, #3934), and where each
# came from: 🎧 voice mode or 🎤 dictation.
KEPT_LOG = "kept.jsonl"
# Whole phrases whose early pass's words were used, as the speech service found no
# new speech past its clip (#4540): kept apart, each naming its early pass's
# ``kept.jsonl`` id, so a review can hear them whole and compare.
REUSED_LOG = "reused.jsonl"
# The page cuts a phrase at 28 s: an early clip length past this is not one.
MAX_EARLY_S = 60.0
# The longest microphone name a kept or wake log line carries (#4249).
MIC_CHARS = 80
SOURCES = ("voice", "dictation")
ENGINES = ("whisper", "parakeet")
# The wake words a page may ask the detector for (``scripts/wake_server.py``); the
# first is every page's own.
WAKE_WORDS = ("jarvis", "alexa")
# Wake words whose phrase is never heard as words when speech recognition does not
# confirm them (#4788): Whisper wrote "Hey Jarvis" said alone as "Harvest." or "Hate
# armies!" and sent that, so the page listens for the next phrase instead. An
# unconfirmed "Alexa" (`wake_confirm = []`) is still heard whole, as before.
NAME_ONLY_WORDS = ("jarvis",)
# How speech recognition writes each wake word, as ``voice_mode.js``'s ``WAKE_WORDS``
# do: a detector wake on a word in ``[speech] wake_confirm`` counts only when the
# words heard in its clip match (#4372).
WAKE_SPOKEN = {
    "jarvis": re.compile(
        r"\b(?:hey|hei|hay|hi|hai|a)[\s,.!-]*(?:jarvis|jarvi|javis|jarves|jarvus)\b", re.I
    ),
    "alexa": re.compile(r"\b(?:alexa|aleksa|alexia|alexis|alekza|elexa)\b", re.I),
}
# The name speech recognition is told to expect as it confirms a wake.
WAKE_HINT = {"alexa": "Alexa"}
_kept_lock = threading.Lock()
# The speech service's warm-ups (#3766), held so none is collected before it ends;
# nothing awaits them.
_stt_warming: set[asyncio.Task] = set()
# Chats whose Claude process voice mode is asking the runtime to start (#4360), one
# ask at a time each, and the tasks asking.
_waking: set[str] = set()
_wakes: set[asyncio.Task] = set()


async def _forward(
    request: Request, path: str, *, socket: str | None = None, **kwargs
) -> httpx.Response:
    if socket is None:
        socket = get_config(request).speech.socket
    if not socket:
        raise HTTPException(status_code=503, detail="no speech service configured")
    transport = httpx.AsyncHTTPTransport(uds=str(Path(socket).expanduser()))
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT) as client:
            reply = await client.post(f"http://speech{path}", **kwargs)
    except httpx.HTTPError as exc:
        log.warning("speech service unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="speech service unreachable") from exc
    if reply.status_code != 200:
        log.warning("speech service %s answered %s: %s", path, reply.status_code, reply.text[:200])
        raise HTTPException(status_code=503, detail="speech service failed")
    return reply


async def _audio(request: Request) -> bytes:
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="no audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="phrase too long")
    return audio


@router.get("/api/speech/status")
async def status(request: Request) -> JSONResponse:
    """``{"loaded"}``: false while the service is still loading its model (#3722)."""
    socket = get_config(request).speech.socket
    if not socket:
        raise HTTPException(status_code=503, detail="no speech service configured")
    transport = httpx.AsyncHTTPTransport(uds=str(Path(socket).expanduser()))
    try:
        async with httpx.AsyncClient(transport=transport, timeout=STATUS_TIMEOUT) as client:
            reply = await client.get("http://speech/status")
        loaded = bool(reply.json().get("loaded")) if reply.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        log.info("speech status unreadable: %r", exc)
        loaded = None
    if loaded is None:
        raise HTTPException(status_code=503, detail="speech service status unknown")
    return JSONResponse({"loaded": loaded})


@router.post("/api/speech/transcribe")
async def transcribe(request: Request) -> JSONResponse:
    """A recorded phrase (16-bit WAV) as ``{"text", "language", "speech_s", "ms", "segments"}``:
    ``segments`` are the text's parts in order, each ``{"text", "unsure"}``, and voice
    mode marks the ones Whisper doubted for the agent (#3817)."""
    _require_same_origin(request)
    audio = await _audio(request)
    source = request.query_params.get("source", "")
    later: list = []
    heard = await _hear(request, audio, source, later=later)
    # The words go back first; the phrase is kept for the bench after the reply is
    # sent, off the phrase's own time (#4540).
    return JSONResponse(heard, background=BackgroundTask(_run_later, later))


@router.post("/api/speech/talk")
async def talk(request: Request) -> JSONResponse:
    """Whether Silero hears speech in a clip, as ``{"speech"}``, with no words made of it:
    voice mode ends a phrase that loud noise holds open (#4645). Nothing is kept."""
    _require_same_origin(request)
    audio = await _audio(request)
    reply = await _forward(request, "/talk", content=audio, headers={"Content-Type": "audio/wav"})
    return JSONResponse({"speech": reply.json().get("speech") is not False})


async def _run_later(saves: list) -> None:
    for save in saves:
        await save


def _early_s(value: object) -> float | None:
    """``?early_s=``: the length of the clip the page's early pass heard (#4540), or
    None when absent or no length a phrase can have."""
    try:
        seconds = float(str(value))
    except ValueError:
        return None
    return round(seconds, 3) if 0 < seconds <= MAX_EARLY_S else None


async def _hear(
    request: Request,
    audio: bytes,
    source: str | None,
    language: str | None = None,
    *,
    later: list | None = None,
    hint: str = "",
) -> dict:
    """The host's words for ``audio``, as ``/api/speech/transcribe`` answers them, and
    the phrase kept for the bench as ``source`` (#3934); ``None`` keeps nothing. With
    ``later`` the keeping is added there to run after the reply, else it is awaited.
    ``language`` is the language heard last when the request's query does not carry
    it (a streamed phrase names its own, #4539).

    ``?early_s=`` and ``?early=`` name the page's early pass of this phrase, its clip's
    length and id (#4540): with no new speech past that clip the reply is
    ``{"reuse": true}`` with no words, and the page takes the early pass's. Heard
    whole, the phrase's kept line ``replaces`` the early pass's, as it does with
    ``?replaces=`` (a whole phrase sent again after its early pass failed), so the
    bench keeps one line a phrase."""
    pinned = get_config(request).speech.language
    # The language heard last: a phrase too short to tell keeps it.
    previous = language or request.query_params.get("language")
    if pinned in LANGUAGES:
        path = f"/transcribe?only={pinned}"
    elif previous in LANGUAGES:
        path = f"/transcribe?language={previous}"
    else:
        path = "/transcribe"
    if hint:
        path += ("&" if "?" in path else "?") + f"hint={hint}"
    early_s = _early_s(request.query_params.get("early_s"))
    if early_s is not None:
        path += ("&" if "?" in path else "?") + f"early_s={early_s}"
    started = time.monotonic()
    reply = await _forward(request, path, content=audio, headers={"Content-Type": "audio/wav"})
    took = time.monotonic() - started
    heard = reply.json()
    language = pinned if pinned in LANGUAGES else heard.get("language")
    reuse = early_s is not None and heard.get("reuse") is True
    if source is not None:
        if reuse:
            save = _keep_reused(request, audio, early_s, took, source)
        else:
            save = _keep_phrase(request, audio, heard, pinned, took, source)
        if later is None:
            await save
        else:
            later.append(save)
    if reuse:
        return {
            "reuse": True,
            "text": "",
            "language": language if language in LANGUAGES else "en",
            "speech_s": None,
            "ms": round(took * 1000),
            "segments": [],
        }
    parts = heard.get("segments") if isinstance(heard.get("segments"), list) else []
    speech_s = heard.get("speech_s")
    return {
        "text": str(heard.get("text") or ""),
        "language": language if language in LANGUAGES else "en",
        # Seconds of speech in it: voice mode drops a fragment of junk (#3934).
        "speech_s": speech_s if type(speech_s) in (int, float) else None,
        # How long the speech service took over it, for voice mode's timings (#4360).
        "ms": round(took * 1000),
        "segments": [
            {"text": part["text"], "unsure": part.get("unsure") is True}
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ],
    }


async def _keep_phrase(
    request: Request, audio: bytes, heard: dict, pinned: str, took_s: float, source: str
) -> None:
    """Keep the phrase for the bench when ``[speech] keep_audio_days`` asks; a write
    that fails is logged and never costs the phrase its words."""
    speech = get_config(request).speech
    if speech.keep_audio_days <= 0 or source == "probe":
        return
    snr = heard.get("snr_db")
    line = {
        "id": _phrase_id(request.query_params.get("id")) or uuid.uuid4().hex[:12],
        "source": source if source in SOURCES else "",
        "engine": heard.get("engine") if heard.get("engine") in ENGINES else "",
        "text": str(heard.get("text") or ""),
        "language": heard.get("language"),
        "only": pinned if pinned in LANGUAGES else "",
        "snr_db": snr if isinstance(snr, (int, float)) and not isinstance(snr, bool) else None,
        "seconds": _seconds(audio),
        "ms": round(took_s * 1000),
        "mic": _mic(request),
    }
    # Heard whole after an early pass of the same phrase (#4540): the bench leaves the
    # early pass's line out, whichever of the two lands first.
    replaces = _phrase_id(request.query_params.get("replaces")) or _phrase_id(
        request.query_params.get("early")
    )
    if replaces and replaces != line["id"]:
        line["replaces"] = replaces
    try:
        await asyncio.to_thread(
            _write_kept,
            Path(speech.keep_audio_dir).expanduser(),
            line,
            audio,
            speech.keep_audio_days * 86400,
        )
    except OSError:
        log.exception("could not keep a phrase in %s", speech.keep_audio_dir)


async def _keep_reused(
    request: Request, audio: bytes, early_s: float, took_s: float, source: str
) -> None:
    """A whole phrase whose early pass's words were used (#4540), kept in
    ``reused.jsonl`` beside the early pass's ``kept.jsonl`` id when ``[speech]
    keep_audio_days`` asks; a write that fails is logged and costs nothing."""
    speech = get_config(request).speech
    if speech.keep_audio_days <= 0 or source == "probe":
        return
    line = {
        "id": uuid.uuid4().hex[:12],
        "early": _phrase_id(request.query_params.get("early")),
        "early_s": early_s,
        "source": source if source in SOURCES else "",
        "seconds": _seconds(audio),
        "ms": round(took_s * 1000),
        "mic": _mic(request),
    }
    try:
        await asyncio.to_thread(
            _write_kept,
            Path(speech.keep_audio_dir).expanduser(),
            line,
            audio,
            speech.keep_audio_days * 86400,
            REUSED_LOG,
        )
    except OSError:
        log.exception("could not keep a reused phrase in %s", speech.keep_audio_dir)


def _write_kept(
    folder: Path, line: dict, audio: bytes, keep_s: float, name: str = KEPT_LOG
) -> None:
    """One kept phrase, its old lines' expiry and its own line as one step: phrases
    heard at once each run in their own thread, and an expiry rewriting the log must
    not lose a line another phrase appends meanwhile (#3934 review)."""
    with _kept_lock:
        _write_log(folder, name, line, audio, keep_s, expire_lines=True)


def _seconds(audio: bytes) -> float:
    try:
        with wave.open(io.BytesIO(audio)) as w:
            return round(w.getnframes() / w.getframerate(), 2)
    except (wave.Error, EOFError, ZeroDivisionError):
        return 0.0


def _write_wake_log(folder: Path, line: dict, audio: bytes | None) -> None:
    _write_log(folder, WAKE_LOG, line, audio, KEEP_AUDIO_S)


def _write_log(
    folder: Path,
    name: str,
    line: dict,
    audio: bytes | None,
    keep_s: float,
    expire_lines: bool = False,
) -> None:
    """One line of the log ``name``, and the phrase's audio beside it when given.

    Every line also drops audio older than ``keep_s``, so recordings expire
    whether or not anything new is kept. With ``expire_lines`` the log's own lines
    past ``keep_s`` go too, so kept phrases' words last no longer than their audio
    (#3934); the wake log keeps its lines."""
    folder.mkdir(parents=True, exist_ok=True)
    if audio is not None and line.get("id"):
        target = folder / f"{line['id']}.wav"
        if not target.exists():
            target.write_bytes(audio)
    cutoff = time.time() - keep_s
    for old in folder.glob("*.wav"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
    if expire_lines:
        _expire_lines(folder / name, cutoff)
    with (folder / name).open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps({"at": round(time.time(), 3), **line}) + "\n")


def _expire_lines(path: Path, cutoff: float) -> None:
    """``path`` without its lines from before ``cutoff``. Lines are written oldest
    first, so a first line still in time means nothing is rewritten."""
    try:
        with path.open(encoding="utf-8") as log_file:
            first = log_file.readline()
    except FileNotFoundError:
        return
    if not first or _line_at(first) >= cutoff:
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    keep = "".join(text for text in lines if _line_at(text) >= cutoff)
    spare = path.with_name(path.name + ".tmp")
    spare.write_text(keep, encoding="utf-8")
    spare.replace(path)


def _line_at(text: str) -> float:
    """A log line's time; a line that cannot be read counts as expired."""
    try:
        at = json.loads(text).get("at")
    except (ValueError, AttributeError):
        return 0.0
    return at if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0


def _mic(request: Request) -> str:
    """Which microphone heard the phrase, as the page names it (``?mic=``, #4249):
    the track's label, and ``call`` when it was opened in call mode for a headset.
    Printable text only, cut short; ``""`` when the page did not say."""
    value = request.query_params.get("mic", "")
    return "".join(ch for ch in value if ch.isprintable())[:MIC_CHARS]


def _phrase_id(value: object) -> str:
    text = str(value or "")
    return text if PHRASE_ID.fullmatch(text) else ""


@router.post("/api/speech/wake")
async def wake(request: Request) -> JSONResponse:
    """A phrase heard while waiting (16-bit WAV) as the wake word detector's
    ``{"woken", "score", "speech_after"}`` (#3476). ``?id=`` names the phrase in the
    wake log. ``?window=1`` marks the last moments of microphone sound sent every
    0.4 s while a reply is read (#3842): a few a second, so only one that woke or
    nearly did is logged. ``?word=alexa`` asks for that wake word instead of "Hey
    Jarvis", for a page whose voice control names it, at that word's own
    ``[speech] wake_thresholds`` score when one is set.

    A wake on a word in ``[speech] wake_confirm`` ("Alexa") also needs speech
    recognition to hear the word in the clip, or it is answered as not woken
    (#4372); when the phrase goes on after the name, the reply carries its words as
    ``heard``, so confirming costs the page no second wait."""
    _require_same_origin(request)
    word = request.query_params.get("word") or WAKE_WORDS[0]
    if word not in WAKE_WORDS:
        raise HTTPException(status_code=400, detail="unknown wake word")
    audio = await _audio(request)
    speech = get_config(request).speech
    if not speech.wake_socket:
        raise HTTPException(status_code=503, detail="no wake word detector configured")
    phrase = _phrase_id(request.query_params.get("id"))
    started = time.monotonic()
    reply = await _forward(
        request,
        f"/wake?threshold={speech.wake_thresholds.get(word, speech.wake_threshold)}"
        + ("" if word == WAKE_WORDS[0] else f"&word={word}"),
        socket=speech.wake_socket,
        content=audio,
        headers={"Content-Type": "audio/wav"},
    )
    found = reply.json()
    result = {
        "woken": bool(found.get("woken")),
        "score": float(found.get("score") or 0.0),
        "speech_after": bool(found.get("speech_after")),
    }
    if result["woken"] and word in NAME_ONLY_WORDS and word not in speech.wake_confirm:
        result["name_only"] = True
    window = request.query_params.get("window") == "1"
    confirm: dict = {}
    if result["woken"] and word in speech.wake_confirm:
        confirm, heard = await _confirm_wake(request, word, audio, result, window)
        if confirm["confirmed"] is False:
            result["woken"] = False
        elif heard is not None:
            # The words after the name are heard already: the page sends them on
            # rather than asking for them again.
            result["heard"] = heard
    if phrase:
        _recent_audio[phrase] = audio
        while len(_recent_audio) > RECENT_PHRASES:
            _recent_audio.popitem(last=False)
    keep = result["woken"] or result["score"] > NEAR_MISS
    if window and not keep:
        return JSONResponse(result)
    if phrase and not _first_line(phrase):
        # The stream answered this phrase already (#4539): the page still gets its
        # answer, but the phrase is logged once.
        return JSONResponse(result)
    line = {
        "id": phrase,
        "check": "window" if window else "detector",
        "mode": speech.wake,
        "word": word,
        "seconds": _seconds(audio),
        "ms": round((time.monotonic() - started) * 1000),
        "mic": _mic(request),
        **{key: value for key, value in result.items() if key != "heard"},
        **confirm,
    }
    await asyncio.to_thread(
        _write_wake_log, Path(speech.wake_log_dir).expanduser(), line, audio if keep else None
    )
    return JSONResponse(result)


def _first_line(phrase: str) -> bool:
    """Whether ``phrase`` has no detector line in the wake log yet, marking it."""
    if phrase in _logged_ids:
        return False
    _logged_ids[phrase] = None
    while len(_logged_ids) > LOGGED_IDS:
        _logged_ids.popitem(last=False)
    return True


async def _confirm_wake(
    request: Request,
    word: str,
    audio: bytes,
    result: dict,
    window: bool,
    language: str | None = None,
) -> tuple[dict, dict | None]:
    """Whether speech recognition hears ``word`` in a clip the detector woke on
    (#4372): the kitchen's "Alexa" woke on noise at 0.94, where Whisper heard no
    words at all. Speech recognition is told to expect the name (``WAKE_HINT``), as it
    wrote quiet real calls as other words, unless its words go on as the message.

    Returns the wake log's fields, ``{"confirmed", "heard", "confirm_ms"}``, and the
    words in ``/api/speech/transcribe``'s shape when the page would ask for them next
    (a phrase that goes on after the name), else ``None``. With no speech service, or
    one that fails, nothing heard the name, so the wake does not count (``confirmed``
    false, ``heard`` null): voice mode could not hear the words after it either."""
    started = time.monotonic()
    speech = get_config(request).speech
    goes_on = result["speech_after"] and not window
    try:
        if not speech.socket:
            raise HTTPException(status_code=503, detail="no speech service configured")
        # Whisper wrote quiet real calls the detector scored 1.0 as "All right." or
        # "Next up.". No hint when its words are sent on, which it could change.
        hint = "" if goes_on else WAKE_HINT.get(word, "")
        heard = await _hear(request, audio, "voice" if goes_on else None, language, hint=hint)
    except HTTPException:
        took = round((time.monotonic() - started) * 1000)
        return {"confirmed": False, "heard": None, "confirm_ms": took}, None
    text = heard["text"].strip()
    confirmed = bool(WAKE_SPOKEN[word].search(text))
    line = {
        "confirmed": confirmed,
        "heard": text[:MAX_SAY_CHARS],
        "confirm_ms": round((time.monotonic() - started) * 1000),
    }
    return line, heard if confirmed and goes_on else None


# ---- the waiting microphone, streamed (#4539) ---------------------------------

# The detector's ``/stream`` upgrade (``scripts/wake_server.py``).
STREAM_UPGRADE = "agentdeck-wake-stream"
# 16 kHz 16-bit mono, as the page sends it.
STREAM_BYTES_PER_S = 32000
# The longest audio record the detector takes (its ``MAX_RECORD``).
STREAM_RECORD_BYTES = 16000
# The page pings every 10 s; a connection silent this long is gone (a phone's Wi-Fi
# dropped, a screen went off) and frees its detector stream.
STREAM_IDLE_S = 25.0
# Audio the deck holds unsent to the detector before it calls the detector behind and
# hands the page back to clips.
STREAM_BEHIND_BYTES = 2 * STREAM_BYTES_PER_S
# A phrase's audio kept for its log line and its confirm: the page cuts at 28 s.
STREAM_PHRASE_BYTES = 30 * STREAM_BYTES_PER_S
# A confirm hears the last 2 s, after up to this much more of the phrase: "Alexa"
# crosses about 0.08 s before the word ends (#4524), so its end is not cut off.
CONFIRM_MORE_S = 0.25
CONFIRM_CLIP_BYTES = 2 * STREAM_BYTES_PER_S
# The detector loads a model for a new stream, or starts on its socket's first use.
DETECTOR_OPEN_TIMEOUT_S = 30.0
# Each page's open stream, by the id it made once on load: its reconnect replaces it.
_wake_streams: dict[str, WakeStream] = {}


class _Fallback(Exception):
    """The detector stream cannot go on; the page sends clips (the message says why)."""


def _pcm_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(STREAM_BYTES_PER_S // 2)
        w.writeframes(pcm[: len(pcm) // 2 * 2])
    return out.getvalue()


def _record(kind: bytes, body: bytes) -> bytes:
    return kind + len(body).to_bytes(4, "big") + body


class _StreamPhrase:
    def __init__(self, phrase: str, mic: str) -> None:
        self.id = phrase
        self.mic = mic
        self.audio = bytearray()
        self.ended = asyncio.Event()
        self.ended_at = 0.0
        self.crossed: dict | None = None
        self.detected: dict | None = None
        self.confirm: dict | None = None
        self.confirming = False
        # Told to beep before its confirm answered.
        self.sure = False
        self.finished = False
        # The language the page heard last when the phrase started, for a confirm.
        self.language: str | None = None


class WakeStream:
    """One page's waiting microphone, passed on to the detector (#4539).

    Three things run at once: the page's audio and phrase marks forwarded to the
    detector, the detector's events answered to the page, and a confirm per phrase
    being checked by speech recognition. The forwarding never waits for Whisper, so
    audio keeps flowing while a confirm runs. Every phrase the page ended gets one
    ``done`` once its verdict is final, and its one wake log line then; a phrase the
    page never ended gets neither, and the page sends it as a clip."""

    def __init__(self, websocket: WebSocket, word: str, page: str) -> None:
        self.websocket = websocket
        self.speech = get_config(websocket).speech
        self.word = word
        self.page = page
        self.confirms = word in self.speech.wake_confirm
        self.phrases: dict[str, _StreamPhrase] = {}
        self.current: _StreamPhrase | None = None
        self.tasks: set[asyncio.Task] = set()
        self.replaced = asyncio.Event()
        self.writer: asyncio.StreamWriter | None = None

    async def run(self) -> None:
        reason = ""
        try:
            reader, self.writer = await self._open()
            waits = {
                asyncio.create_task(self._from_page()),
                asyncio.create_task(self._from_detector(reader)),
                asyncio.create_task(self.replaced.wait()),
            }
            try:
                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits | self.tasks:
                    task.cancel()
            first = next(iter(done))
            if first.exception() is not None:
                log.warning("wake stream failed", exc_info=first.exception())
                reason = "stream failed"
            elif first.result() is True:
                reason = ""  # replaced by the same page's next connection
            else:
                reason = first.result()
        except _Fallback as exc:
            reason = str(exc)
        finally:
            if self.writer is not None:
                self.writer.close()
            if _wake_streams.get(self.page) is self:
                del _wake_streams[self.page]
        if reason:
            log.info("wake stream for %s falls back to clips: %s", self.word, reason)
            await self._send({"fallback": reason})
        try:
            if self.websocket.client_state != WebSocketState.DISCONNECTED:
                await self.websocket.close()
        except RuntimeError:
            pass

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        speech = self.speech
        if not speech.wake_socket or speech.wake != "detector":
            raise _Fallback("no detector")
        threshold = speech.wake_thresholds.get(self.word, speech.wake_threshold)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(Path(speech.wake_socket).expanduser())),
                DETECTOR_OPEN_TIMEOUT_S,
            )
        except (OSError, TimeoutError) as exc:
            raise _Fallback("detector unreachable") from exc
        # A small kernel buffer, so "behind" below is seen within about 3 s rather
        # than the ~6 s Linux's default would hold unseen.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(SOL_SOCKET, SO_SNDBUF, STREAM_RECORD_BYTES)
        # Only a word that is confirmed has a beep to give before its `woken`.
        sure = self.speech.wake_sure.get(self.word) if self.confirms else None
        query = f"threshold={threshold}&word={self.word}" + (f"&sure={sure}" if sure else "")
        writer.write(
            (
                f"POST /stream?{query} HTTP/1.1\r\n"
                f"Host: wake\r\nUpgrade: {STREAM_UPGRADE}\r\nConnection: Upgrade\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode()
        )
        try:
            status = await asyncio.wait_for(reader.readline(), DETECTOR_OPEN_TIMEOUT_S)
            while (await asyncio.wait_for(reader.readline(), DETECTOR_OPEN_TIMEOUT_S)) not in (
                b"\r\n",
                b"\n",
                b"",
            ):
                pass
        except (OSError, TimeoutError, ValueError) as exc:
            writer.close()
            raise _Fallback("detector did not answer") from exc
        code = status.split(b" ")[1:2]
        if code != [b"101"]:
            writer.close()
            raise _Fallback("detector busy" if code == [b"503"] else "detector refused")
        return reader, writer

    async def _from_page(self) -> str:
        while True:
            try:
                message = await asyncio.wait_for(self.websocket.receive(), STREAM_IDLE_S)
            except TimeoutError:
                return ""
            except (WebSocketDisconnect, RuntimeError):
                return ""
            if message["type"] == "websocket.disconnect":
                return ""
            if message.get("bytes") is not None:
                if not self._audio(message["bytes"]):
                    return "detector behind"
            elif message.get("text") is not None:
                self._mark(message["text"])

    def _audio(self, pcm: bytes) -> bool:
        phrase = self.current
        if phrase is None or phrase.ended.is_set() or not pcm:
            return True
        if len(phrase.audio) < STREAM_PHRASE_BYTES:
            phrase.audio += pcm
        # The detector takes at most half a second a record.
        for start in range(0, len(pcm), STREAM_RECORD_BYTES):
            self.writer.write(_record(b"A", pcm[start : start + STREAM_RECORD_BYTES]))
        return self.writer.transport.get_write_buffer_size() <= STREAM_BEHIND_BYTES

    def _mark(self, text: str) -> None:
        try:
            body = json.loads(text)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        if "phrase" in body:
            phrase = _phrase_id(body.get("phrase"))
            if not phrase or phrase in self.phrases:
                return
            # A phrase the page started over without ending is dropped unlogged: the
            # page sends it as a clip.
            if self.current is not None and not self.current.ended.is_set():
                self.phrases.pop(self.current.id, None)
            mic = "".join(ch for ch in str(body.get("mic") or "") if ch.isprintable())
            self.current = _StreamPhrase(phrase, mic[:MIC_CHARS])
            language = body.get("language")
            self.current.language = language if language in LANGUAGES else None
            self.phrases[phrase] = self.current
            self.writer.write(_record(b"P", phrase.encode()))
        elif "end" in body:
            phrase = self.current
            if phrase is None or phrase.id != body.get("end") or phrase.ended.is_set():
                return
            phrase.ended_at = time.monotonic()
            phrase.ended.set()
            self.writer.write(_record(b"E", phrase.id.encode()))

    async def _from_detector(self, reader: asyncio.StreamReader) -> str:
        while True:
            try:
                line = await reader.readline()
            except (OSError, ValueError):
                return "detector stream failed"
            if not line:
                return "detector stream ended"
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if "crossed" in event:
                await self._crossed(event)
            elif "sure" in event:
                await self._sure(event)
            elif "end" in event:
                phrase = self.phrases.get(str(event["end"]))
                if phrase is not None:
                    phrase.detected = event
                    await self._finish(phrase)

    async def _crossed(self, event: dict) -> None:
        phrase = self.phrases.get(str(event["crossed"]))
        if phrase is None or phrase.crossed is not None:
            return
        phrase.crossed = event
        if not self.confirms:
            # "Hey Jarvis" beeps at once; what follows in the phrase is the message.
            woke: dict = {"woken": phrase.id, "score": event.get("score")}
            if self.word in NAME_ONLY_WORDS:
                woke["name_only"] = True
                # Where it crossed: the page tells a sentence said straight after
                # the name from the name alone by it (#4804).
                if isinstance(event.get("at"), (int, float)):
                    woke["at"] = event["at"]
            await self._send(woke)
            return
        phrase.confirming = True
        await self._send({"confirming": phrase.id})
        task = asyncio.create_task(self._confirm(phrase))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _sure(self, event: dict) -> None:
        """The detector is sure enough to beep while speech recognition still checks
        the name (#4614). One already answered has beeped, or will not."""
        phrase = self.phrases.get(str(event["sure"]))
        if phrase is None or not phrase.confirming:
            return
        phrase.sure = True
        await self._send({"sure": phrase.id, "score": event.get("score")})

    async def _confirm(self, phrase: _StreamPhrase) -> None:
        """Whether Whisper hears the name in the phrase's last 2 s (#4372), beside
        the forwarding; cancelled with the connection, it sends and logs nothing."""
        try:
            await asyncio.wait_for(phrase.ended.wait(), CONFIRM_MORE_S)
        except TimeoutError:
            pass
        clip = _pcm_wav(bytes(phrase.audio[-CONFIRM_CLIP_BYTES:]))
        started = time.monotonic()
        try:
            fields, _ = await _confirm_wake(
                self.websocket,
                self.word,
                clip,
                {"speech_after": False},
                window=False,
                language=phrase.language,
            )
        except Exception:  # noqa: BLE001 - the phrase still gets its done
            log.exception("wake stream confirm failed")
            took = round((time.monotonic() - started) * 1000)
            fields = {"confirmed": False, "heard": None, "confirm_ms": took}
        phrase.confirm = fields
        phrase.confirming = False
        if fields["confirmed"]:
            await self._send({"woken": phrase.id, "score": phrase.crossed.get("score")})
        await self._finish(phrase)

    async def _finish(self, phrase: _StreamPhrase) -> None:
        """The phrase's one ``done`` and one log line, once both the detector's end and
        any confirm are in."""
        if phrase.finished or phrase.detected is None or phrase.confirming:
            return
        phrase.finished = True
        self.phrases.pop(phrase.id, None)
        woken = phrase.crossed is not None and (
            not self.confirms or bool(phrase.confirm and phrase.confirm["confirmed"])
        )
        score = float(phrase.detected.get("score") or 0.0)
        audio = _pcm_wav(bytes(phrase.audio))
        _recent_audio[phrase.id] = audio
        while len(_recent_audio) > RECENT_PHRASES:
            _recent_audio.popitem(last=False)
        # The line is written before ``done`` goes out: the page may close the
        # moment it reads it, and that cancels this task.
        if _first_line(phrase.id):
            await self._log(phrase, woken, score, audio)
        done: dict = {"done": phrase.id, "woken": woken, "score": score}
        if woken and not self.confirms and self.word in NAME_ONLY_WORDS:
            done["name_only"] = True
        await self._send(done)

    async def _log(self, phrase: _StreamPhrase, woken: bool, score: float, audio: bytes) -> None:
        assert phrase.detected is not None
        keep = woken or score > NEAR_MISS
        line = {
            "id": phrase.id,
            "check": "detector",
            "mode": self.speech.wake,
            "word": self.word,
            "seconds": round(len(phrase.audio) / STREAM_BYTES_PER_S, 2),
            # From the page's end of the phrase to the detector's last frame.
            "ms": round((time.monotonic() - phrase.ended_at) * 1000) if phrase.ended_at else None,
            "mic": phrase.mic,
            "woken": woken,
            "score": score,
            "speech_after": False,
            "stream": True,
            # Seconds into the phrase of the frame that crossed, or null.
            "crossed_at": phrase.detected.get("at"),
            # Beeped before its confirm answered.
            **({"sure": True} if phrase.sure else {}),
            **(phrase.confirm or {}),
        }
        await asyncio.to_thread(
            _write_wake_log,
            Path(self.speech.wake_log_dir).expanduser(),
            line,
            audio if keep else None,
        )

    async def _send(self, event: dict) -> None:
        try:
            await self.websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass


@router.websocket("/api/speech/wake-stream")
async def wake_stream(websocket: WebSocket) -> None:
    """The waiting microphone, streamed to the wake word detector (#4539).

    ``?page=`` the id the page made once on load; ``?word=alexa`` as for
    ``/api/speech/wake``; ``?language=`` the language last heard, for a confirm.
    The page sends text ``{"phrase": id, "mic"}``, then 16 kHz 16-bit mono audio as
    binary messages, then ``{"end": id}``, and ``{"ping": 1}`` every 10 s. It gets
    ``{"woken": id, "score"}`` the moment the phrase wakes ("Alexa" once speech
    recognition heard the name, after ``{"confirming": id}``, and ``{"sure": id}`` when
    the detector's score reached ``[speech] wake_sure``: beep, do not listen yet), one
    ``{"done": id, "woken", "score"}`` per ended phrase, and ``{"fallback": reason}`` before the
    deck closes a stream that cannot go on."""
    if not _same_origin_socket(websocket):
        await websocket.close(code=1008)
        return
    word = websocket.query_params.get("word") or WAKE_WORDS[0]
    page = _phrase_id(websocket.query_params.get("page"))
    if word not in WAKE_WORDS or not page:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    stream = WakeStream(websocket, word, page)
    older = _wake_streams.get(page)
    _wake_streams[page] = stream
    if older is not None:
        older.replaced.set()
    await stream.run()


def _same_origin_socket(websocket: WebSocket) -> bool:
    """As ``_require_same_origin`` for a WebSocket, whose scheme is ``ws``/``wss``:
    another site's page cannot open the microphone stream."""
    if websocket.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = websocket.headers.get("origin")
    return not origin or urlsplit(origin).netloc == websocket.headers.get("host")


@router.post("/api/speech/wake-log")
async def wake_log(request: Request) -> Response:
    """What Whisper made of a waiting phrase, for the wake log (#3476): ``{"id",
    "text", "matched", "ms", "detected"}``. ``matched`` is the page's own wake word
    check, ``ms`` the wait from the phrase's end to the words, ``detected`` that
    the detector woke on it first. ``{"waiting": "start"|"stop", "spell"}`` marks
    when a spell of waiting starts and stops instead, so the report counts the time
    really spent waiting. ``{"carry": "paused"|"started"|"tapped", "kiosk",
    "audio"}`` marks a kiosk page whose sound did not start after a load, and that it
    then went on by itself or by a tap (#4684);
    ``audio`` is the page's audio state."""
    _require_same_origin(request)
    body = await _json_object(request)
    speech = get_config(request).speech
    folder = Path(speech.wake_log_dir).expanduser()
    if "waiting" in body:
        spell = _phrase_id(body.get("spell"))
        if body.get("waiting") not in ("start", "stop") or not spell:
            raise HTTPException(status_code=400, detail="a waiting start or stop and its spell")
        line = {"check": "waiting", "state": body["waiting"], "spell": spell, "mode": speech.wake}
        await asyncio.to_thread(_write_wake_log, folder, line, None)
        return Response(status_code=204)
    if "carry" in body:
        if body.get("carry") not in ("paused", "started", "tapped"):
            raise HTTPException(status_code=400, detail="a carry paused, started or tapped")
        line = {
            "check": "carry",
            "state": body["carry"],
            "kiosk": str(body.get("kiosk") or "")[:MAX_ID_CHARS],
            "audio": str(body.get("audio") or "")[:MAX_ID_CHARS],
        }
        await asyncio.to_thread(_write_wake_log, folder, line, None)
        return Response(status_code=204)
    phrase = _phrase_id(body.get("id"))
    if not phrase:
        raise HTTPException(status_code=400, detail="no phrase id")
    matched = bool(body.get("matched"))
    ms = body.get("ms")
    line = {
        "id": phrase,
        "check": "whisper",
        "mode": speech.wake,
        "text": str(body.get("text") or "")[:MAX_SAY_CHARS],
        "matched": matched,
        "ms": round(ms) if isinstance(ms, int | float) else None,
        "detected": bool(body.get("detected")),
    }
    audio = _recent_audio.get(phrase) if matched else None
    await asyncio.to_thread(_write_wake_log, folder, line, audio)
    return Response(status_code=204)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="expected JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


@router.post("/api/speech/say")
async def say(request: Request) -> Response:
    """``{"text", "language"}`` read aloud, as WAV."""
    _require_same_origin(request)
    body = await _json_object(request)
    text = str(body.get("text") or "").strip()[:MAX_SAY_CHARS]
    if not text:
        raise HTTPException(status_code=400, detail="no text")
    language = body.get("language") if body.get("language") in LANGUAGES else "en"
    speech = get_config(request).speech
    voice = getattr(speech, f"voice_{language}")
    speed = getattr(speech, f"speed_{language}")
    reply = await _forward(
        request,
        "/say",
        json={"text": text, "language": language, "voice": voice, "speed": speed},
    )
    return Response(reply.content, media_type="audio/wav", headers={"Cache-Control": "no-store"})


async def _warm_speech(socket: str) -> None:
    """Have the speech service load its speech-to-text model; it answers at once
    and loads behind the reply. Run as a task nothing waits on (#3766)."""
    transport = httpx.AsyncHTTPTransport(uds=str(Path(socket).expanduser()))
    try:
        async with httpx.AsyncClient(transport=transport, timeout=STATUS_TIMEOUT) as client:
            await client.post("http://speech/warm")
    except httpx.HTTPError as exc:
        log.info("speech service warm-up failed: %r", exc)


def _background(warming: set[asyncio.Task], coroutine) -> None:
    task = asyncio.create_task(coroutine)
    warming.add(task)
    task.add_done_callback(warming.discard)


@router.post("/api/voice/heard")
async def voice_heard(request: Request) -> JSONResponse:
    """``{"session_key", "text", "aloud"}`` heard in voice mode, as the fast agent's
    ``{"say", "relay", "more"}``: what to say now, what to send the chat (null: nothing),
    and whether the words stop mid-thought, so the page waits for the rest (#3934)."""
    _require_same_origin(request)
    body = await _json_object(request)
    key = str(body.get("session_key") or "").strip()
    text = str(body.get("text") or "").strip()
    if not key or not text:
        raise HTTPException(status_code=400, detail="a session key and words are needed")
    aloud = body.get("aloud") if isinstance(body.get("aloud"), list) else []
    lines = [str(line) for line in aloud[-5:] if isinstance(line, str)]
    phrase_id = str(body.get("id") or "").strip()[:MAX_ID_CHARS]
    run = str(body.get("run") or "").strip()[:MAX_ID_CHARS]
    drop = str(body.get("drop") or "").strip()[:MAX_ID_CHARS]
    answer = await voice_helper.heard(request.app, key, text, lines, phrase_id, run, drop)
    if answer is None:
        raise HTTPException(status_code=503, detail="the fast agent has no answer")
    if answer.back:
        # The ask to go back to the deck chat (#3816): the page opens it in place.
        state = get_state(request)
        deck_key = meta_chat.deck_chat_session_key(state, get_db(request))
        if deck_key is None:
            return JSONResponse({"say": "", "relay": text})
        jump = {"path": state.chat_path_for_session(deck_key), "title": meta_chat.SPOKEN_TITLE}
        return JSONResponse({"say": "", "relay": None, "jump": jump})
    more = {"more": True} if answer.more else {}
    return JSONResponse({"say": answer.say, "relay": answer.relay, **more})


@router.post("/api/voice/narrate")
async def voice_narrate(request: Request) -> Response:
    """``{"session_key", "seconds", "run", "lang"}``: the chat has said nothing aloud for that
    long while it works, so the fast agent says what it is doing (#3799), as
    ``{"say"}``; 204 when it has nothing to say.

    ``transcript_key`` (#4349): the chat whose steps are told, when voice mode follows the
    chat's side chat. The fast agent is still ``session_key``'s; a side chat this deck
    cannot find has nothing to say."""
    _require_same_origin(request)
    body = await _json_object(request)
    key = str(body.get("session_key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="a session key is needed")
    try:
        seconds = max(0, min(3600, int(body.get("seconds") or 0)))
    except (TypeError, ValueError):
        seconds = 0
    run = str(body.get("run") or "").strip()[:MAX_ID_CHARS]
    narration_id = str(body.get("id") or "").strip()[:MAX_ID_CHARS]
    told = str(body.get("transcript_key") or "").strip() or key
    try:
        account, session, provider = resolve_current_leg(request, told)
    except HTTPException:
        if told == key:
            raise
        return Response(status_code=204)
    detail = await provider.load_transcript(account, session)
    done = voice_helper.steps(detail.events)
    finnish = body.get("lang") == "fi"
    said = await voice_helper.narrate(request.app, key, done, seconds, run, finnish, narration_id)
    if not said:
        return Response(status_code=204)
    return JSONResponse({"say": said})


@router.post("/api/voice/sent")
async def voice_sent(request: Request) -> Response:
    """``{"session_key", "id", "text", "how", "to"?}``: what the page really sent for a
    phrase (``text`` null: nothing, and ``how`` says why), saved in the voice log
    (#3475); ``to``, the side chat it went to (#4360).
    ``{"session_key", "id", "timing"}`` instead saves the phrase's measured waits
    (#3765), and ``{"session_key", "id", "spoken"}`` the fast agent's line the page
    really said for it, which it holds back until the chat stays quiet (#3799)."""
    _require_same_origin(request)
    body = await _json_object(request)
    key = str(body.get("session_key") or "").strip()
    phrase_id = str(body.get("id") or "").strip()[:MAX_ID_CHARS]
    if not key or not phrase_id:
        raise HTTPException(status_code=400, detail="a session key and a phrase id are needed")
    if isinstance(body.get("timing"), dict):
        host = await _host_waits(request, key, phrase_id)
        await voice_helper.timed(key, phrase_id, body["timing"], host)
        return Response(status_code=204)
    if isinstance(body.get("spoken"), str):
        await voice_helper.spoken(key, phrase_id, body["spoken"][:MAX_SAY_CHARS])
        return Response(status_code=204)
    text = body.get("text")
    text = str(text)[: voice_helper.MAX_HEARD_CHARS * 2] if isinstance(text, str) else None
    how = str(body.get("how") or "")[:MAX_SAY_CHARS]
    # The chat it went to, when voice mode follows the side chat (#4349, #4360).
    to = str(body.get("to") or "").strip()[:MAX_KEY_CHARS]
    await voice_helper.sent(key, phrase_id, text, how, to)
    return Response(status_code=204)


async def _host_waits(request: Request, key: str, phrase_id: str) -> dict:
    """The chat's own part of a timed phrase, from its transcript (#4360); nothing
    when the send was not reported or the chat cannot be read."""
    reports = await asyncio.to_thread(voice_helper.sent_reports, key, phrase_id)
    if not reports or not isinstance(reports[-1].get("sent"), str):
        return {}
    report = reports[-1]
    try:
        # The side chat's own transcript, for a phrase sent there (#4349).
        account, session, provider = resolve_current_leg(request, report.get("to") or key)
        detail = await provider.load_transcript(account, session)
    except Exception as exc:  # only measured: the page's own waits are saved anyway
        log.info("voice timing: no transcript for %s: %s", key, exc)
        return {}
    return voice_helper.host_waits(detail.events, reports, phrase_id)


@router.post("/api/voice/warm")
async def voice_warm(request: Request) -> Response:
    """Voice mode is listening: start the chat's fast agent and load the speech
    model before a phrase needs them (#3766)."""
    _require_same_origin(request)
    body = await _json_object(request)
    key = str(body.get("session_key") or "").strip()
    run = str(body.get("run") or "").strip()[:MAX_ID_CHARS]
    speech = get_config(request).speech
    if speech.socket:
        _background(_stt_warming, _warm_speech(speech.socket))
    if key:
        _wake_chat(request, key)
        await voice_helper.warm(request.app, key, run)
    return Response(status_code=204)


def _wake_chat(request: Request, key: str) -> None:
    """Start the chat's own Claude process too, while the phrase is still being heard
    (#4360), so its send does not wait for a cold start. In the background: neither
    this reply nor a send waits on it, and a chat whose provider cannot is skipped."""
    try:
        account, session, provider = resolve_current_leg(request, key)
    except Exception as exc:  # noqa: BLE001 -- a warm never fails over its head start
        log.debug("voice mode will not wake %s: %s", key, exc)
        return
    wake = getattr(provider, "wake_worker", None)
    if wake is None or session.key in _waking:
        return
    _waking.add(session.key)

    async def run() -> None:
        try:
            state = await wake(account, session.session_id)
            log.debug("voice mode woke %s: %s", session.key, state or "not asked")
        except Exception as exc:  # noqa: BLE001 -- only a head start: the send spawns anyway
            log.info("voice mode could not wake %s: %s", session.key, exc)
        finally:
            _waking.discard(session.key)

    _background(_wakes, run())


@router.post("/api/voice/end")
async def voice_end(request: Request) -> Response:
    """Voice mode turned off: the chat's fast agent and its memory go."""
    _require_same_origin(request)
    body = await _json_object(request)
    key = str(body.get("session_key") or "").strip()
    run = str(body.get("run") or "").strip()[:MAX_ID_CHARS]
    if key:
        await voice_helper.end(key, run)
    return Response(status_code=204)


@router.get("/sessions/{session_key}/voice-log", response_class=HTMLResponse)
async def voice_log(request: Request, session_key: str) -> HTMLResponse:
    """The chat's saved voice conversations, newest first: every leg of it (#3475)."""
    keys = get_state(request).chat_session_keys(session_key)
    phrases = voice_helper.saved(dict.fromkeys((*keys, session_key)))
    for phrase in phrases:
        phrase["when"] = datetime.fromtimestamp(phrase["at"]).strftime("%Y-%m-%d %H:%M:%S")
        phrase["parts"] = voice_helper.timing_parts(phrase.get("timing"))
        phrase["early"] = voice_helper.EARLY_WAYS.get((phrase.get("timing") or {}).get("early"))
    return get_templates(request).TemplateResponse(
        request,
        "voice_log.html",
        {
            "session_key": session_key,
            "phrases": phrases,
            "answered": voice_helper.ANSWERED,
            "summary": voice_helper.timing_summary(phrases),
            "early_summary": voice_helper.early_summary(phrases),
            "summary_of": voice_helper.TIMED_SUMMARY,
        },
    )
