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
did, keeps its audio there for 30 days.

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

``GET /api/speech/status`` says whether the service has its model loaded yet: 🎤 asks
when a phrase is slow to come back, so its bubble can say the service is starting
(#3722).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
import wave
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

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
MAX_ID_CHARS = 40
# The speech service's warm-ups (#3766), held so none is collected before it ends;
# nothing awaits them.
_stt_warming: set[asyncio.Task] = set()


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
    """A recorded phrase (16-bit WAV) as ``{"text", "language", "segments"}``:
    ``segments`` are the text's parts in order, each ``{"text", "unsure"}``, and voice
    mode marks the ones Whisper doubted for the agent (#3817)."""
    _require_same_origin(request)
    audio = await _audio(request)
    pinned = get_config(request).speech.language
    # The language heard last: a phrase too short to tell keeps it.
    previous = request.query_params.get("language")
    if pinned in LANGUAGES:
        path = f"/transcribe?only={pinned}"
    elif previous in LANGUAGES:
        path = f"/transcribe?language={previous}"
    else:
        path = "/transcribe"
    reply = await _forward(request, path, content=audio, headers={"Content-Type": "audio/wav"})
    heard = reply.json()
    language = pinned if pinned in LANGUAGES else heard.get("language")
    parts = heard.get("segments") if isinstance(heard.get("segments"), list) else []
    return JSONResponse(
        {
            "text": str(heard.get("text") or ""),
            "language": language if language in LANGUAGES else "en",
            "segments": [
                {"text": part["text"], "unsure": part.get("unsure") is True}
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ],
        }
    )


def _seconds(audio: bytes) -> float:
    try:
        with wave.open(io.BytesIO(audio)) as w:
            return round(w.getnframes() / w.getframerate(), 2)
    except (wave.Error, EOFError, ZeroDivisionError):
        return 0.0


def _write_wake_log(folder: Path, line: dict, audio: bytes | None) -> None:
    _write_log(folder, WAKE_LOG, line, audio, KEEP_AUDIO_S)


def _write_log(folder: Path, name: str, line: dict, audio: bytes | None, keep_s: float) -> None:
    """One line of the log ``name``, and the phrase's audio beside it when given.

    Every line also drops audio older than ``keep_s``, so recordings expire
    whether or not anything new is kept."""
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
    with (folder / name).open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps({"at": round(time.time(), 3), **line}) + "\n")


def _phrase_id(value: object) -> str:
    text = str(value or "")
    return text if PHRASE_ID.fullmatch(text) else ""


@router.post("/api/speech/wake")
async def wake(request: Request) -> JSONResponse:
    """A phrase heard while waiting (16-bit WAV) as the wake word detector's
    ``{"woken", "score", "speech_after"}`` (#3476). ``?id=`` names the phrase in the
    wake log."""
    _require_same_origin(request)
    audio = await _audio(request)
    speech = get_config(request).speech
    if not speech.wake_socket:
        raise HTTPException(status_code=503, detail="no wake word detector configured")
    phrase = _phrase_id(request.query_params.get("id"))
    started = time.monotonic()
    reply = await _forward(
        request,
        f"/wake?threshold={speech.wake_threshold}",
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
    if phrase:
        _recent_audio[phrase] = audio
        while len(_recent_audio) > RECENT_PHRASES:
            _recent_audio.popitem(last=False)
    keep = result["woken"] or result["score"] > NEAR_MISS
    line = {
        "id": phrase,
        "check": "detector",
        "mode": speech.wake,
        "seconds": _seconds(audio),
        "ms": round((time.monotonic() - started) * 1000),
        **result,
    }
    await asyncio.to_thread(
        _write_wake_log, Path(speech.wake_log_dir).expanduser(), line, audio if keep else None
    )
    return JSONResponse(result)


@router.post("/api/speech/wake-log")
async def wake_log(request: Request) -> Response:
    """What Whisper made of a waiting phrase, for the wake log (#3476): ``{"id",
    "text", "matched", "ms", "detected"}``. ``matched`` is the page's own wake word
    check, ``ms`` the wait from the phrase's end to the words, ``detected`` that
    the detector woke on it first. ``{"waiting": "start"|"stop", "spell"}`` marks
    when a spell of waiting starts and stops instead, so the report counts the time
    really spent waiting."""
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
    ``{"say", "relay"}``: what to say now and what to send the chat (null: nothing)."""
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
    return JSONResponse({"say": answer.say, "relay": answer.relay})


@router.post("/api/voice/narrate")
async def voice_narrate(request: Request) -> Response:
    """``{"session_key", "seconds", "run", "lang"}``: the chat has said nothing aloud for that
    long while it works, so the fast agent says what it is doing (#3799), as
    ``{"say"}``; 204 when it has nothing to say."""
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
    account, session, provider = resolve_current_leg(request, key)
    detail = await provider.load_transcript(account, session)
    done = voice_helper.steps(detail.events)
    finnish = body.get("lang") == "fi"
    said = await voice_helper.narrate(request.app, key, done, seconds, run, finnish, narration_id)
    if not said:
        return Response(status_code=204)
    return JSONResponse({"say": said})


@router.post("/api/voice/sent")
async def voice_sent(request: Request) -> Response:
    """``{"session_key", "id", "text", "how"}``: what the page really sent for a phrase
    (``text`` null: nothing, and ``how`` says why), saved in the voice log (#3475).
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
        await voice_helper.timed(key, phrase_id, body["timing"])
        return Response(status_code=204)
    if isinstance(body.get("spoken"), str):
        await voice_helper.spoken(key, phrase_id, body["spoken"][:MAX_SAY_CHARS])
        return Response(status_code=204)
    text = body.get("text")
    text = str(text)[: voice_helper.MAX_HEARD_CHARS * 2] if isinstance(text, str) else None
    how = str(body.get("how") or "")[:MAX_SAY_CHARS]
    await voice_helper.sent(key, phrase_id, text, how)
    return Response(status_code=204)


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
        await voice_helper.warm(request.app, key, run)
    return Response(status_code=204)


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
    return get_templates(request).TemplateResponse(
        request,
        "voice_log.html",
        {"session_key": session_key, "phrases": phrases, "answered": voice_helper.ANSWERED},
    )
