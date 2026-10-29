"""Dictation into the chat composer (#2456): the browser listens, the draft only grows."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest
from public_copy import shipped

# A stand-in for the browser's speech recognition. Tests drive it through
# `window.__speech`: `emit([[text, isFinal], ...])` delivers every result so far,
# the way a real recognizer re-reports the whole session on each event.
_FAKE_RECOGNITION = """
(() => {
  const made = [];
  class FakeRecognition {
    constructor() { this.calls = []; made.push(this); }
    start() { this.calls.push('start'); }
    stop() { this.calls.push('stop'); this.ended = true; this.onend && this.onend(); }
    abort() { this.calls.push('abort'); this.ended = true; this.onend && this.onend(); }
  }
  // Current Chromium ships the unprefixed name too; replace both.
  window.SpeechRecognition = FakeRecognition;
  window.webkitSpeechRecognition = FakeRecognition;
  window.__speech = {
    made,
    last: () => made[made.length - 1],
    emit(pairs) {
      const results = pairs.map(([text, isFinal]) => {
        const r = [{ transcript: text, confidence: 0.9 }];
        r.isFinal = isFinal;
        return r;
      });
      this.last().onresult({ resultIndex: 0, results });
    },
    fail(error) { this.last().onerror({ error }); },
  };
})();
"""

_NO_RECOGNITION = """
for (const name of ['SpeechRecognition', 'webkitSpeechRecognition']) {
  delete window[name];
  Object.defineProperty(window, name, { value: undefined, configurable: true });
}
"""


async def test_both_composers_and_the_settings_page_offer_dictation(tmp_path):
    from test_inject import _web_app

    from agentdeck import voice_helper

    app = _web_app(tmp_path, max_message_chars=500)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://deck") as client:
        chat = (await client.get("/sessions/codex:test:sid")).text
        # A chat voice mode was used in links to what its fast agent heard and said (#3475).
        voice_helper._save("codex:test:sid", {
            "at": 1.0, "heard": "Fix the log in bag.", "aloud": ["Reading the logs."],
            "outcome": "answered", "reply": "SAY: On it.\nRELAY: Fix the login bug.",
            "say": "On it.", "relay": "Fix the login bug.", "fresh": True, "ms": 900,
            "id": "a"})
        voice_helper._save("codex:test:sid", {
            "at": 1.5, "sent_for": "a", "sent": None, "how": "not sent: sent by hand"})
        voice_helper._save("codex:test:sid", {
            "at": 2.0, "heard": "Thanks for watching.", "aloud": [], "outcome": "answered",
            "reply": "SAY:\nRELAY: NONE", "say": "", "relay": None, "fresh": False, "ms": 700})
        voice_helper._save("codex:test:sid", {
            "at": 3.0, "heard": "Agent Dick", "aloud": [], "outcome": "timed out",
            "reply": None, "say": None, "relay": "Agent Dick", "fresh": False, "ms": 6000})
        # The fast agent spoke for a quiet chat (#3799); its line for a phrase is held,
        # and said only if the chat stays quiet: here the chat spoke first.
        voice_helper._save("codex:test:sid", {
            "at": 4.0, "narrated": ["Bash: pytest -q"], "quiet": 5, "outcome": "answered",
            "reply": "SAY: Running the tests.", "say": "Running the tests.", "fresh": False,
            "ms": 800, "id": "n"})
        voice_helper._save("codex:test:sid", {"at": 4.1, "spoken_for": "n",
                                              "spoken": "Running the tests."})
        voice_helper._save("codex:test:sid", {
            "at": 5.0, "heard": "Check the logs.", "aloud": [], "outcome": "answered",
            "reply": "SAY: The logs.\nRELAY: Check the logs.", "say": "The logs.",
            "relay": "Check the logs.", "fresh": False, "ms": 700, "id": "h", "held": True})
        voiced = (await client.get("/sessions/codex:test:sid")).text
        log_page = (await client.get("/sessions/codex:test:sid/voice-log")).text
        front = (await client.get("/")).text
        settings = (await client.get("/settings")).text
        # A speech service listening for English only (#3417), then for either
        # language, the default with the Parakeet engine (#3509).
        globals_ = app.state.templates.env.globals
        globals_["host_speech"] = True
        globals_["speech_language"] = "en"
        english = (await client.get("/sessions/codex:test:sid")).text
        host_settings = (await client.get("/settings")).text
        globals_["speech_language"] = "auto"
        either = (await client.get("/sessions/codex:test:sid")).text
        # An owner who always speaks Finnish (#3525).
        globals_["speech_language"] = "fi"
        finnish = (await client.get("/sessions/codex:test:sid")).text

    assert "voice-log" not in chat
    assert 'href="/sessions/codex:test:sid/voice-log"' in voiced
    phrases = re.findall(r'<section class="voice-log-phrase".*?</section>', log_page, re.S)
    assert ["Agent Dick" in phrases[1], "Thanks" in phrases[2], "bag" in phrases[3]] == [
        True] * 3
    assert "<strong>timed out</strong>" in phrases[1] and "not reported" in phrases[1]
    narrated = re.findall(r'<section class="voice-log-phrase voice-log-narrated".*?</section>',
                          log_page, re.S)
    assert len(narrated) == 1 and "agent quiet 5 s" in narrated[0]
    assert "Bash: pytest -q" in narrated[0]
    assert re.search(r"Spoken</span> Running the tests\.", narrated[0])
    assert log_page.index("voice-log-narrated") < log_page.index("Agent Dick")
    held = phrases[0]
    assert "Check the logs." in held and "Proposed</span> The logs." in held
    assert "nothing: the chat spoke first" in held and "Said</span>" not in held
    assert "not meant for the chat" in phrases[2]
    assert "🔊 Reading the logs." in phrases[3] and "Fix the login bug." in phrases[3]
    # What the helper proposed is told apart from what the page really sent.
    assert "<em>not sent: sent by hand</em>" in phrases[3]
    # The chat page carries only its own composer: the new-chat form left it with its
    # chat list (#2603) and is checked on the front page below. New work's appears
    # only when the host has an issue board, so it is left out here and checked
    # against a fixed board below (#2464).
    outside_new_work = re.sub(
        r'<details class="new-chat new-work".*?</details>', "", chat, flags=re.S
    )
    assert outside_new_work.count("data-speech-button") == 1
    assert 'class="speech-control"' in chat.split('class="inject-form"', 1)[1]
    assert 'id="new-chat-message"' in front and "data-speech-button" in front
    assert "data-speech-lang-setting" in settings
    assert '<option value="fi-FI">Suomi</option>' in settings
    assert "composer_speech.js" in chat
    # Voice mode (#3401): the chat composer alone, carrying the line its sends add,
    # and only with a speech service (#3470).
    assert "data-voice-button" not in chat and "data-host-speech" not in chat
    voice = re.sub(r'<details class="new-chat new-work".*?</details>', "", english, flags=re.S)
    assert voice.count("data-voice-button") == 1
    assert 'data-voice-line="(Voice mode: if this will take' in english
    assert 'data-voice-unheard="(Voice mode: when the person said this' in english
    assert 'data-voice-short="(Voice mode: this short yes or no' in english
    assert "voice_mode.js" in english
    # How long it listens with no words is a setting (#3424), 15 s unless changed.
    assert 'data-voice-idle-seconds="15"' in english
    assert "data-voice-line-fi" not in english
    # 🎤 listens through the host too (#3422), and /settings says so.
    assert 'class="speech-control" data-host-speech' in english
    assert "speech model does the listening" in " ".join(host_settings.split())
    assert "speech model does the listening" not in " ".join(settings.split())
    assert 'data-voice-line-fi="(Voice mode:' in either
    assert "data-voice-language" not in either
    assert 'data-voice-line-fi="(Voice mode:' in finnish
    assert 'data-voice-language="fi"' in finnish


def test_the_chat_view_shows_a_mark_instead_of_the_voice_line():
    """The agent receives the voice-mode line; the person sees 🎧 in its place (#3401)."""
    from agentdeck.voice_mode import (
        OLD_VOICE_LINES,
        SHORT_ANSWER_LINE,
        UNHEARD_LINE,
        VOICE_LINE,
        VOICE_LINE_FI,
    )
    from agentdeck.web.render import card_preview, display_chat_message

    assert display_chat_message(f"Add a test\n\n{VOICE_LINE}") == "Add a test 🎧"
    # A message sent before the line changed (#3407) still shows the mark.
    assert display_chat_message(f"Add a test\n\n{OLD_VOICE_LINES[0]}") == "Add a test 🎧"
    # The Finnish line asks for a Finnish answer and is hidden the same way (#3409).
    assert VOICE_LINE_FI.endswith("overrides any standing instruction to answer in English.)")
    assert display_chat_message(f"Lisää testi\n\n{OLD_VOICE_LINES[1]}") == "Lisää testi 🎧"
    assert display_chat_message(f"Lisää testi\n\n{VOICE_LINE_FI}") == "Lisää testi 🎧"
    assert display_chat_message(VOICE_LINE) == "🎧"
    assert card_preview(f"Add a test\n\n{VOICE_LINE}") == "Add a test 🎧"
    # Only a trailing line is the deck's own; a quoted one stays as written.
    quoted = f"Why does {VOICE_LINE} say that?"
    assert display_chat_message(quoted) == quoted
    # The line saying the person had not heard everything goes with it (#3520).
    unheard = UNHEARD_LINE.replace("{heard}", "“Shall I merge it?”")
    assert display_chat_message(f"Yes\n\n{unheard}\n\n{VOICE_LINE}") == "Yes 🎧"
    assert display_chat_message(f"Yes\n\n{unheard}") == f"Yes\n\n{unheard}"
    # A line sent before it asked for a repeat (#3771) is still hidden.
    old = unheard.split(". Do not take")[0] + (
        ". Do not take it as an answer to a question they have not heard; if it could be "
        "one, ask them first.)"
    )
    assert display_chat_message(f"Yes\n\n{old}\n\n{VOICE_LINE}") == "Yes 🎧"
    both = f"Yes\n\n{unheard}\n\n{SHORT_ANSWER_LINE}\n\n{VOICE_LINE_FI}"
    assert display_chat_message(both) == "Yes 🎧"
    # With 🎧 off a spoken yes or no carries the short-answer line alone (#3540).
    assert display_chat_message(f"Yes\n\n{SHORT_ANSWER_LINE}") == "Yes 🎧"


def _speech_server(socket_path):
    """``scripts/speech_server.py``'s HTTP side on ``socket_path``, with stand-in models."""
    import importlib.util
    import socket
    import threading
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "speech_server.py"
    spec = importlib.util.spec_from_file_location("speech_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeSpeech:
        said = []
        previous = []

        def transcribe(self, data, previous="", only=""):
            if not data.startswith(b"RIFF"):
                raise ValueError("expected 16-bit PCM")
            self.previous.append(previous + only.upper())
            return {"text": "hei maailma", "language": "fi", "seconds": 0.1,
                    "segments": [{"text": "hei", "unsure": False},
                                 {"text": "maailma", "unsure": True}, "x", {"text": 3}]}

        def say(self, text, language, name="", speed=1.0):
            self.said.append((text, language, name, speed))
            return b"RIFF-wav"

        def status(self):
            return {"engine": "parakeet", "loaded": False}

        def warm(self):
            self.warmed += 1

    FakeSpeech.warmed = 0

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    sock.listen(4)
    server = module.SpeechServer(sock, FakeSpeech())
    serve = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    serve.daemon = True
    serve.start()
    server.module = module
    return server


def _wake_server(socket_path):
    """``scripts/wake_server.py``'s HTTP side on ``socket_path``, with a stand-in
    detector: a phrase whose audio ends in ``J`` wakes it (#3476)."""
    import importlib.util
    import socket
    import sys
    import threading
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if "speech_server" not in sys.modules:
        spec = importlib.util.spec_from_file_location("speech_server", scripts / "speech_server.py")
        sys.modules["speech_server"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["speech_server"])
    spec = importlib.util.spec_from_file_location("wake_server", scripts / "wake_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeDetector:
        thresholds = []

        def wake(self, data, threshold):
            self.thresholds.append(threshold)
            woken = data.endswith(b"J")
            return {"woken": woken, "score": 0.9 if woken else 0.05, "speech_after": False}

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    sock.listen(4)
    server = module.WakeServer(sock, FakeDetector())
    serve = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    serve.daemon = True
    serve.start()
    return server


@shipped("scripts/speech_server.py", "scripts/wake_server.py")
async def test_speech_routes_pass_audio_to_the_host_service_and_back(tmp_path):
    """The page's phrase reaches the host service and its words come back with the
    language heard; text comes back as audio; no service, or a dead one, is a 503 the
    page falls back on; another site cannot use it (#3409)."""
    import sys
    from types import SimpleNamespace
    from unittest import mock

    from fastapi import FastAPI

    from agentdeck.config import AppConfig, SpeechConfig
    from agentdeck.web import routes_speech
    from agentdeck.web.routes_speech import router

    # The routes alone: a whole deck app costs this test its time budget.
    app = FastAPI()
    app.include_router(router)
    app.state.config = AppConfig()
    server = _speech_server(tmp_path / "s.sock")
    waker = _wake_server(tmp_path / "w.sock")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://deck") as client:
            unconfigured = await client.post("/api/speech/say", json={"text": "hi"})
            no_status = await client.get("/api/speech/status")
            # The wake word detector (#3476): its answer comes back and is logged
            # beside Whisper's, and a phrase either woke on keeps its audio.
            no_detector = await client.post("/api/speech/wake?id=a1", content=b"RIFF")
            app.state.config.speech.wake_socket = str(tmp_path / "w.sock")
            app.state.config.speech.wake_log_dir = str(tmp_path / "wake")
            app.state.config.speech.wake_threshold = 0.6
            # A recording past its 30 days goes on the next line written, even one
            # that keeps no audio of its own.
            (tmp_path / "wake").mkdir()
            stale = tmp_path / "wake/old.wav"
            stale.write_bytes(b"RIFF")
            month_ago = time.time() - 31 * 86400
            os.utime(stale, (month_ago, month_ago))
            started = await client.post(
                "/api/speech/wake-log", json={"waiting": "start", "spell": "s1"}
            )
            stale_gone = not stale.exists()
            woke = await client.post("/api/speech/wake?id=a1", content=b"RIFF..J")
            missed = await client.post("/api/speech/wake?id=b2", content=b"RIFF..x")
            await client.post("/api/speech/wake?id=../c3", content=b"RIFF..x")
            whisper_woke = await client.post(
                "/api/speech/wake-log",
                json={"id": "b2", "text": "Hey Jarvis.", "matched": True, "ms": 812.4},
            )
            unnamed = await client.post("/api/speech/wake-log", json={"text": "hi"})
            bad_spell = await client.post("/api/speech/wake-log", json={"waiting": "go"})
            await client.post("/api/speech/wake-log", json={"waiting": "stop", "spell": "s1"})
            app.state.config.speech.socket = str(tmp_path / "s.sock")
            # English only: the host transcribes as English, whatever the page last
            # heard (#3417).
            app.state.config.speech.language = "en"
            english = await client.post(
                "/api/speech/transcribe?language=fi", content=b"RIFF....WAVE"
            )
            app.state.config.speech.language = "auto"
            heard = await client.post("/api/speech/transcribe?id=k1", content=b"RIFF....WAVE")
            # Voice mode turning on loads the speech model behind the reply (#3766).
            warm = await client.post("/api/voice/warm", json={})
            while routes_speech._stt_warming:
                await asyncio.sleep(0.005)
            # Whether a slow phrase is waiting on the model loading (#3722).
            loading = await client.get("/api/speech/status")
            # The language heard last rides along; anything else is not passed on.
            await client.post("/api/speech/transcribe?language=fi", content=b"RIFF....WAVE")
            await client.post("/api/speech/transcribe?language=x%26y", content=b"RIFF....WAVE")
            garbled = await client.post("/api/speech/transcribe", content=b"not audio")
            spoken = await client.post(
                "/api/speech/say", json={"text": "Valmis.", "language": "fi"}
            )
            await client.post("/api/speech/say", json={"text": "Done.", "language": "sv"})
            # The voice is the deck's setting, so it changes with no code change (#3455).
            app.state.config.speech.voice_en = "en_US-joe-medium"
            await client.post("/api/speech/say", json={"text": "Hi.", "language": "en"})
            cross = await client.post(
                "/api/speech/say", json={"text": "hi"}, headers={"sec-fetch-site": "cross-site"}
            )
            app.state.config.speech.socket = str(tmp_path / "gone.sock")
            dead = await client.post("/api/speech/transcribe", content=b"RIFF....WAVE")
            dead_status = await client.get("/api/speech/status")
    finally:
        server.shutdown()
        server.server_close()
        waker.shutdown()
        waker.server_close()

    assert unconfigured.status_code == 503
    assert (no_status.status_code, dead_status.status_code) == (503, 503)
    assert loading.json() == {"loaded": False}
    assert no_detector.status_code == 503
    assert woke.json() == {"woken": True, "score": 0.9, "speech_after": False}
    assert missed.json()["woken"] is False
    assert waker.detector.thresholds == [0.6, 0.6, 0.6]
    assert (whisper_woke.status_code, unnamed.status_code) == (204, 400)
    assert (started.status_code, bad_spell.status_code, stale_gone) == (204, 400, True)
    log_text = (tmp_path / "wake/wake-log.jsonl").read_text()
    lines = [json.loads(line) for line in log_text.splitlines()]
    assert [(line.get("id") or line.get("spell"), line["check"]) for line in lines] == [
        ("s1", "waiting"), ("a1", "detector"), ("b2", "detector"), (None, "detector"),
        ("b2", "whisper"), ("s1", "waiting"),
    ]
    assert lines[4]["matched"] is True and lines[4]["ms"] == 812
    # The report counts the time really spent waiting, and a phrase the detector
    # never checked makes the switch verdict ineligible.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wake_report", Path(__file__).resolve().parents[1] / "scripts/wake_report.py"
    )
    wake_report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wake_report)

    def phrase(woken, text, matched, detector=True, whisper=True):
        found = {}
        if whisper:
            found["whisper"] = {"id": text, "text": text, "matched": matched, "mode": "shadow"}
        if detector:
            found["detector"] = {"id": text, "woken": woken, "score": 0.9, "mode": "shadow"}
        return found

    spells = {
        "s1": {"start": {"at": 0.0}, "stop": {"at": 7200.0}},
        "s2": {"start": {"at": 9000.0}},
    }
    complete = {"p1": phrase(True, "Hey Jarvis.", True), "p2": phrase(False, "Hi", False)}
    printed = wake_report.report(complete, spells)
    assert wake_report.waiting_hours(spells) == (2.0, 1)
    assert "Caught 100%" in printed and "withheld" not in printed
    # Either check missing on a shadow phrase withholds the verdict (owner's rule).
    for missing in ({"detector": False}, {"whisper": False}):
        partial = {**complete, "p3": phrase(True, "Hey Jarvis!", True, **missing)}
        printed = wake_report.report(partial, spells)
        assert "Switch verdict withheld: 1 shadow phrases" in printed
        assert "Caught" not in printed
    assert sorted(path.name for path in (tmp_path / "wake").glob("*.wav")) == ["a1.wav", "b2.wav"]
    # The parts, and which Whisper doubted, come along for voice mode to mark (#3817).
    parts = [{"text": "hei", "unsure": False}, {"text": "maailma", "unsure": True}]
    assert english.json() == {"text": "hei maailma", "language": "en", "segments": parts}
    assert heard.json() == {"text": "hei maailma", "language": "fi", "segments": parts}
    assert (warm.status_code, server.speech.warmed) == (204, 1)
    assert server.speech.previous == ["EN", "", "fi", ""]
    # Whisper's made-up closing lines and segments it rates as no speech are dropped.
    phantom = server.module.is_phantom

    def segment(text, no_speech=0.0, logprob=-0.2):
        return type("S", (), {"text": text, "no_speech_prob": no_speech, "avg_logprob": logprob})

    assert phantom(segment(" Thanks for watching, and I'll see you in the next video."))
    assert phantom(segment(" Tarkista lokit.", no_speech=0.9, logprob=-0.9))
    assert not phantom(segment(" Tarkista lokit."))
    # A short word a person may really say is dropped only when Whisper doubted it.
    assert phantom(segment(" Thank you.", no_speech=0.3))
    assert phantom(segment(" Mm-hmm.", logprob=-0.8))
    assert not phantom(segment(" Thank you.", no_speech=0.02, logprob=-0.2))
    assert not phantom(segment(" You?", no_speech=0.05, logprob=-0.3))
    # Hums are never an answer, however sure Whisper was (#3439); short words are.
    assert phantom(segment(" MMM!"))
    assert phantom(segment(" Hmm, uh..."))
    assert phantom(segment(" Ööh."))
    assert not phantom(segment(" Mm-hmm."))
    assert not phantom(segment(" Mm hmm."))
    assert phantom(segment(" Mm, mm."))
    assert not phantom(segment(" Ei."))
    assert not phantom(segment(" Stop!"))
    assert not phantom(segment(" Um, yes."))
    # The Parakeet engine (#3509) keeps the text checks only, and needs no language:
    # it replies with the pinned one, else the one heard last.
    module = server.module
    assert module.is_phantom_text("See you in the next video!") and module.is_phantom_text("Hmm.")
    assert not module.is_phantom_text("Thank you.") and not module.is_phantom_text("Mm-hmm.")
    config = tmp_path / "config.toml"
    config.write_text(
        '[speech]\nengine = "parakeet"\nparakeet_model = "/m/orukeet"\n'
        'whisper_model = "large-v3-turbo"\nwhisper_beam = 1\nwhisper_prompt = ""\n'
    )
    assert module.configured_engine(str(config)) == {
        "engine": "parakeet", "parakeet_model": "/m/orukeet",
        "whisper_model": "large-v3-turbo", "whisper_beam": 1, "whisper_prompt": "",
    }
    assert module.configured_engine(str(tmp_path / "none.toml")) == {}
    assert module.PARAKEET_MODEL == SpeechConfig().parakeet_model
    assert module.WHISPER_MODEL == SpeechConfig().whisper_model
    assert module.WHISPER_BEAM == SpeechConfig().whisper_beam
    assert module.WHISPER_PROMPT == SpeechConfig().whisper_prompt
    args = type("A", (), {"engine": "parakeet", "parakeet_model": "/m/orukeet", "idle_exit": 300})
    host = module.Speech(args)
    host.parakeet = lambda speech: {1: "Tee siitä commit ja pushaa se.", 2: "Thanks for watching."}[
        len(speech)
    ]
    real = (module.read_wav, module.speech_only)
    module.read_wav = lambda data: data
    module.speech_only = lambda samples: samples or None
    try:
        assert host.transcribe([1], "fi", "en")["text"] == "Tee siitä commit ja pushaa se."
        assert host.transcribe([1], "fi", "en")["language"] == "en"
        assert host.transcribe([1], "fi")["language"] == "fi"
        assert [host.transcribe([1, 2], "xx")[key] for key in ("text", "language")] == ["", "en"]
        assert host.transcribe([], "fi") == {"text": "", "language": "fi", "seconds": 0.0}
        module.gpu_mib = lambda: None
        assert (host.status()["engine"], host.status()["loaded"]) == ("parakeet", True)
        # Whisper names the parts it doubted, so voice mode can mark them (#3817).
        host.args.engine, host.args.beam, host.args.prompt = "whisper", 1, ""

        def said(text, logprob):
            return SimpleNamespace(
                text=text, no_speech_prob=0.0, avg_logprob=logprob, temperature=0.0
            )

        heard_segments = [said(" Tee issue.", -0.2), said(" Igu su aseto sorriva.", -0.73)]
        host.whisper = SimpleNamespace(transcribe=lambda *_, **__: (heard_segments, None))
        doubted = host.transcribe([1], "", "fi")
        assert (doubted["text"], doubted["segments"]) == (
            "Tee issue. Igu su aseto sorriva.",
            [{"text": "Tee issue.", "unsure": False},
             {"text": "Igu su aseto sorriva.", "unsure": True}],
        )
        # Whisper on the card falls back to Parakeet for a phrase it cannot hear, and
        # for every phrase once it failed to load (#3517).
        host.args.engine = "whisper"

        def broken(*_):
            raise RuntimeError("CUDA failed with error out of memory")

        host.transcribe_whisper = broken
        assert host.transcribe([1], "fi")["text"] == "Tee siitä commit ja pushaa se."
        host.whisper_failed = "no CUDA-capable device is detected"
        host.transcribe_whisper = None  # never called again
        assert host.transcribe([1], "fi")["text"] == "Tee siitä commit ja pushaa se."
        assert host.status()["whisper_failed"] == "no CUDA-capable device is detected"
    finally:
        module.read_wav, module.speech_only = real
    # A warm-up loads the engine a phrase would use, Parakeet once Whisper fails to
    # load, and a second warm-up while one runs starts nothing (#3766).
    args.engine = "whisper"
    args.device, args.model = "cuda", "large-v3-turbo"
    cold = module.Speech(args)
    loads = []

    def no_card(device):  # CUDA failing as the card is probed, before the model
        loads.append("whisper")
        raise RuntimeError("no CUDA-capable device is detected")

    def parakeet():
        loads.append("parakeet")
        return lambda speech: "hei"

    cold.load_parakeet = parakeet
    fake_whisper = SimpleNamespace(WhisperModel=None)
    with (
        mock.patch.object(module, "compute_type", no_card),
        mock.patch.dict(sys.modules, {"faster_whisper": fake_whisper}),
    ):
        with cold.recognition_lock:  # a phrase being heard: the warm-up waits for it
            cold.warm()
            first = cold.warming
            cold.warm()
            assert cold.warming is first and loads == []
            # Piper reads while a phrase is heard: the two do not share a lock.
            voice = SimpleNamespace(
                config=SimpleNamespace(length_scale=1.0),
                synthesize_wav=lambda text, out, syn_config: (
                    out.setparams((1, 2, 22050, 0, "NONE", "")), out.writeframes(b"\0\0")
                ),
            )
            cold.voices["fi_FI-harri-medium"] = voice
            piper = SimpleNamespace(SynthesisConfig=lambda length_scale: None)
            with mock.patch.dict(sys.modules, {"piper": piper}):
                assert cold.say("Hei.", "fi").startswith(b"RIFF")
        first.join(2)
    assert loads == ["whisper", "parakeet"] and cold.parakeet is not None
    assert cold.whisper_failed == "no CUDA-capable device is detected"
    cold.warm()
    assert cold.warming is first
    # The owner's decision on #3766: the model stays loaded, from login on.
    unit = (Path(__file__).resolve().parents[1] / "systemd/agentdeck-speech.service").read_text()
    assert "--idle-exit 0" in unit and "WantedBy=default.target" in unit
    assert "Restart=on-failure" in unit
    # So does the wake word detector, warmed at start (#3793).
    root = Path(__file__).resolve().parents[1]
    wake_unit = (root / "systemd/agentdeck-wake.service").read_text()
    assert "--idle-exit 0" in wake_unit and "WantedBy=default.target" in wake_unit
    assert "server.detector.warm()" in (root / "scripts/wake_server.py").read_text()
    # The language changes only on a sure call; a third language keeps the last one.
    choose = server.module.choose_language
    assert choose({"fi": 0.9, "en": 0.05}, "en") == ("fi", pytest.approx(0.947, abs=0.001))
    assert choose({"en": 0.5, "fi": 0.4}, "fi")[0] == "fi"
    assert choose({"ja": 0.97, "en": 0.02, "fi": 0.005}, "fi")[0] == "fi"
    assert choose({"ja": 0.97, "en": 0.02, "fi": 0.005})[0] == "en"
    # Silero closes speech well inside the page's pause, so the quiet tail is cut.
    script = Path(__file__).resolve().parents[1] / "src/agentdeck/web/static/voice_mode.js"
    pause = int(re.search(r"HOST_PAUSE_MS = (\d+)", script.read_text()).group(1))
    assert server.module.VAD_SILENCE_MS + server.module.VAD_PAD_MS < pause
    # A one-word answer the page started a phrase for (3 chunks of 4096 samples at
    # 48 kHz, about 255 ms) is never too short for the VAD to keep.
    assert server.module.VAD_MIN_SPEECH_MS < 3 * 4096 * 1000 // 48000
    assert garbled.status_code == 503
    assert (spoken.status_code, spoken.headers["content-type"], spoken.content) == (
        200, "audio/wav", b"RIFF-wav"
    )
    assert server.speech.said == [
        ("Valmis.", "fi", "fi_FI-harri-medium", 1.5),
        ("Done.", "en", "en_US-ryan-high", 1.0),
        ("Hi.", "en", "en_US-joe-medium", 1.0),
    ]
    # The service's own defaults match the setting's, for a request naming no voice.
    assert server.module.DEFAULT_VOICES == {
        "en": SpeechConfig().voice_en, "fi": SpeechConfig().voice_fi
    }
    assert cross.status_code == 403
    assert dead.status_code == 503


async def test_the_fast_agent_says_what_the_quiet_agent_is_doing(monkeypatch):
    """The chat silent after a send, the fast agent is told the steps the agent took
    since the person last spoke, read from the transcript, and says one sentence on
    them; "SAY:" alone or no steps says nothing (204). It is saved in the voice log
    and remembered for the next phrase (#3799). A last step with no result yet is
    told as still running, and for how long, so it is said to continue (#3806)."""
    import asyncio
    import json as json_
    import re
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from fastapi import FastAPI

    from agentdeck import voice_helper
    from agentdeck.config import AppConfig
    from agentdeck.models import Account, TranscriptEvent
    from agentdeck.web import routes_speech

    started = datetime.now(UTC) - timedelta(seconds=40)
    replies = ["SAY: Running the tests now.", "SAY:"]
    told = []
    lines = asyncio.Queue()

    def write(data):
        told.append(json_.loads(data)["message"]["content"])
        lines.put_nowait(json_.dumps({"type": "result", "result": replies.pop(0)}).encode())

    async def drain():
        return None

    process = SimpleNamespace(returncode=None, stdout=SimpleNamespace(readline=lines.get),
                              stdin=SimpleNamespace(write=write, drain=drain), kill=lambda: None)
    process.wait = drain

    async def start(account, prompt=None):
        return process

    events = [
        TranscriptEvent(seq=1, role="user", text="Old message"),
        TranscriptEvent(seq=2, role="tool", tool_name="Bash", tool_summary="git log"),
        TranscriptEvent(seq=3, role="user", text="Fix the login bug 🎧"),
        TranscriptEvent(seq=4, role="assistant", tool_name="Read", tool_summary="login.py"),
        TranscriptEvent(seq=5, role="tool", text="file contents"),
        TranscriptEvent(seq=6, role="user", text="and the readme", queued=True),
        TranscriptEvent(seq=7, role="tool", tool_name="Grep", subagent="a1"),
        TranscriptEvent(seq=8, role="assistant", text="API error", failed=True),
        TranscriptEvent(seq=9, role="assistant", text="The bug is in the  session check."),
        TranscriptEvent(seq=10, role="assistant", tool_name="Bash", ts=started,
                        tool_display_name="Run tests", tool_summary="pytest -q " + "x" * 200),
    ]
    transcript = SimpleNamespace(events=events)

    async def load_transcript(account, session):
        return transcript

    provider = SimpleNamespace(load_transcript=load_transcript)
    monkeypatch.setattr(routes_speech, "resolve_current_leg",
                        lambda request, key: (None, None, provider))
    monkeypatch.setattr(voice_helper, "start_process", start)
    app = FastAPI()
    app.include_router(routes_speech.router)
    app.state.config = AppConfig()
    app.state.accounts = [Account("claude_code:main", "claude_code", "main", Path("/a/main"))]
    app.state.app_state = SimpleNamespace(usage={}, sessions={})
    key = "claude_code:main:quiet"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://deck") as client:
        said = await client.post("/api/voice/narrate", json={
            "session_key": key, "seconds": 5, "lang": "fi", "run": "r", "id": "n1"})
        # The page then says which line it really spoke.
        spoke = await client.post("/api/voice/sent", json={
            "session_key": key, "id": "n1", "spoken": "Running the tests now."})
        nothing = await client.post("/api/voice/narrate", json={"session_key": key, "seconds": 30})
        transcript.events = events[:3]
        no_steps = await client.post("/api/voice/narrate", json={"session_key": key})
        no_key = await client.post("/api/voice/narrate", json={})
    agent = voice_helper._agents[key]
    await voice_helper.end(key)

    assert said.status_code == 200 and said.json() == {"say": "Running the tests now."}
    assert spoke.status_code == 204
    assert [nothing.status_code, no_steps.status_code, no_key.status_code] == [204, 204, 400]
    # The last call has no result yet: it is marked running, with how long (#3806).
    assert re.fullmatch(
        "Agent working quietly for 5 seconds. What it did since the person last spoke:\n"
        "- Read: login.py\n- Wrote: The bug is in the session check.\n"
        f"- Run tests: pytest -q {'x' * 138}… \\(still running for 4\\d seconds, "
        "no result yet\\)\n\nSay it in Finnish.", told[0])
    # A result after it, whichever parallel call it answers, means it may be done.
    then = started + timedelta(seconds=40)
    result = TranscriptEvent(seq=11, role="tool", text="ok")
    call = TranscriptEvent(seq=12, role="assistant", tool_name="Bash", ts=started)
    assert voice_helper.steps(events + [result], then)[-1].endswith("…")
    assert voice_helper.steps([call, result, call], then)[-1] == (
        "Bash (still running for 40 seconds, no result yet)")
    assert voice_helper.steps([call, call, result], then) == ["Bash", "Bash"]
    assert "edelleen" in voice_helper.system_prompt()
    assert told[1].startswith("Agent working quietly for 30 seconds.")
    assert told[1].endswith("Say it in the language of their last message.")
    assert len(told) == 2
    assert agent.history == ["You, while it worked: Running the tests now."]
    saved = voice_helper.saved([key])
    assert [(p["say"], p["quiet"], p["outcome"]) for p in reversed(saved)] == [
        ("Running the tests now.", 5, "answered"), ("", 30, "answered")]
    assert saved[-1]["narrated"][0] == "Read: login.py"
    assert saved[-1]["spoken"] == "Running the tests now." and saved[0]["spoken"] is None


async def test_the_fast_agent_answers_at_once_and_relays_what_was_meant(monkeypatch):
    """Each phrase goes to one Haiku process per chat, kept open between phrases: its
    SAY line is spoken, its RELAY line is sent, NONE sends nothing, and a rewording is
    not trusted. Its memory is only what was said by voice; a process that misses the
    timeout is closed, and a fresh one is told the voice conversation so far. Voice
    mode turning off ends it; another site cannot use it (#3444). Every phrase is
    saved and read back on the chat's voice log page, timeouts included (#3475).
    The page can start it before a phrase; its instructions carry the owner's words,
    the deck's projects and the chat's title; a filler "Okay," is not spoken (#3507)."""
    import asyncio
    import json as json_
    from types import SimpleNamespace

    from fastapi import FastAPI

    from agentdeck import voice_helper
    from agentdeck.config import AppConfig
    from agentdeck.models import Account
    from agentdeck.web.routes_speech import router

    class FakeProcess:
        def __init__(self, replies):
            self.replies, self.told, self.returncode = list(replies), [], None
            self.lines = asyncio.Queue()
            self.stdin = SimpleNamespace(write=self.write, drain=self.drain)
            self.stdout = SimpleNamespace(readline=self.lines.get)

        def write(self, data):
            self.told.append(json_.loads(data)["message"]["content"])
            reply = self.replies.pop(0)
            if reply is not None:  # None: it never answers
                self.lines.put_nowait(b'{"type": "assistant"}\n')
                self.lines.put_nowait(json_.dumps({"type": "result", "result": reply}).encode())

        async def drain(self):
            return None

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    started = []
    scripts = [
        ["SAY: Starting on the login bug.\nRELAY: Fix the login bug.",
         "SAY: Okay.\nRELAY: NONE",
         "SAY: Sure.\nRELAY: Rewrite the whole app in Rust and deploy it to every server.",
         None],
        ["SAY: Adding a test.\nRELAY: and add a test for it"],
        ["SAY: Okay, the greeting.\nRELAY: Hello."],
    ]

    async def start(account, prompt=None):
        started.append(account.key)
        prompts.append(prompt)
        process = FakeProcess(scripts[len(started) - 1])
        processes.append(process)
        return process

    processes, prompts = [], []
    monkeypatch.setattr(voice_helper, "start_process", start)
    monkeypatch.setattr(voice_helper, "TURN_TIMEOUT_S", 0.05)
    app = FastAPI()
    app.include_router(router)
    app.state.config = AppConfig()
    main = Account("claude_code:main", "claude_code", "main", Path("/accounts/main"))
    app.state.accounts = [main]
    chat = SimpleNamespace(
        project=SimpleNamespace(authoritative=True), project_name="bazaar",
        display_title="Fix the login bug")
    app.state.app_state = SimpleNamespace(usage={}, sessions={"claude_code:main:s1": chat})
    app.state.config = AppConfig()
    transport = httpx.ASGITransport(app=app)

    async def hear(text, aloud=(), key="claude_code:main:s1"):
        return await client.post("/api/voice/heard", json={
            "session_key": key, "text": text, "aloud": list(aloud), "id": text[:8]})

    async with httpx.AsyncClient(transport=transport, base_url="http://deck") as client:
        warmed = await client.post("/api/voice/warm", json={"session_key": "claude_code:main:s1"})
        warm_again = await client.post(
            "/api/voice/warm", json={"session_key": "claude_code:main:s1"})
        first = await hear("Fix the log in bag.")
        # The page says what it really sent, here with words typed meanwhile (#3475).
        reported = await client.post("/api/voice/sent", json={
            "session_key": "claude_code:main:s1", "id": "Fix the ",
            "text": "Fix the login bug. please", "how": "sent"})
        noise = await hear("Thanks for watching.", aloud=["On it, reading the logs."])
        reworded = await hear("Check the logs.")
        missed = await hear("And add a test four it.")
        fresh = await hear("and add a test four it")
        cross = await client.post("/api/voice/heard", json={
            "session_key": "k", "text": "hi"}, headers={"sec-fetch-site": "cross-site"})
        empty = await hear(" ")
        ended = await client.post(
            "/api/voice/end", json={"session_key": "claude_code:main:s1", "run": "r1"})
        after_end = dict(voice_helper._agents)
        # A warm sent before the end but delivered after it starts nothing.
        late_warm = await client.post(
            "/api/voice/warm", json={"session_key": "claude_code:main:s1", "run": "r1"})
        late_phrase = await client.post("/api/voice/heard", json={
            "session_key": "claude_code:main:s1", "text": "Hello.", "run": "r1"})
        after_late_warm = (dict(voice_helper._agents), len(started))
        # A chat whose page went away without saying so is closed once idle,
        # with no later phrase to notice it.
        monkeypatch.setattr(voice_helper, "IDLE_S", 0.01)
        # An owner who always speaks Finnish is always acknowledged in Finnish (#3525).
        app.state.config.speech.language = "fi"
        await hear("Hello.", key="claude_code:main:s2")
        await asyncio.sleep(0.05)
        after_idle = dict(voice_helper._agents)
        app.state.accounts = []
        none = await hear("Hello.")

    saved = voice_helper.saved(["claude_code:main:s1"])
    assert [(p["heard"], p["outcome"], p["relay"]) for p in reversed(saved)] == [
        ("Fix the log in bag.", "answered", "Fix the login bug."),
        ("Thanks for watching.", "answered", None),
        ("Check the logs.", "answered", "Check the logs."),
        ("And add a test four it.", "timed out", "And add a test four it."),
        ("and add a test four it", "answered", "and add a test for it"),
        ("Hello.", "no account", "Hello."),
    ]
    assert saved[-2]["aloud"] == ["On it, reading the logs."] and saved[1]["fresh"]
    assert reported.status_code == 204
    assert saved[-1]["report"]["sent"] == "Fix the login bug. please"
    assert saved[-2]["report"] is None
    assert saved[3]["reply"].startswith("SAY: Sure.\nRELAY: Rewrite the whole app")

    assert first.json() == {"say": "Starting on the login bug.", "relay": "Fix the login bug."}
    # Started ahead by the page, once: the first phrase found it running.
    assert warmed.status_code == warm_again.status_code == 204
    assert not saved[-1]["fresh"]
    assert "AgentDeck" in prompts[0] and ", bazaar." in prompts[0]
    assert prompts[0].endswith("titled: Fix the login bug")
    assert "acknowledgement, in the language they spoke" in prompts[0]
    assert "acknowledgement, in Finnish" in prompts[2]
    assert noise.json() == {"say": "", "relay": None}
    assert reworded.json() == {"say": "Sure.", "relay": "Check the logs."}
    assert missed.status_code == 503
    assert fresh.json() == {"say": "Adding a test.", "relay": "and add a test for it"}
    # One process per chat, kept open; a second only after the first missed its turn.
    assert started == ["claude_code:main"] * 3
    assert processes[0].returncode == -9
    assert processes[0].told[0] == "Heard: Fix the log in bag."
    assert processes[0].told[1] == (
        "Said aloud by the agent since:\nOn it, reading the logs.\n\nHeard: Thanks for watching.")
    # The fresh process is told the voice conversation, and nothing else: the
    # phrase the timed-out one never answered too, which the next one follows on.
    assert processes[1].told == ["Earlier in this voice conversation:\n"
        "Person: Fix the log in bag.\nYou: Starting on the login bug.\n"
        "Agent said aloud: On it, reading the logs.\nPerson: Thanks for watching.\n"
        "Person: Check the logs.\nYou: Sure.\nPerson: And add a test four it.\n\n"
        "Heard: and add a test four it"]
    assert ended.status_code == 204 and after_end == {}
    assert late_warm.status_code == 204 and after_late_warm == ({}, 2)
    assert late_phrase.status_code == 503
    assert processes[1].returncode == -9
    assert after_idle == {} and processes[2].returncode == -9
    assert [r.status_code for r in (cross, empty, none)] == [403, 400, 503]
    # Voice mode ending while a process is still starting: it is closed, not left
    # running untracked (#3448 review).
    gate = asyncio.Event()
    late = FakeProcess(["SAY: Hi.\nRELAY: hi"])

    async def slow_start(account, prompt=None):
        await gate.wait()
        return late

    monkeypatch.setattr(voice_helper, "start_process", slow_start)
    app.state.accounts = [main]
    turn = asyncio.create_task(voice_helper.heard(app, "s3", "hi", []))
    await asyncio.sleep(0)
    await voice_helper.end("s3")
    gate.set()
    raced = await turn
    assert raced is None and late.returncode == -9 and "s3" not in voice_helper._agents
    parse = voice_helper.parse
    assert parse("SAY: (no reply)\nRELAY: NONE", "thanks for watching") == (
        voice_helper.Answer("", None))
    assert parse("SAY: Okay.\nRELAY: NONE.", "None.").relay == "None."
    assert parse("Starting now.", "fix it") == voice_helper.Answer("", "fix it")
    assert parse("SAY: Okay, the login bug.\nRELAY: fix it", "fix it").say == "The login bug."
    assert parse("SAY: OK.\nRELAY: fix it", "fix it").say == "OK."
    assert parse("SAY: Okayish.\nRELAY: fix it", "fix it").say == "Okayish."
    # A short yes or no is never dropped as noise; a lone "you" after a question is
    # the Finnish "juu", elsewhere it goes as heard; "uh-huh" and real noise still are
    # dropped (#3521).
    assert parse("SAY:\nRELAY: NONE", "Joo.") == voice_helper.Answer("", "Joo.")
    assert parse("SAY:\nRELAY: NONE", "Ei, ei.").relay == "Ei, ei."
    assert parse("SAY:\nRELAY: NONE", "You.", question=True).relay == "Juu."
    assert parse("SAY: Yes.\nRELAY: You.", "You.", question=True).relay == "Juu."
    assert parse("SAY:\nRELAY: NONE", "You.").relay == "You."
    assert parse("SAY:\nRELAY: NONE", "Uh-huh.", question=True).relay is None
    assert parse("SAY:\nRELAY: NONE", "Bröt.", question=True).relay is None
    assert parse("SAY: Sure.\nRELAY: thank you", "Thank you", question=True).relay == (
        "thank you")
    # Words Whisper doubted reach the chat as heard, marked, however the fast agent
    # reworded them, and are never noise (#3817).
    doubted = "Tee se. [unsure: Igu su aseto sorriva.]"
    assert parse("SAY: Ok.\nRELAY: Tee se. Eli sun asetus jää.", doubted).relay == doubted
    kept = "SAY: Ok.\nRELAY: Tee se nyt. [unsure: Igu su aseto sorriva.]"
    assert parse(kept, doubted).relay == "Tee se nyt. [unsure: Igu su aseto sorriva.]"
    assert parse("SAY:\nRELAY: NONE", "[unsure: No.]").relay == "[unsure: No.]"
    assert parse("SAY:\nRELAY: NONE", doubted).relay == doubted
    # Nor is a doubted lone "you" a sure "Juu.", or a doubted phrase a jump (#3817).
    assert parse("SAY:\nRELAY: Juu.", "[unsure: You.]", question=True).relay == (
        "[unsure: You.]")
    assert parse("BACK", "[unsure: Back to deck.]", back=True) == voice_helper.Answer(
        "", "[unsure: Back to deck.]")
    agent = voice_helper._Agent("q")
    assert voice_helper._asked(agent, ["Long line " * 80 + "Shall I?"])
    agent.history = ["Agent said aloud: Which one?", "Person: hm"]
    assert voice_helper._asked(agent, []) and not voice_helper._asked(agent, ["Done."])
    command = voice_helper.fast_command("/bin/claude")
    assert "[unsure: …]" in command[command.index("--system-prompt") + 1]
    assert command[:4] == ["/bin/claude", "-p", "--model", "haiku"]
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert "--no-session-persistence" in command and "Never add, guess" in command[
        command.index("--system-prompt") + 1]
    assert "go back to the deck chat" not in prompts[0]

    # The way back to the deck chat (#3816): told only in a chat that is not the deck chat
    # while one exists, it sends nothing and names the deck chat's page. A BACK from a
    # process not told it is only words.
    assert parse("BACK", "back to the deck chat", back=True) == voice_helper.Answer(
        "", None, back=True)
    assert parse("BACK", "back to the deck chat").relay == "back to the deck chat"
    app.state.db = SimpleNamespace(deck_chat_starts=lambda: [("deck-token", "first", 0)])
    app.state.app_state = SimpleNamespace(
        usage={}, sessions={"claude_code:main:s1": chat},
        session_key_for_token={"deck-token": "claude_code:main:deck"}.get,
        chat_identity_key=lambda key: key, chat_path_for_session=lambda key: f"/chats/{key}")
    monkeypatch.setattr(voice_helper, "start_process", start)
    scripts.extend([["BACK"], ["BACK"]])
    async with httpx.AsyncClient(transport=transport, base_url="http://deck") as client:
        back = await hear("Take me back to the deck chat.")
        stays = await hear("Back.", key="claude_code:main:deck")
    assert "go back to the deck chat" in prompts[-2] and "BACK" in prompts[-2]
    assert "go back to the deck chat" not in prompts[-1]
    assert back.json() == {"say": "", "relay": None, "jump": {
        "path": "/chats/claude_code:main:deck", "title": "Deck chat"}}
    assert stays.json()["relay"] == "Back."
    assert voice_helper.saved(["claude_code:main:s1"])[0]["back"] is True


async def test_an_answer_thrown_away_is_forgotten_and_the_waits_are_saved(monkeypatch):
    """#3765: the page asks the fast agent as soon as a phrase's words come back and
    throws the answer away when more words join; the next ask names it, so its lines
    leave the history and the process is told it was never spoken. The page's
    measured waits are saved with the phrase. The "Mm." is gone (#3780): nothing
    reads one and its route is no more."""
    from types import SimpleNamespace

    from fastapi import FastAPI

    from agentdeck import voice_helper
    from agentdeck.config import AppConfig
    from agentdeck.models import Account
    from agentdeck.web import routes_speech

    told, starts = [], []

    class Process:
        returncode = None

        def __init__(self):
            self.lines = asyncio.Queue()
            self.stdin = SimpleNamespace(write=self.write, drain=self.drain)
            self.stdout = SimpleNamespace(readline=self.lines.get)

        def write(self, data):
            told.append(json.loads(data)["message"]["content"])
            result = {"type": "result", "result": f"SAY: Heard {len(told)}.\nRELAY: x"}
            self.lines.put_nowait(json.dumps(result).encode())

        async def drain(self):
            return None

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def start(account, prompt=None):
        starts.append(prompt)
        return Process()

    monkeypatch.setattr(voice_helper, "start_process", start)
    app = FastAPI()
    app.include_router(routes_speech.router)
    app.state.config = AppConfig()
    app.state.accounts = [Account("claude_code:main", "claude_code", "main", Path("/a"))]
    app.state.app_state = SimpleNamespace(usage={}, sessions={})
    forwarded = []

    async def forward(request, path, **kwargs):
        forwarded.append((path, kwargs["json"]))
        return SimpleNamespace(content=b"RIFFmm")

    monkeypatch.setattr(routes_speech, "_forward", forward)
    key = "claude_code:main:s9"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://deck"
    ) as client:

        async def hear(text, phrase_id, drop="", aloud=()):
            return await client.post("/api/voice/heard", json={
                "session_key": key, "text": text, "id": phrase_id, "drop": drop,
                "aloud": list(aloud)})

        await hear("Fix the", "p1", aloud=["Done."])
        await hear("Fix the login bug", "p2", drop="p1", aloud=["Done."])
        # A drop naming an ask that is not the last one changes nothing.
        await hear("And the test", "p3", drop="p1")
        timing = await client.post("/api/voice/sent", json={
            "session_key": key, "id": "p2",
            "timing": {"words": 210, "sent": 1210, "voice": 530.4, "bogus": 1, "sent_x": -1}})
        mm = await client.get("/api/voice/filler?language=fi")

    agent = voice_helper._agents[key]
    assert told[0] == "Said aloud by the agent since:\nDone.\n\nHeard: Fix the"
    assert told[1] == (voice_helper.UNSPOKEN + "\n\nSaid aloud by the agent since:\nDone."
                       "\n\nHeard: Fix the login bug")
    assert told[2] == "Heard: And the test"
    assert agent.history == ["Agent said aloud: Done.", "Person: Fix the login bug",
                             "You: Heard 2.", "Person: And the test", "You: Heard 3."]
    # A fresh process is given the history, which never held the dropped ask.
    agent.last_id, agent.last_lines, agent.new = "p3", 2, True
    agent.history.pop()
    agent.history.pop()
    assert voice_helper.message("x", [], agent.history) == (
        "Earlier in this voice conversation:\nAgent said aloud: Done.\n"
        "Person: Fix the login bug\nYou: Heard 2.\n\nHeard: x")
    saved = voice_helper.saved([key])
    assert [p.get("replaces") for p in saved] == [None, "p1", None]
    assert timing.status_code == 204
    assert saved[1]["timing"] == {"words": 210, "sent": 1210, "voice": 530}
    assert saved[0]["timing"] is None and len(starts) == 1
    assert mm.status_code in (404, 405) and forwarded == []


@pytest.mark.browser
async def test_speech_adds_to_the_draft_never_sends_and_stops_on_send(tmp_path):
    """The button adds no height and a crowded phone row still fits, words land after
    the draft as they are heard, typing mid-dictation moves the anchor, a send aborts
    listening so nothing refills the cleared box, an error says why, Android's growing
    repeats are not doubled, and the language is Finnish until switched."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    from agentdeck.models import IssueRef

    app = _web_app(tmp_path, max_message_chars=500)
    # The issue chip and a price are what crowded the row on a real phone.
    app.state.app_state.set_issues("codex:test:sid", [IssueRef("eerovil/agentdeck", 2456)])
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_FAKE_RECOGNITION)
        page = await context.new_page()
        sent = []

        async def fulfil(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        await page.route("**/sessions/codex:test:sid/inject", fulfil)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        mic = page.locator("form.inject-form [data-speech-button]")
        await mic.wait_for(state="visible")

        layout = await page.evaluate(
            """() => {
              const form = document.querySelector('form.inject-form');
              const control = form.querySelector('.speech-control');
              const withMic = form.getBoundingClientRect().height;
              control.style.display = 'none';
              const without = form.getBoundingClientRect().height;
              control.style.display = '';
              const send = form.querySelector('#composer-controls button[type=submit]');
              const box = control.getBoundingClientRect();
              const picker = form.querySelector('.image-picker').getBoundingClientRect();
              const chips = [...form.querySelectorAll(
                '.composer-actions > .speech-control, .composer-actions > details')]
                .map(el => el.getBoundingClientRect());
              const edge = form.getBoundingClientRect().right;
              return {
                pickerSize: [Math.round(picker.width), Math.round(picker.height)],
                pickerClear: picker.right <= box.left,
                // 🎤, ⚡ and the issue chip; 🎧 needs the speech service (#3470).
                chipsWhole: chips.length === 3 && chips.every(c => c.width >= 29
                  && c.right <= edge),
                addedHeight: Math.round(withMic - without),
                micSize: [Math.round(box.width), Math.round(box.height)],
                sendOnScreen: send.getBoundingClientRect().right <= window.innerWidth,
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )

        box = page.locator("#inject-message")
        await box.fill("Fix the")
        await page.evaluate(
            """() => { window.__inputs = 0; document.getElementById('inject-message')
                 .addEventListener('input', () => window.__inputs++); }"""
        )
        await mic.click()
        started = await page.evaluate(
            """() => ({ lang: __speech.last().lang, calls: __speech.last().calls,
                       continuous: __speech.last().continuous,
                       interim: __speech.last().interimResults,
                       pressed: document.querySelector('form.inject-form [data-speech-button]')
                         .getAttribute('aria-pressed'),
                       status: document.querySelector('form.inject-form .speech-status')
                         .textContent,
                       focused: document.activeElement.id })"""
        )
        await page.evaluate("__speech.emit([['login bug', false]])")
        interim = await box.input_value()
        await page.evaluate("__speech.emit([['login bug', true], [' today', false]])")
        heard = await box.input_value()
        inputs = await page.evaluate("window.__inputs")

        # The person types mid-dictation: the next words go after what they typed.
        await box.press("End")
        await box.type(", please")
        await page.evaluate(
            "__speech.emit([['login bug', true], [' today', true], [' and add a test', true]])"
        )
        after_typing = await box.input_value()

        # Their own tap keeps the last phrase (stop, not abort).
        await mic.click()
        stopped = await page.evaluate(
            """() => ({ calls: __speech.last().calls,
                       pressed: document.querySelector('form.inject-form [data-speech-button]')
                         .getAttribute('aria-pressed') })"""
        )

        # A send while listening aborts, and a late result cannot refill the box.
        await mic.click()
        await page.evaluate("__speech.emit([[' now', false]])")
        async with page.expect_response("**/inject"):
            await box.press("Enter")
        await wait_until(page, "the composer cleared after the send",
                         "() => document.getElementById('inject-message').value === ''")
        send_calls = await page.evaluate("__speech.last().calls")
        await page.evaluate("__speech.last().onresult && __speech.emit([[' late', true]])")
        after_send = await box.input_value()

        # An error says why, above the button, without adding a row.
        await mic.click()
        await page.evaluate("__speech.fail('not-allowed'); __speech.last().onend()")
        error = await page.evaluate(
            """() => { const s = document.querySelector('form.inject-form .speech-status');
                       const b = document.querySelector('form.inject-form [data-speech-button]');
                       return { text: s.textContent, shown: !s.hidden,
                                pressed: b.getAttribute('aria-pressed') }; }"""
        )

        # Chrome on Android re-reports the growing sentence; it lands once.
        await box.fill("")
        await mic.click()
        await page.evaluate(
            """__speech.emit([['okay', true], ['okay it', true], ['Okay it works', true],
                             ['but', true], ['but I', true], ['but I cannot', false]])"""
        )
        android = await box.input_value()

        # Tapping the bubble switches to English, keeping what was heard, and listens on.
        await page.locator("form.inject-form [data-speech-lang-cycle]").click()
        switched = await page.evaluate(
            """() => ({ recognizers: __speech.made.length, previous: __speech.made.at(-2).calls,
                       lang: __speech.last().lang, calls: __speech.last().calls,
                       pressed: document.querySelector('form.inject-form [data-speech-button]')
                         .getAttribute('aria-pressed'),
                       status: document.querySelector('form.inject-form .speech-status')
                         .textContent })"""
        )
        await page.evaluate("__speech.emit([['change the language', true]])")
        after_switch = await box.input_value()
        await mic.click()

        # The language chosen on /settings is the one the recognizer is asked for.
        await page.goto(f"{base_url}/settings")
        switched_setting = await page.input_value("[data-speech-lang-setting]")
        await page.select_option("[data-speech-lang-setting]", "fi-FI")
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await mic.click()
        chosen = await page.evaluate("__speech.last().lang")
        await page.goto(f"{base_url}/settings")
        remembered = await page.input_value("[data-speech-lang-setting]")

        # A browser with no speech recognition shows no button at all.
        bare = await browser.new_context(viewport={"width": 360, "height": 780},
                                         service_workers="block")
        await bare.add_init_script(_NO_RECOGNITION)
        bare_page = await bare.new_page()
        await bare_page.goto(f"{base_url}/sessions/codex:test:sid")
        await bare_page.wait_for_selector("form.inject-form #inject-message")
        bare_visible = await bare_page.locator(
            "form.inject-form [data-speech-button]").is_visible()

        # With the speech service 🎧 joins the row, and the crowded row still fits.
        app.state.templates.env.globals["host_speech"] = True
        hosted = await browser.new_context(viewport={"width": 320, "height": 780},
                                           service_workers="block")
        await hosted.add_init_script(_NO_RECOGNITION)
        await hosted.add_init_script(_FAKE_MICROPHONE)
        hosted_page = await hosted.new_page()
        await hosted_page.goto(f"{base_url}/sessions/codex:test:sid")
        await hosted_page.locator("form.inject-form [data-voice-button]").wait_for(
            state="visible")
        crowded = await hosted_page.evaluate(
            """() => {
              const form = document.querySelector('form.inject-form');
              const edge = form.getBoundingClientRect().right;
              const chips = [...form.querySelectorAll(
                '.composer-actions > .speech-control, .composer-actions > details')]
                .map(el => el.getBoundingClientRect());
              const send = form.querySelector('#composer-controls button[type=submit]');
              return {
                chips: chips.filter(c => c.width >= 29 && c.right <= edge).length,
                sendOnScreen: send.getBoundingClientRect().right <= window.innerWidth,
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )
        await browser.close()

    assert layout == {"pickerSize": [30, 29], "pickerClear": True, "chipsWhole": True,
                      "addedHeight": 0, "micSize": [30, 29], "sendOnScreen": True,
                      "overflow": False}
    assert started == {"lang": "fi-FI", "calls": ["start"], "continuous": True,
                       "interim": True, "pressed": "true", "status": "Listening · Suomi",
                       "focused": "inject-message"}
    assert interim == "Fix the login bug"
    # Another result in the same run is not a pause, so no full stop (#2469).
    assert heard == "Fix the login bug today"
    assert inputs == 2
    assert after_typing == "Fix the login bug today, please and add a test"
    assert stopped == {"calls": ["start", "stop"], "pressed": "false"}
    assert len(sent) == 1 and "Fix the login bug today, please and add a test. Now" in sent[0]
    assert send_calls == ["start", "abort"]
    assert after_send == ""
    assert error == {"text": "Microphone blocked", "shown": True, "pressed": "false"}
    assert android == "Okay it works but I cannot"
    assert switched == {"recognizers": 5, "previous": ["start", "stop"], "lang": "en-US",
                        "calls": ["start"], "pressed": "true", "status": "Listening · English"}
    assert after_switch == "Okay it works but I cannot. Change the language"
    assert switched_setting == "en-US"
    assert chosen == "fi-FI"
    assert remembered == "fi-FI"
    assert bare_visible is False
    assert crowded == {"chips": 4, "sendOnScreen": True, "overflow": False}


def test_both_new_work_forms_offer_dictation(tmp_path, monkeypatch):
    """#2464: the front page's New work and a chat's "New work from this chat"."""
    from test_new_work import IssueRef, _app, _client

    app, _gh = _app(tmp_path, monkeypatch)
    key = "claude_code:test:sid1"
    app.state.app_state.add_issue(key, IssueRef("eerovil/agentdeck", 2400))
    client = _client(app)
    front = client.get("/").text
    chat = client.get(f"/sessions/{key}").text

    for page, box_id in ((front, "new-work"), (chat, "session-new-work")):
        box = page.split(f'id="{box_id}"', 1)[1].split("</details>", 1)[0]
        actions = box.split('class="composer-actions"', 1)[1].split("</form>", 1)[0]
        assert "data-speech-button" in actions


@pytest.mark.browser
async def test_speech_fills_the_new_work_form_and_stops_when_it_is_filed(
    tmp_path, monkeypatch
):
    """On the front page, the New work 🎤 writes into New work's text, adds no row,
    and filing aborts listening so nothing refills the cleared box."""
    from conftest import async_playwright, wait_until
    from test_new_work import _app, _creates
    from test_web import _live_server

    app, gh = _app(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block",
        )
        await context.add_init_script(_FAKE_RECOGNITION)
        page = await context.new_page()
        await page.goto(f"{base_url}/")
        box = page.locator("details#new-work")
        await box.locator("summary").click()
        form = box.locator("form")
        mic = form.locator("[data-speech-button]")
        await mic.wait_for(state="visible")

        layout = await form.evaluate(
            """form => {
              const actions = form.querySelector('.composer-actions');
              const control = actions.querySelector('.speech-control');
              const withMic = actions.getBoundingClientRect().height;
              control.style.display = 'none';
              const without = actions.getBoundingClientRect().height;
              control.style.display = '';
              const file = actions.querySelector('button[type=submit]').getBoundingClientRect();
              return {
                addedHeight: Math.round(withMic - without),
                fileOnScreen: file.right <= window.innerWidth,
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )

        message = form.locator('textarea[name="message"]')
        await message.fill("Usage chart")
        await mic.click()
        await page.evaluate("__speech.emit([['labels overlap on phone', true]])")
        heard = await message.input_value()

        await page.evaluate("__speech.emit([['labels overlap on phone', true], [' now', false]])")
        await form.locator('button[type="submit"]').click()
        await box.locator('[data-new-work-receipt="sent"]').wait_for()
        await wait_until(page, "the New work form cleared after filing",
                         """() => document.querySelector(
                              '#new-work textarea[name=message]').value === ''""")
        calls = await page.evaluate("__speech.last().calls")
        await page.evaluate("__speech.emit([[' late', true]])")
        after = await message.input_value()
        await browser.close()

    assert layout == {"addedHeight": 0, "fileOnScreen": True, "overflow": False}
    assert heard == "Usage chart labels overlap on phone"
    assert calls == ["start", "abort"]
    assert after == ""
    ((_argv, stdin, _repo),) = _creates(gh)
    assert json.loads(stdin)["body"].startswith("Usage chart labels overlap on phone now")


@pytest.mark.browser
async def test_speech_keeps_listening_when_the_browser_ends_a_phrase(tmp_path):
    """#2464: a browser that ends recognition after a pause or a silence is restarted,
    the next words land after what was heard, and only a recognizer that keeps ending
    at once with nothing heard is given up on, saying so."""
    from conftest import async_playwright
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=500)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_FAKE_RECOGNITION)
        page = await context.new_page()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        mic = page.locator("form.inject-form [data-speech-button]")
        await mic.wait_for(state="visible")
        box = page.locator("#inject-message")
        state = """() => ({
          value: document.getElementById('inject-message').value,
          calls: __speech.last().calls.slice(), made: __speech.made.length,
          pressed: document.querySelector('form.inject-form [data-speech-button]')
            .getAttribute('aria-pressed'),
          status: document.querySelector('form.inject-form .speech-status').hidden
            ? '' : document.querySelector('form.inject-form .speech-status').textContent })"""

        await box.fill("Fix")
        await mic.click()
        await page.evaluate("__speech.emit([['the login bug', true]])")
        # The browser ends after a phrase, then reports silence and ends again.
        await page.evaluate("__speech.last().onend()")
        await page.evaluate("__speech.fail('no-speech'); __speech.last().onend()")
        paused = await page.evaluate(state)
        # A new run numbers its results from zero; they go after what was heard.
        await page.evaluate("__speech.emit([['and add a test', true]])")
        resumed = await page.evaluate(state)

        # Ending at once, again and again, with nothing heard is given up on.
        for _ in range(3):
            await page.evaluate("__speech.last().onend()")
        given_up = await page.evaluate(state)
        await browser.close()

    assert paused == {"value": "Fix the login bug.", "calls": ["start"] * 3, "made": 1,
                      "pressed": "true", "status": "Listening · Suomi"}
    assert resumed["value"] == "Fix the login bug. And add a test"
    assert given_up == {"value": "Fix the login bug. And add a test.",
                        "calls": ["start"] * 5, "made": 1, "pressed": "false",
                        "status": "Dictation stopped"}


@pytest.mark.browser
async def test_speech_lands_a_sentence_once_however_the_browser_re_reports_it(tmp_path):
    """#2464: a final version with punctuation, one that corrects the last word (or,
    #2472, a word mid-sentence), a shorter re-report, and a sentence repeated after a restart
    each land once, while a new phrase that merely starts with the same letters is kept."""
    from conftest import async_playwright
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=500)
    cases = {
        "punctuation": ("[['okay', true], ['okay it', true], ['Okay, it works.', true]]",
                        "Okay, it works."),
        "corrected": ("[['okay if', true], ['okay it works', true]]", "Okay it works"),
        "shorter": ("[['okay it works', true], ['okay it', true]]", "Okay it works"),
        "prefix-letters": ("[['go', true], ['good morning', true]]", "Go good morning"),
        "desktop": ("[['fix the', true], [' login bug', true]]", "Fix the login bug"),
        # #2472: Chrome on Android re-reported whole sentences with a word
        # corrected in the middle, and each landed twice.
        "corrected-mid": (
            "[['no pituus riippuu siitä että kuin monta kuvaa siellä nyt on', true],"
            " ['no pituus riippuu siitä että kuinka monta kuvaa siellä nyt on ja', true]]",
            "No pituus riippuu siitä että kuinka monta kuvaa siellä nyt on ja"),
        "corrected-same-length": (
            "[['videot kaikkiin videot säilytetään 20 sekuntia', true],"
            " ['videot kaikki videot säilytetään 20 sekuntia', true]]",
            "Videot kaikki videot säilytetään 20 sekuntia"),
        "same-opening": ("[['fix the login bug', true], ['fix the test and docs', true]]",
                         "Fix the login bug fix the test and docs"),
        "short-new": ("[['fix the', true], ['add the tests', true]]",
                      "Fix the add the tests"),
        "near-identical": ("[['i like apples today', true], ['i like oranges today', true]]",
                           "I like apples today i like oranges today"),
    }
    got = {}
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_FAKE_RECOGNITION)
        page = await context.new_page()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        mic = page.locator("form.inject-form [data-speech-button]")
        await mic.wait_for(state="visible")
        box = page.locator("#inject-message")
        for name, (results, _want) in cases.items():
            await box.fill("")
            await mic.click()
            await page.evaluate(f"__speech.emit({results})")
            got[name] = await box.input_value()
            await mic.click()

        # The browser ends after a sentence and the next run repeats it, grown.
        await box.fill("")
        await mic.click()
        await page.evaluate("__speech.emit([['okay it works', true]])")
        await page.evaluate("__speech.last().onend()")
        await page.evaluate("__speech.emit([['okay it works fine', true], ['thanks', true]])")
        got["after-restart"] = await box.input_value()
        await browser.close()

    want = {name: text for name, (_results, text) in cases.items()}
    # The next run re-reports the sentence grown, so it was not finished yet.
    want["after-restart"] = "Okay it works fine thanks"
    assert got == want


@pytest.mark.browser
async def test_speech_ends_each_phrase_with_a_full_stop(tmp_path):
    """#2469: the browser's own punctuation is asked for where it exists; otherwise a
    phrase the browser paused after gets a full stop and the next a capital, the phrase
    still being heard stays open, a mark the browser added is kept, and dictating into the middle
    of a sentence neither capitalises it nor splits it with a full stop."""
    from conftest import async_playwright
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=500)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_FAKE_RECOGNITION)
        page = await context.new_page()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        mic = page.locator("form.inject-form [data-speech-button]")
        await mic.wait_for(state="visible")
        box = page.locator("#inject-message")
        got = {}

        # The phone's Chrome today: no setting, and no punctuation. The issue's own
        # words, Android-style, the browser ending after every pause.
        await mic.click()
        got["asked"] = await page.evaluate("'unspokenPunctuation' in __speech.last()")
        for phrase in ("moi haluaisin nyt kokeilla tätä", "toimiiko", "ei toimi",
                       "Aa toimii sittenkin joo"):
            await page.evaluate(f"__speech.emit([['{phrase}', true]])")
            got.setdefault("open", await box.input_value())
            await page.evaluate("__speech.last().onend()")
        await page.evaluate("__speech.emit([['älä', true], ['älä lopeta', false]])")
        got["listening"] = await box.input_value()
        await mic.click()
        got["issue"] = await box.input_value()

        # A mark the browser added stays, and a comma leaves the next word small.
        await box.fill("")
        await mic.click()
        for phrase in ("is it done?", "well,"):
            await page.evaluate(f"__speech.emit([['{phrase}', true]])")
            await page.evaluate("__speech.last().onend()")
        await page.evaluate("__speech.emit([['almost', true]])")
        await mic.click()
        got["own-marks"] = await box.input_value()

        # Into the middle of a sentence: no capital, and no full stop before the rest.
        await box.fill("Fix  soon")
        await box.evaluate("el => el.setSelectionRange(4, 4)")
        await mic.click()
        await page.evaluate("__speech.emit([['the login bug', true]])")
        await mic.click()
        got["middle"] = await box.input_value()

        # A browser that has the setting is asked to punctuate.
        await page.evaluate(
            "window.SpeechRecognition.prototype.unspokenPunctuation = false")
        await box.fill("")
        await mic.click()
        got["enabled"] = await page.evaluate(
            "Object.prototype.hasOwnProperty.call(__speech.last(), 'unspokenPunctuation')"
            " && __speech.last().unspokenPunctuation")
        await mic.click()
        await browser.close()

    assert got == {
        "asked": False,
        "open": "Moi haluaisin nyt kokeilla tätä",
        "listening": ("Moi haluaisin nyt kokeilla tätä. Toimiiko. Ei toimi. "
                      "Aa toimii sittenkin joo. Älä lopeta"),
        "issue": ("Moi haluaisin nyt kokeilla tätä. Toimiiko. Ei toimi. "
                  "Aa toimii sittenkin joo. Älä lopeta."),
        "own-marks": "Is it done? Well, almost.",
        "middle": "Fix the login bug soon",
        "enabled": True,
    }


@pytest.mark.browser
async def test_speech_ends_a_phrase_after_a_short_silence_on_desktop(tmp_path):
    """#2480: desktop Chrome keeps listening through a pause, so 0.6 s of silence after
    a final result is the pause. Results arriving back to back stay one sentence, a
    re-guess of a word already heard does not restart the count, new words do, and
    stopping or sending leaves no full stop to land later."""
    from conftest import async_playwright
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=500)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_FAKE_RECOGNITION)
        page = await context.new_page()
        await page.clock.install()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        mic = page.locator("form.inject-form [data-speech-button]")
        await mic.wait_for(state="visible")
        box = page.locator("#inject-message")
        await page.clock.pause_at(await page.evaluate("Date.now()") + 1000)
        got = {}

        # Desktop Chrome: one run, each phrase reported once, never an `end`.
        await mic.click()
        await page.evaluate("__speech.emit([['fix the', true]])")
        await page.clock.run_for(300)
        await page.evaluate("__speech.emit([['fix the', true], [' login bag', true]])")
        await page.clock.run_for(599)
        got["no-pause-yet"] = await box.input_value()
        # Chrome re-guesses a word it already heard: not speech, so the count goes on.
        await page.evaluate("__speech.emit([['fix the', true], [' login bug', true]])")
        await page.clock.run_for(1)
        got["paused"] = await box.input_value()
        # New words, still being heard, then final: the next sentence.
        await page.evaluate(
            "__speech.emit([['fix the', true], [' login bug', true], [' and add', false]])")
        await page.clock.run_for(700)
        got["still-speaking"] = await box.input_value()
        await page.evaluate(
            "__speech.emit([['fix the', true], [' login bug', true], [' and add a test', true]])")
        await page.clock.run_for(600)
        got["second"] = await box.input_value()
        await mic.click()

        # Sending inside the 0.6 s leaves nothing to fire into the cleared composer.
        await box.fill("")
        await mic.click()
        await page.evaluate("__speech.emit([['ship it', true]])")
        await page.evaluate("document.querySelector('form.inject-form').requestSubmit()")
        await box.fill("")
        await page.clock.run_for(1000)
        got["after-send"] = await box.input_value()
        await browser.close()

    assert got == {
        "no-pause-yet": "Fix the login bag",
        "paused": "Fix the login bug.",
        "still-speaking": "Fix the login bug. And add",
        "second": "Fix the login bug. And add a test.",
        "after-send": "",
    }


# Speech out for voice mode: `window.__voice.spoken` collects every utterance, and
# each one ends at once so the flow moves on.
_FAKE_SYNTHESIS = """
(() => {
  const spoken = [];
  class FakeUtterance { constructor(text) { this.text = text; } }
  window.SpeechSynthesisUtterance = FakeUtterance;
  Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
    speak(u) {
      // A browser that will not speak here never starts the utterance, or fails it.
      if (window.__voice.mute) return;
      if (window.__voice.refuse) {
        setTimeout(() => u.onerror({ error: 'not-allowed' }), 0);
        return;
      }
      spoken.push(u.text);
      u.onstart && u.onstart();
      if (window.__voice.hold) held.push(u); else setTimeout(() => u.onend && u.onend(), 0);
    },
    cancel() { held.length = 0; }, getVoices() { return []; },
  } });
  const held = [];
  window.__voice = { spoken, hold: false, held, mute: false, refuse: false };
})();
"""

# A turn as the live stream swaps it in: the status fragment, then the reply.
_TURN = """
(([state, reply]) => {
  if (reply) {
    const transcript = document.querySelector('.transcript');
    const row = document.createElement('div');
    row.id = 'event-' + Math.random().toString(36).slice(2);
    row.className = 'ev assistant';
    row.innerHTML = '<div class="ev-text"></div>';
    // Raw markdown, or HTML as the page's markdown pass leaves it.
    const text = row.querySelector('.ev-text');
    if (/<br>/.test(reply)) text.innerHTML = reply; else text.textContent = reply;
    transcript.appendChild(row);
    transcript.dispatchEvent(new CustomEvent('htmx:sseMessage', { bubbles: true }));
  }
  // The stream re-sends the composer's controls on every turn change: Stop while working.
  document.getElementById('composer-controls').innerHTML = (state === 'thinking'
    ? '<button class="stop-button" type="submit" form="interrupt-form">Stop</button>' : '')
    + '<button type="submit">Send</button>';
  // 'background': still thinking, but only on work the ended turn left running (#3529).
  const tag = state === 'background'
    ? '<span class="status-tag thinking" data-background>thinking</span>'
    : `<span class="status-tag ${state}">${state}</span>`;
  const status = document.getElementById('session-status');
  status.innerHTML = `<div class="session-head">${tag}</div>`;
  status.dispatchEvent(new CustomEvent('htmx:sseMessage', { bubbles: true }));
})
"""

# Rows landing in the transcript in one swap: a tool call, or a message as the deck
# renders one it delivered ('user:<its action ID>', or 'user:' for one sent elsewhere).
_ROW = """
((kinds) => {
  const transcript = document.querySelector('.transcript');
  [].concat(kinds).forEach((kind) => {
    const row = document.createElement('div');
    const user = kind.startsWith('user:');
    row.className = user ? 'ev user' : 'ev tool tool-call';
    if (user) row.id = 'event-' + Math.random().toString(36).slice(2);
    if (user && kind.length > 5) row.dataset.observedActionId = kind.slice(5);
    row.innerHTML = '<div class="ev-text">' + (user ? 'message 🎧' : 'tool') + '</div>';
    transcript.appendChild(row);
  });
  transcript.dispatchEvent(new CustomEvent('htmx:sseMessage', { bubbles: true }));
})
"""


# How long the chat may stay quiet before the fast agent speaks (#3799), as "<after a
# send> <while it works>" in seconds.
_QUIET = """
((seconds) => document.querySelector('form.inject-form .voice-control')
  .setAttribute('data-voice-quiet-seconds', seconds))
"""


def _action_id(body):
    """The action ID a sent form carried, urlencoded or multipart."""
    import re

    return re.search(r'client_action_id"?(?:=|\r\n\r\n)([0-9a-f-]{36})', body).group(1)


class _HostVoice:
    """The deck's speech service for voice mode, faked at its two routes. `said` is
    every text read aloud, as asked for; `hear(text)` speaks into the fake microphone
    (`_FAKE_MICROPHONE`), and the host transcribes that phrase as `text`. A phrase
    with nothing queued is heard as no words. `hold` reads for 20 s (or that many
    seconds), and `hang`
    never answers a reading, as a browser that will not play in the background.
    `wait`, an event, holds each transcription until it is set. A phrase in `doubted`
    comes back as wholly unsure (#3817)."""

    def __init__(self, page):
        self.page = page
        self.said, self.phrases, self.queue, self.hung = [], [], [], []
        self.languages, self.narrated, self.doubted = [], [], set()
        self.narration = ""
        self.hold = self.hang = False
        self.wait = None

    async def install(self):
        await self.page.route("**/api/speech/say", self._say)
        await self.page.route("**/api/speech/transcribe*", self._transcribe)
        await self.page.route("**/api/voice/narrate", self._narrate)

    async def _narrate(self, route):
        # The fast agent on the chat's quiet work (#3799): `narration` is its line,
        # empty for nothing to say.
        self.narrated.append(json.loads(route.request.post_data))
        if not self.narration:
            await route.fulfill(status=204)
            return
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"say": self.narration}))

    async def _say(self, route):
        body = json.loads(route.request.post_data)
        self.said.append(body["text"])
        self.languages.append(body["language"])
        if self.hang:
            self.hung.append(route)
            return
        hold = 20 if self.hold is True else self.hold
        body = _silent_wav(seconds=hold or 0.2)
        await route.fulfill(status=200, body=body, content_type="audio/wav")

    async def _transcribe(self, route):
        text = self.queue.pop(0) if self.queue else ""
        self.phrases.append(text)
        if self.wait:
            await self.wait.wait()
        parts = [{"text": text, "unsure": text in self.doubted}]
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"text": text, "language": "en", "segments": parts}))

    async def sound(self, ms=400):
        await self.page.evaluate("__mic.level(0.5)")
        await self.page.wait_for_timeout(ms)
        await self.page.evaluate("__mic.level(0)")

    async def hear(self, text, ms=400):
        """Says `text` and returns once the host has been asked for it."""
        self.queue.append(text)
        asked = len(self.phrases)
        await self.sound(ms)
        for _ in range(60):
            if len(self.phrases) > asked:
                return
            await self.page.wait_for_timeout(50)
        raise AssertionError(f"the host was never asked for {text!r}")


async def _voice_page(playwright, base_url, *, phone=True, scripts=()):
    """A page on the test chat with a fake microphone and the fake speech service."""
    browser = await playwright.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    # A phone: a wide page mounts the front list beside the chat after load, and
    # that navigation turns voice mode off if the tap came first.
    sizes = {"viewport": {"width": 360, "height": 780}} if phone else {}
    context = await browser.new_context(service_workers="block", **sizes)
    await context.add_init_script(_FAKE_MICROPHONE)
    for script in scripts:
        await context.add_init_script(script)
    page = await context.new_page()
    host = _HostVoice(page)
    await host.install()
    return browser, page, host


def _host_voice_app(tmp_path, **kwargs):
    from test_inject import _web_app

    app = _web_app(tmp_path, **kwargs)
    app.state.templates.env.globals["host_speech"] = True
    return app


_VOICE_STATE = ("() => document.querySelector('[data-voice-button]')"
                ".getAttribute('data-voice-state')")
_LISTENING = f"() => ({_VOICE_STATE})() === 'listening'"
# The chat composer's 🎤 is voice mode's microphone with a speech service (#3787):
# with voice_mode.js kept off the page, it dictates as on the forms that still do.
_VOICE_MODE_JS = "**/static/voice_mode.js*"


async def _voice_on(page):
    """Voice mode reading aloud (🎧) and listening to everything (🎤's Always)."""
    await page.locator("form.inject-form [data-voice-button]").click()
    await _pick(page, "open")


async def _pick(page, mode):
    """🎤's long-press menu (#3787): 'jarvis', 'open' (Always) or 'off'."""
    item = page.locator(f'form.inject-form [data-voice-mic-mode="{mode}"]')
    # A page still settling (the desktop list mounting beside the chat) can swap the
    # composer under the first press.
    for _ in range(3):
        await _hold(page, page.locator("form.inject-form [data-voice-mic]"))
        if await item.is_visible():
            break
    await item.click()


async def _hold(page, locator):
    """A long press: on 🎤 it opens the menu of how it listens (#3787)."""
    box = await locator.bounding_box()
    await page.mouse.move(box["x"] + 5, box["y"] + 5)
    await page.mouse.down()
    await page.wait_for_timeout(800)
    await page.mouse.up()


@pytest.mark.browser
async def test_voice_mode_speaks_the_marked_paragraph_and_sends_what_it_hears(tmp_path):
    """Turned on, it says so and listens; a finished turn's 🔊 paragraph is read and
    nothing else; a second of silence after the words sends them with the
    instruction line; "cancel" sends nothing; mid-turn "stop" presses Stop and other
    words go as a follow-up; a reply with no 🔊 paragraph is read without its code."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server

    from agentdeck.voice_mode import SHORT_ANSWER_LINE, UNHEARD_LINE, VOICE_LINE

    app = _host_voice_app(tmp_path, max_message_chars=1000)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser, page, host = await _voice_page(playwright, base_url)
        said = host.said
        sent, stops = [], []
        # A script error, such as a call to a removed helper (#3780), fails the test.
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def fulfil(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        async def stop(route):
            stops.append(route.request.url)
            await route.fulfill(status=202, body='<div id="inject-result"></div>',
                                content_type="text/html")

        async def sends(count):
            for _ in range(60):
                if len(sent) >= count:
                    return
                await page.wait_for_timeout(50)

        await page.route("**/sessions/codex:test:sid/inject", fulfil)
        await page.route("**/sessions/codex:test:sid/interrupt", stop)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        button = page.locator("form.inject-form [data-voice-button]")
        await button.wait_for(state="visible")
        mic = page.locator("form.inject-form [data-voice-mic]")
        # The fast agent speaking for a quiet chat (#3799) is tested on its own below.
        await page.evaluate(_QUIET + "('60 600')")
        # Turned on during a turn it did not see start, which counts as unheard (#3520).
        await page.evaluate(_TURN + "(['thinking', null])")
        await _voice_on(page)
        await wait_until(page, "it listens after saying it is on", _LISTENING)
        started = [said[:], await button.get_attribute("aria-pressed")]
        # The page's own stream can swap the status back meanwhile; the turn is still
        # one this page did not start.
        await page.evaluate(_TURN + "(['thinking', null])")

        # Words heard, then silence: the composer's own Send carries them and the line.
        await host.hear("add a test for it")
        await sends(1)
        # "cancel" alone sends nothing.
        await wait_until(page, "it listens after the acknowledgement", _LISTENING)
        await host.hear("cancel")
        await page.wait_for_timeout(1300)
        after_cancel = len(sent)

        # The agent works: "stop" presses Stop, other words go as a follow-up.
        await page.evaluate(_TURN + "(['thinking', null])")
        await host.hear("stop")
        await page.wait_for_timeout(1300)
        await host.hear("also check the docs")
        await sends(2)

        # The agent's first 🔊 note is read while it works; a second one is not.
        await wait_until(page, "it listens after the acknowledgement", _LISTENING)
        said_before = len(said)
        await page.evaluate(_TURN + "(['thinking', '🔊 On it, checking the logs.'])")
        await _until(page, "the note was read", lambda: len(said) > said_before)
        await page.evaluate(_TURN + "(['thinking', '🔊 Still going.'])")
        await page.wait_for_timeout(300)
        noted = said[said_before:]
        await wait_until(page, "it listens after the note", _LISTENING)

        # The turn ends with a marked reply: only its 🔊 paragraph is read. Words still
        # being said when the turn ends are not cut off: they wait for their own
        # silence, and the reply waits for them. They go with the unheard warning, which
        # asks the agent to say again what it said, so that reply is never read; the
        # next turn's is (#3771).
        said_before = len(said)
        sent_before = len(sent)
        host.queue.append("and the changelog")
        await page.evaluate("__mic.level(0.5)")
        await page.evaluate(_TURN + "(['idle', null])")
        await page.wait_for_timeout(400)
        await page.evaluate("__mic.level(0)")
        await page.wait_for_timeout(600)
        early = len(sent) - sent_before
        # The reply lands 2 s after the status, inside the 4 s window.
        await page.wait_for_timeout(1000)
        await page.evaluate(_TURN + "(['idle', '🔊 Said while they spoke.'])")
        await sends(sent_before + 1)
        await page.wait_for_timeout(600)
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(
            _TURN + """(['idle', '<pre><code>🔊 rm -rf build</code></pre>'
                         + '🔊 Done. The `auth` test passes.<br><br>Details: see src/app.py.'])"""
        )
        await _until(page, "the reply was read",
                     lambda: "The test passes." in said[said_before:])
        marked = said[said_before:]

        # A reply without one is read from its first prose paragraph, never its code.
        await wait_until(page, "it listens after the reply", _LISTENING)
        await page.evaluate(_TURN + "(['thinking', null])")
        said_before = len(said)
        await page.evaluate(
            _TURN + """(['idle', 'Fixed it in https://x.test/a, /var/app.py and src/parser.py'
                         + ' with <code>parse_all()</code> and/or a retry.<br><br>'
                         + '<pre><code>rm -rf build</code></pre>'])"""
        )
        for _ in range(120):
            if len(said) > said_before:
                break
            await page.wait_for_timeout(50)
        unmarked = " ".join(said[said_before:])

        # A short turn whose marked reply lands before the status leaves "thinking"
        # is read once, as it lands, and not skipped or repeated at the turn's end.
        await page.evaluate(_TURN + "(['thinking', null])")
        await wait_until(page, "it listens during the short turn", _LISTENING)
        said_before = len(said)
        await page.evaluate(_TURN + "(['thinking', '🔊 All green.<br><br>Details below.'])")
        await page.evaluate(_TURN + "(['idle', null])")
        await page.wait_for_timeout(4500)
        short_turn = said[said_before:]

        # A turn so short the page never sees it start: the reply to a message sent
        # from here is still read, not skipped for "Hey Jarvis" (#3506).
        await wait_until(page, "it listens before the unseen turn", _LISTENING)
        await host.hear("how much would it cost")
        await sends(4)
        await wait_until(page, "it listens after the acknowledgement", _LISTENING)
        said_before = len(said)
        await page.evaluate(_TURN + "(['idle', '🔊 About fifty euros.'])")
        await _until(page, "the unseen turn's reply was read",
                     lambda: "About fifty euros." in said[said_before:])
        unseen_turn = said[said_before:]

        # A turn this page did not start (a wake): what is said during it tells the
        # agent the person had not heard it, naming the last 🔊 line read (#3520).
        await wait_until(page, "it listens before the woken turn", _LISTENING)
        await page.evaluate(_TURN + "(['thinking', null])")
        await host.hear("yes do it")
        await sends(5)
        await wait_until(page, "it listens after the acknowledgement", _LISTENING)
        await page.evaluate(_TURN + "(['idle', '🔊 Woken turn done.'])")
        await _until(page, "the woken turn's reply was read",
                     lambda: "Woken turn done." in said)

        # A turn that ends with a helper agent still running in the background: the
        # chat stays "thinking", but its reply is read now, not held as mid-turn text
        # until the agent finishes (#3529).
        await wait_until(page, "it listens before the background turn", _LISTENING)
        await page.evaluate(_TURN + "(['thinking', null])")
        said_before = len(said)
        await page.evaluate(_TURN + "(['background', 'The phone agent is on it.'])")
        await _until(page, "the reply was read while the agent runs",
                     lambda: "The phone agent is on it." in said[said_before:])
        background_turn = said[said_before:]
        await page.evaluate(_TURN + "(['idle', null])")

        # Nothing is said at a send but its tone (#3799): the chat says it in its own
        # words. Only when it stays quiet while it works does the fast agent speak, here
        # 0.5 s after a send and then every 1.5 s, saying what the agent does; with
        # nothing from it, its own line for the phrase or "Got it, working on it.".
        await page.evaluate(_QUIET + "('0.5 1.5')")
        # The voice log is told which of the fast agent's lines were really said.
        spoken = []

        async def spoken_route(route):
            body = json.loads(route.request.post_data)
            if "spoken" in body:
                spoken.append(body["spoken"])
            await route.fulfill(status=204)

        async def tests_too(route):
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"say": "The tests, got it.", "relay": "check the tests too"}))

        await page.route("**/api/voice/sent", spoken_route)
        await wait_until(page, "it listens before the message", _LISTENING)
        said_before = len(said)
        await host.hear("look at the logs")
        await page.wait_for_timeout(1500)
        idle_send = said[said_before:]
        await wait_until(page, "it listens before the second message", _LISTENING)
        said_before = len(said)
        await page.route("**/api/voice/heard", tests_too)
        await host.hear("check the tests too")
        await sends(len(sent) + 1)
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['thinking', '🔊 Checking the tests.'])")
        await page.wait_for_timeout(1000)
        own_note = said[said_before:]
        await wait_until(page, "it listens mid-turn", _LISTENING)

        async def readme(route):
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"say": "The readme, got it.", "relay": "and the readme"}))

        await page.unroute("**/api/voice/heard")
        await page.route("**/api/voice/heard", readme)
        said_before = len(said)
        narrated_before = len(host.narrated)
        await host.hear("and the readme")
        await _until(page, "the quiet turn was spoken for", lambda: len(said) > said_before)
        host.narration = "I'm reading the readme."
        await _until(page, "the long quiet was spoken for", lambda: len(said) > said_before + 1)
        # Nothing new to say on the next long quiet spell: still a line.
        host.narration = ""
        await _until(page, "the next long quiet was spoken for",
                     lambda: len(said) > said_before + 2)
        quiet_turn = said[said_before:]
        quiet_asks = host.narrated[narrated_before:]
        await page.unroute("**/api/voice/heard")
        await page.evaluate(_TURN + "(['idle', null])")
        narrated_at_end = len(host.narrated)
        await page.wait_for_timeout(2000)
        after_end = len(host.narrated) - narrated_at_end
        await page.unroute("**/api/voice/sent")
        await page.evaluate(_QUIET + "('60 600')")

        # The fast agent hears it first (#3444): its words are spoken at once in place
        # of the acknowledgement, and its cleaned-up text is what is sent. Noise it
        # drops sends nothing and says nothing, whatever it answered. The 🔊 lines
        # read aloud go with the next phrase. While it thinks, nothing more is heard,
        # and what is typed meanwhile is kept.
        fast = []

        async def fast_route(route):
            body = json.loads(route.request.post_data)
            fast.append(body)
            if "watching" in body["text"]:
                answer = {"say": "Okay.", "relay": None}
            else:
                await asyncio.sleep(0.8)
                answer = {"say": "Starting on the login bug.", "relay": "Fix the login bug."}
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(answer))

        await page.route("**/api/voice/heard", fast_route)
        # And then says what it really did with each phrase, for the voice log (#3475).
        reports = []

        async def sent_route(route):
            reports.append(json.loads(route.request.post_data))
            await route.fulfill(status=204)

        await page.route("**/api/voice/sent", sent_route)
        await wait_until(page, "it listens before the reply", _LISTENING)
        await page.evaluate(_TURN + "(['idle', '🔊 Ready for more.'])")
        for _ in range(120):
            if said[-1] == "Ready for more.":
                break
            await page.wait_for_timeout(50)
        await wait_until(page, "it listens before the fast agent", _LISTENING)
        sent_before = len(sent)
        said_before = len(said)
        await host.hear("fix the log in bag")
        while not fast:
            await page.wait_for_timeout(50)
        waiting_state = await page.evaluate(f"({_VOICE_STATE})()")
        await page.evaluate("""() => {
            const box = document.querySelector('form.inject-form textarea');
            box.value += ' please'; box.dispatchEvent(new Event('input', { bubbles: true })); }""")
        await _until(page, "the fast agent's words were sent", lambda: len(sent) > sent_before)
        await page.wait_for_timeout(500)
        fast_spoken = said[said_before:]
        fast_sent = sent[sent_before:]
        await wait_until(page, "it listens after the fast agent", _LISTENING)
        noise_before = len(said)
        await host.hear("thanks for watching")
        await page.wait_for_timeout(1600)
        noise_spoken = said[noise_before:]
        noise_sent = len(sent) - sent_before - len(fast_sent)
        noise_draft = await page.evaluate(
            "document.querySelector('form.inject-form textarea').value")
        # Sent by hand while the fast agent thinks: that send wins, and the answer
        # does not send the draft a second time (#3448).
        await wait_until(page, "it listens before the hand send", _LISTENING)
        hand_before = len(sent)
        asked_before = len(fast)
        await host.hear("check the readme")
        while len(fast) == asked_before:
            await page.wait_for_timeout(50)
        await page.locator("#composer-controls button[type=submit]:not(.stop-button)").click()
        await page.wait_for_timeout(1500)
        hand_sent = sent[hand_before:]
        await page.unroute("**/api/voice/heard")
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 Done.'])")
        await page.wait_for_timeout(2500)

        # A wake that ends while the fast agent thinks: what was unheard when the
        # words ended still goes with them, and a bare "go ahead" asks for a
        # confirmation (#3520).
        async def wake_route(route):
            await page.evaluate(_TURN + "(['idle', null])")
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"say": "", "relay": "go ahead"}))

        await page.route("**/api/voice/heard", wake_route)
        await wait_until(page, "it listens before the wake", _LISTENING)
        wake_before = len(sent)
        await page.evaluate(_TURN + "(['thinking', null])")
        await host.hear("go ahead")
        await sends(wake_before + 1)
        wake_sent = sent[wake_before:]
        await page.unroute("**/api/voice/heard")

        # A wake that starts and replies after the words ended, while the fast agent
        # thinks: the reply is held until the words are sent, which carry the
        # warning (#3520). The agent is told to say again what it said, so the held
        # reply is never read: only the new reply is (#3771).
        async def late_wake_route(route):
            await page.evaluate(_TURN + "(['thinking', null])")
            await page.evaluate(_TURN + "(['idle', '🔊 Shall I pause the batch?'])")
            await asyncio.sleep(2)
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"say": "", "relay": "yes approve that one"}))

        await page.route("**/api/voice/heard", late_wake_route)
        await wait_until(page, "it listens before the late wake", _LISTENING, timeout=6000)
        late_before = len(sent)
        late_said = len(said)
        await host.hear("yes approve that one")
        await sends(late_before + 1)
        late_sent = sent[late_before:]
        await page.unroute("**/api/voice/heard")
        await page.wait_for_timeout(1500)
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 First, shall I pause the batch?'])")
        await _until(page, "the new reply was read",
                     lambda: "First, shall I pause the batch?" in said)
        late_heard = said[late_said:]

        # "lopeta kuuntelu" switches 🎤 to Hey Jarvis (#3540): words without it are let
        # go of, the agent starting a turn leaves it waiting, and after its reply it
        # waits again rather than listening.
        await wait_until(page, "it listens again", _LISTENING)
        stopped_sent = len(sent)
        await host.hear("Lopeta kuuntelu.")
        await wait_until(page, "it waits for Hey Jarvis", f"() => ({_VOICE_STATE})() === 'waiting'",
                         timeout=6000)
        stopped = [await page.evaluate(f"({_VOICE_STATE})()"),
                   await page.locator(".voice-control .speech-status").text_content(),
                   said[-1], len(sent) - stopped_sent]
        host.doubted |= {"just thinking aloud", "Hey Jarvis, check the build"}
        await host.hear("just thinking aloud")
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.wait_for_timeout(1200)
        stopped_working = [await page.evaluate(f"({_VOICE_STATE})()"), len(sent) - stopped_sent]
        await page.evaluate(_TURN + "(['idle', '🔊 Read while stopped.'])")
        await _until(page, "the reply was read", lambda: "Read while stopped." in said)
        await wait_until(page, "it waits after the reply",
                         f"() => ({_VOICE_STATE})() === 'waiting'", timeout=6000)
        read_stopped = "Read while stopped." in said
        # "Hey Jarvis" starts a message with no tap (#3500). Whisper doubted it all:
        # the words after the name go marked, not the doubt of a phrase let go of
        # before it (#3817).
        await host.hear("Hey Jarvis, check the build")
        await sends(stopped_sent + 1)
        jarvis_sent = sent[stopped_sent:]
        await wait_until(page, "it waits after the message",
                         f"() => ({_VOICE_STATE})() === 'waiting'", timeout=6000)
        # A long press on 🎤 opens the menu; Always listening listens at once.
        await _hold(page, mic)
        menu = await page.locator("[data-voice-mic-menu]").is_visible()
        await page.locator('[data-voice-mic-mode="open"]').click()
        await wait_until(page, "Always listens", _LISTENING)

        # A tap on 🎤 while it listens, with nothing heard, goes to Jarvis (#3787).
        await mic.click()
        to_jarvis = await page.evaluate(f"({_VOICE_STATE})()")
        # Off from the menu: the microphone is let go of, a reply is still read, and
        # nothing listens until 🎤 is tapped again.
        await _pick(page, "off")
        muted = await page.evaluate(
            f"""() => [({_VOICE_STATE})(), document.querySelector('[data-voice-mic]')
                       .hasAttribute('data-voice-hears'),
                       __mic.stream.getTracks().map((t) => t.readyState)]""")
        await page.evaluate(_TURN + "(['thinking', null])")
        said_before = len(said)
        await page.evaluate(_TURN + "(['idle', '🔊 Read while muted.'])")
        await _until(page, "the muted reply was read", lambda: len(said) > said_before)
        await wait_until(page, "nothing listens after it", f"() => ({_VOICE_STATE})() === 'idle'",
                         timeout=6000)
        muted_after = said[said_before:]
        await _pick(page, "open")
        await wait_until(page, "Always listens again", _LISTENING)

        # 🎧 tapped mid-reply only stops that reading: 🎧 stays on, the bubble says
        # so, and listening goes on (#3779).
        host.hold = True
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 Shall I stop the batch?'])")
        await wait_until(page, "it reads the newer question",
                         f"() => ({_VOICE_STATE})() === 'speaking'", timeout=6000)
        await button.click()
        host.hold = False
        await wait_until(page, "it listens after the stop", _LISTENING)
        hushed = [await button.get_attribute("aria-pressed"),
                  await page.locator(".voice-control .speech-status").text_content(),
                  await page.evaluate("document.documentElement.classList.contains('voice-on')")]
        # Tapped with nothing read, 🎧 goes off and listening goes on; a message then
        # asks for no 🔊 line and says nothing of what went unheard (#3540).
        await button.click()
        await wait_until(page, "it listens with reading off", _LISTENING)
        skip_before = len(sent)
        await host.hear("yes approve that one")
        await sends(skip_before + 1)
        skip_sent = sent[skip_before:]
        await wait_until(page, "it listens after the send", _LISTENING, timeout=6000)
        reading_off = await button.get_attribute("aria-pressed")

        # Both off: voice mode ends and lets go of the microphone.
        await _pick(page, "off")
        held_off = [await page.evaluate("document.documentElement.classList.contains('voice-on')"),
                    await page.evaluate("__mic.stream.getTracks().map((t) => t.readyState)")]

        # Gone quiet after the idle seconds, a turn's 🔊 note is read and it goes back
        # to waiting for "Hey Jarvis"; the turn's reply is read and then it listens (#3456).
        await page.evaluate("""() => document.querySelector('[data-voice-button]')
            .closest('.voice-control').setAttribute('data-voice-idle-seconds', '1')""")
        await _voice_on(page)
        await wait_until(page, "it waits after the idle second",
                         f"() => ({_VOICE_STATE})() === 'waiting'", timeout=6000)
        said_before = len(said)
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['thinking', '🔊 Checking the logs.'])")
        await _until(page, "the note was read", lambda: len(said) > said_before)
        await wait_until(page, "it waits again", f"() => ({_VOICE_STATE})() === 'waiting'",
                         timeout=6000)
        mid_turn = said[said_before:]
        await page.evaluate(_TURN + "(['idle', '🔊 Logs are clean.'])")
        await wait_until(page, "the turn's end listens", _LISTENING, timeout=6000)

        # The turn ends while its note is still read: the reply follows the note
        # before anything listens.
        await page.evaluate(_TURN + "(['thinking', null])")
        host.hold = 2.5
        said_before = len(said)
        await page.evaluate("""() => { window.__modes = [];
            document.addEventListener('agentdeck:voice-state',
              (e) => __modes.push(e.detail.mode)); }""")
        await page.evaluate(_TURN + "(['thinking', '🔊 Still checking.'])")
        await wait_until(page, "the note is being read",
                         f"() => ({_VOICE_STATE})() === 'speaking'", timeout=6000)
        host.hold = False
        await page.evaluate(_TURN + "(['idle', '🔊 All done.'])")
        await _until(page, "the reply was read", lambda: "All done." in said[said_before:],
                     timeout_ms=10000)
        await wait_until(page, "it listens after the reply", _LISTENING, timeout=6000)
        overlap = said[said_before:]
        modes = await page.evaluate("__modes")
        await browser.close()

    assert errors == []
    assert overlap == ["Still checking.", "All done."]
    reading = modes[modes.index("speaking"):modes.index("listening")]
    assert reading.count("speaking") == 2 and "waiting" not in reading
    assert mid_turn == ["Checking the logs."]
    assert started == [["Voice mode on."], "true"]
    assert after_cancel == 1
    assert "add+a+test+for+it" in sent[0] or "add a test for it" in sent[0]
    assert VOICE_LINE.split(" ")[0] in sent[0]
    assert len(stops) == 1
    assert len(sent) == 14
    assert "also+check+the+docs" in sent[1] or "also check the docs" in sent[1]
    # Only a message said with something unheard says so (#3520): the second 🔊 note
    # of a turn is not read, and the wake's turn was not started from here.
    wake = sent.index(wake_sent[0])
    late = sent.index(late_sent[0])
    skip = sent.index(skip_sent[0])
    assert [i for i, body in enumerate(sent) if "had not heard" in body] == [
        0, 1, 2, 4, wake, late]
    assert VOICE_LINE.split(" ")[0] not in skip_sent[0] and sent.index(skip_sent[0]) == skip
    assert "which was: “Done.”." in late_sent[0]
    assert "Shall I pause the batch?" not in late_heard, late_heard
    assert late_heard[-1] == "First, shall I pause the batch?", late_heard
    assert "which was: nothing yet." in sent[0]
    assert "which was: “Done.”." in wake_sent[0]
    assert [i for i, body in enumerate(sent) if SHORT_ANSWER_LINE in body] == [4, wake]
    assert "which was: “On it, checking the logs.”." in sent[2]
    tail = UNHEARD_LINE.split("}. ")[1]
    assert f"which was: “About fifty euros.”. {tail}\r\n\r\n{SHORT_ANSWER_LINE}" in sent[4]
    assert early == 0 and ("and+the+changelog" in sent[2] or "and the changelog" in sent[2])
    assert noted == ["On it, checking the logs."]
    assert short_turn == ["All green."]
    assert unseen_turn == ["About fifty euros."]
    assert background_turn == ["The phone agent is on it."], background_turn
    ack = "Got it, working on it."
    assert idle_send == [] and own_note == ["Checking the tests."]
    assert quiet_turn == ["The readme, got it.", "I'm reading the readme.",
                          "Still working on it."], quiet_turn
    # The chat's own note came first, so "The tests, got it." was never said, and the
    # voice log is not told it was.
    # Each line said goes on its quiet check's entry; the phrase's own line on the
    # phrase's entry too.
    assert spoken == ["The readme, got it.", "The readme, got it.", "I'm reading the readme.",
                      "Still working on it."]
    assert [(a["session_key"], a["run"] > "", a["lang"]) for a in quiet_asks[:2]] == [
        ("codex:test:sid", True, "en")] * 2
    assert 0 <= quiet_asks[0]["seconds"] <= 2 and quiet_asks[1]["seconds"] >= 1
    assert after_end == 0
    # Its line for the phrase is said only if the chat stays quiet (#3799).
    assert fast_spoken == []
    assert waiting_state != "listening"
    assert len(fast_sent) == 1 and ("Fix the login bug. please" in fast_sent[0]
                                    or "Fix+the+login+bug.+please" in fast_sent[0])
    assert "log in bag" not in fast_sent[0] and "log+in+bag" not in fast_sent[0]
    assert noise_sent == 0 and noise_draft == "" and noise_spoken == []
    assert len(hand_sent) == 1 and "readme" in hand_sent[0]
    assert [(r["id"], r["text"], r["how"]) for r in reports] == [
        (fast[0]["id"], "Fix the login bug. please", "sent"),
        (fast[1]["id"], None, "not sent: taken as noise"),
        (fast[2]["id"], None, "not sent: sent by hand meanwhile"),
        (reports[3]["id"], "go ahead", "sent"),
        (reports[4]["id"], "yes approve that one", "sent"),
        (reports[5]["id"], "[unsure: check the build]", "sent as heard: no answer in time"),
        (reports[6]["id"], "yes approve that one", "sent as heard: no answer in time"),
    ]
    assert [f["session_key"] for f in fast] == ["codex:test:sid"] * 3
    assert fast[0]["text"].lower().startswith("fix the log in bag")
    # Each 🔊 line goes once; the server keeps it whether or not an answer came.
    assert [f["aloud"] for f in fast] == [["Ready for more."], [], []]
    # Read a sentence at a time (#3480).
    assert [t for t in marked if t != ack] == ["Done.", "The test passes."], marked
    for code in ("rm -rf", "https", "/var", "src/", "parser.py", "parse_all", "auth"):
        assert code not in unmarked + " ".join(marked), code
    assert "and/or a retry" in unmarked
    assert unmarked.startswith("Fixed it in") and unmarked.endswith("More on screen.")
    assert stopped == ["waiting", "Say “Hey Jarvis” · tap 🎤 to talk",
                       "OK, only after Hey Jarvis.", 0]
    assert menu is True
    assert stopped_working == ["waiting", 0]
    assert read_stopped
    assert len(jarvis_sent) == 1 and ("check+the+build" in jarvis_sent[0]
                                      or "check the build" in jarvis_sent[0])
    assert "Jarvis" not in jarvis_sent[0]
    assert to_jarvis == "waiting"
    assert muted == ["idle", False, ["ended"]]
    assert muted_after == ["Read while muted."]
    assert hushed == ["true", "Stopped reading", True]
    assert reading_off == "false"
    assert held_off == [False, ["ended"]]


async def _until(page, what, check, timeout_ms=6000):
    """Waits for `check()`, a test-side condition such as a route's count, to be true."""
    for _ in range(timeout_ms // 50):
        if check():
            return
        await page.wait_for_timeout(50)
    raise AssertionError(f"never happened: {what}")


# The page as hidden or shown, as a tab switch reports it; and every stream it closes.
_HIDE = """
((hidden) => {
  window.__hidden = hidden;
  document.dispatchEvent(new Event('visibilitychange'));
})
"""
_VISIBILITY = """
(() => {
  window.__hidden = false;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.__hidden });
  Object.defineProperty(document, 'visibilityState', { configurable: true,
    get: () => (window.__hidden ? 'hidden' : 'visible') });
  window.__closed = 0;
  const close = EventSource.prototype.close;
  EventSource.prototype.close = function () { window.__closed += 1; return close.call(this); };
})();
"""
# Every AudioContext the page makes, so a test can suspend them as a phone does a
# background tab's audio.
_CONTEXTS = """
(() => {
  const Real = window.AudioContext;
  window.__contexts = [];
  window.AudioContext = class extends Real {
    constructor(...a) { super(...a); window.__contexts.push(this); }
  };
})();
"""


@pytest.mark.browser
async def test_voice_mode_goes_on_in_a_hidden_tab_and_says_what_the_browser_refused(tmp_path):
    """With the page hidden it keeps the stream open, reads the reply and listens on
    (#3408). A reading the browser never starts playing there pauses voice mode with
    that reason, and the tap that resumes reads the reply it missed; so does a reading
    whose audio stops moving after a tab switch, a tab frozen mid-reply, and a working
    turn's 🔊 note cut off the same way. Off while hidden, the stream closes as
    before. A recording that stops in the background is tested below
    (`test_host_listening_pauses_when_a_background_tab_loses_its_audio`)."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server

    state = """() => ({ mode: document.querySelector('[data-voice-button]')
                         .getAttribute('data-voice-state'),
                       status: document.querySelector(
                           'form.inject-form .voice-control .speech-status')
                         .textContent, closed: __closed })"""
    resting = f"() => ({_VOICE_STATE})() === 'resting'"
    speaking = f"() => ({_VOICE_STATE})() === 'speaking'"
    app = _host_voice_app(tmp_path, max_message_chars=1000)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser, page, host = await _voice_page(
            playwright, base_url, phone=False, scripts=(_VISIBILITY, _CONTEXTS))
        said = host.said
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        button = page.locator("form.inject-form [data-voice-button]")
        mic = page.locator("form.inject-form [data-voice-mic]")
        await _voice_on(page)
        await wait_until(page, "voice mode listens", _LISTENING)

        await page.evaluate(_HIDE + "(true)")
        hidden = await page.evaluate(state)
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 First reply.'])")
        await _until(page, "the hidden page read the reply", lambda: "First reply." in said)
        await wait_until(page, "the hidden page listens after it", _LISTENING)

        # The browser never starts playing in the background: paused, the reply unread.
        host.hang = True
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 Second reply.'])")
        await wait_until(page, "speaking pauses", """() => document.querySelector(
            'form.inject-form .voice-control .speech-status').textContent.includes('speak')""",
                         timeout=10000)
        no_speak = await page.evaluate(state)
        host.hang = False
        await page.evaluate(_HIDE + "(false)")
        await button.click()
        await _until(page, "the tap read the missed reply",
                     lambda: said.count("Second reply.") == 2)

        # Audio the browser stops after a tab switch mid-reply, and a tab frozen
        # mid-reply: each pauses with its reason, the reply unread.
        cut_off = {}
        for kind in ("hidden", "frozen"):
            await wait_until(page, "it listens", _LISTENING)
            host.hold = True
            await page.evaluate(_TURN + "(['thinking', null])")
            await page.evaluate(_TURN + f"(['idle', '🔊 Reply {kind}.'])")
            await wait_until(page, "it speaks", speaking)
            if kind == "hidden":
                await page.evaluate(_HIDE + "(true)")
                await page.evaluate("__contexts.forEach((ctx) => ctx.suspend())")
            else:
                await page.evaluate("""() => { document.dispatchEvent(new Event('freeze'));
                                               document.dispatchEvent(new Event('resume')); }""")
            await wait_until(page, "the cut-off reading pauses", resting, timeout=10000)
            cut_off[kind] = await page.evaluate(state)
            host.hold = False
            await page.evaluate("__contexts.forEach((ctx) => ctx.resume())")
            await page.evaluate(_HIDE + "(false)")
            await button.click()
            await _until(page, "the tap read the cut-off reply again",
                         lambda kind=kind: said.count(f"Reply {kind}.") == 2)

        # A question paused in the background, its replay then skipped with a tap: the
        # answer says it was not heard and names the last line heard in full (#3520).
        sent = []

        async def inject(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        await page.route("**/sessions/codex:test:sid/inject", inject)
        await wait_until(page, "it listens", _LISTENING)
        host.hold = True
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 Shall I stop the batch?'])")
        await wait_until(page, "it speaks", speaking)
        await page.evaluate(_HIDE + "(true)")
        await page.evaluate("__contexts.forEach((ctx) => ctx.suspend())")
        await wait_until(page, "the cut-off question pauses", resting, timeout=10000)
        await page.evaluate("__contexts.forEach((ctx) => ctx.resume())")
        await page.evaluate(_HIDE + "(false)")
        # Shown again, a timer from before the pause may read it again by itself;
        # otherwise the tap that resumes does.
        await page.wait_for_timeout(1000)
        if await page.evaluate(f"({_VOICE_STATE})()") == "resting":
            await button.click()
        await wait_until(page, "it speaks the question again", speaking)
        # Still reading a moment later: no timer left from before listens over it.
        await page.wait_for_timeout(600)
        replayed = [
            said.count("Shall I stop the batch?"),
            await page.evaluate(f"({_VOICE_STATE})()"),
        ]
        # Skipped: 🎧 off and on again (#3540).
        await button.click()
        host.hold = False
        await button.click()
        await wait_until(page, "the skip listens", _LISTENING)
        await host.hear("yes approve that one")
        await _until(page, "the answer was sent", lambda: bool(sent), timeout_ms=10000)

        # A working turn's 🔊 note cut off the same way is read again by the tap,
        # while the agent still works.
        await wait_until(page, "it listens", _LISTENING)
        host.hold = True
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['thinking', '🔊 Working on it.<br><br>Details.'])")
        await wait_until(page, "the note is spoken", speaking)
        await page.evaluate(_HIDE + "(true)")
        await page.evaluate("__contexts.forEach((ctx) => ctx.suspend())")
        await wait_until(page, "the cut-off note pauses", resting, timeout=10000)
        host.hold = False
        await page.evaluate("__contexts.forEach((ctx) => ctx.resume())")
        await page.evaluate(_HIDE + "(false)")
        await button.click()
        await _until(page, "the tap read the cut-off note again",
                     lambda: said.count("Working on it.") == 2)
        await page.evaluate(_TURN + "(['idle', null])")

        await wait_until(page, "it listens again", _LISTENING, timeout=8000)
        # Turned off while hidden, the stream closes.
        await page.evaluate(_HIDE + "(true)")
        before_off = await page.evaluate("__closed")
        # 🎧 and 🎤 both tapped off (#3540).
        await button.click()
        await mic.click()
        await page.wait_for_timeout(300)
        off = await page.evaluate(
            "[__closed, document.documentElement.classList.contains('voice-on')]"
        )
        await browser.close()

    assert hidden["closed"] == 0 and hidden["mode"] == "listening"
    assert "can’t speak in background" in no_speak["status"] and no_speak["closed"] == 0
    assert no_speak["mode"] == "resting"
    assert "can’t speak in background" in cut_off["hidden"]["status"]
    assert "tab stopped in background" in cut_off["frozen"]["status"]
    assert replayed == [2, "speaking"] and len(sent) == 1 and "had not heard" in sent[0]
    assert "which was: “Reply frozen.”." in sent[0]
    assert before_off == 0 and off[0] > 0 and off[1] is False


@pytest.mark.browser
@pytest.mark.parametrize(
    "kind",
    ["question", "mcp_form", "secret", "mic", "jarvis", "finnish", "refreshed", "midway"])
async def test_voice_mode_answers_a_question_box_by_number(tmp_path, monkeypatch, kind):
    """A question box is read with numbered options, "option two" ticks the second,
    and the box's own Submit sends it through the real route. An English box is read
    with the English voice even for an owner who speaks only Finnish (#3572). A tap on 🎤 alone, in
    Tap mode with 🎧 off, answers a box already open rather than sending a chat
    message (#3540 review). In Hey Jarvis mode, on before the box arrives, words
    without the name are let go of and "Hey Jarvis" plus the answer answers it (the
    owner's decision on #3540). A box brought in by the page's own refresh, which a
    reconnected stream does not re-send, is asked too, and one swapped in again
    between two questions keeps the answer already given (#3799). An answer Whisper
    doubted picks nothing and is asked for again (#3817)."""
    from dataclasses import replace

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _question_interaction

    from agentdeck.models import Capability, InjectResult
    from agentdeck.providers import PROVIDERS

    app = _host_voice_app(tmp_path)
    if kind == "finnish":
        app.state.templates.env.globals["speech_language"] = "fi"
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    # A Codex MCP form asks through the same box and Submit.
    question = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite")
    if kind == "secret":
        # A secret choice: options only, so no password field gives it away (#3401 review).
        secret = replace(question.questions[0], secret=True, allow_other=False)
        box = {"pending": replace(question, questions=(secret,))}
    elif kind == "midway":
        second = _question_interaction("tok-1", "Cache", "Which cache?", "Redis", "None")
        box = {"pending": replace(question, questions=question.questions + second.questions)}
    else:
        box = {"pending": replace(question, kind="mcp_form" if kind == "mcp_form" else "question")}
    captured = {}

    async def answer_interaction(account, session, interaction_id, *, answers, decision):
        captured.update(answers=answers, decision=decision)
        box["pending"] = None
        return InjectResult(True)

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "pending_interaction", lambda account, session: box["pending"])
    monkeypatch.setattr(provider, "answer_interaction", answer_interaction)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser, page, host = await _voice_page(playwright, base_url)
        sent = []

        async def fulfil(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(status=202, body='<div id="inject-result"></div>',
                                content_type="text/html")

        await page.route("**/sessions/codex:test:sid/inject", fulfil)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.wait_for_selector("#pending-interaction form[data-interaction-kind]",
                                     timeout=10000)
        if kind == "mic":
            await page.locator("form.inject-form [data-voice-mic]").click()
        elif kind == "jarvis":
            # The box arrives once Hey Jarvis is already waiting, as the stream swaps it in.
            await page.evaluate("""() => { const slot = document.getElementById(
                'pending-interaction-slot');
                window.__box = slot.innerHTML; slot.innerHTML = ''; }""")
            await page.locator("form.inject-form [data-voice-button]").click()
            await _pick(page, "jarvis")
            await wait_until(page, "it waits for Hey Jarvis",
                             f"() => ({_VOICE_STATE})() === 'waiting'", timeout=8000)
            await page.evaluate("""() => { const slot = document.getElementById(
                'pending-interaction-slot'); slot.innerHTML = __box; htmx.process(slot);
                slot.dispatchEvent(new CustomEvent('htmx:sseMessage', { bubbles: true })); }""")
            await _until(page, "the question was read",
                         lambda: "Option 2, SQLite." in " ".join(host.said))
            await wait_until(page, "it waits for Hey Jarvis to answer",
                             f"() => ({_VOICE_STATE})() === 'waiting'", timeout=8000)
            await host.hear("option two")
            await page.wait_for_timeout(1500)
            unwoken = [captured.copy(), await page.locator("#pending-interaction form").count()]
            # A doubted answer picks nothing and is asked for again (#3817).
            host.doubted.add("Hey Jarvis, option one")
            await host.hear("Hey Jarvis, option one")
            # In Hey Jarvis mode the answer again takes the name too, as any answer.
            await _until(page, "it asked again", lambda: "Please say it again." in host.said[-1])
            await wait_until(page, "it waits for the answer again",
                             f"() => ({_VOICE_STATE})() === 'waiting'", timeout=8000)
            await host.hear("Hey Jarvis, option two")
        elif kind == "refreshed":
            await page.evaluate("""() => {
                document.getElementById('pending-interaction-slot').innerHTML = ''; }""")
            await _voice_on(page)
            await wait_until(page, "it listens with no box", _LISTENING)
            await page.evaluate("""() => htmx.ajax('GET',
                '/partials/sessions/codex:test:sid/interaction?known_interaction_id=',
                { target: '#pending-interaction-slot', swap: 'innerHTML' })""")
            await _until(page, "the refreshed question was read",
                         lambda: "Option 2, SQLite." in " ".join(host.said))
        else:
            await _voice_on(page)
        if kind == "secret":
            await _until(page, "it sent the person to the screen",
                         lambda: "Answer on screen" in " ".join(host.said))
            asked = " ".join(host.said)
            still_open = await page.locator("#pending-interaction form").count()
            await browser.close()
            assert "Which database" not in asked and "SQLite" not in asked
            assert still_open == 1 and captured == {}
            return
        if kind != "jarvis":
            await wait_until(page, "it listens for the answer", _LISTENING)
        asked = " ".join(host.said)
        read_in = [
            lang
            for text, lang in zip(host.said, host.languages, strict=True)
            if "SQLite" in text
        ]
        if kind == "question":
            # A doubted answer picks nothing and is asked for again (#3817).
            host.doubted.add("option one")
            await host.hear("option one")
            await _until(page, "it asked again", lambda: "Please say it again." in host.said[-1])
            await wait_until(page, "it listens for the answer again", _LISTENING)
        if kind != "jarvis":
            await host.hear("option two")
        if kind == "midway":
            # The same box swapped in again while the second question is asked.
            await _until(page, "the second question was read",
                         lambda: "Which cache?" in " ".join(host.said))
            await page.evaluate("""() => htmx.ajax('GET',
                '/partials/sessions/codex:test:sid/interaction?known_interaction_id=',
                { target: '#pending-interaction-slot', swap: 'innerHTML' })""")
            await wait_until(page, "it listens for the second answer", _LISTENING)
            await host.hear("option one")
        await wait_until(page, "the answer reached the route",
                         "() => !document.querySelector('#pending-interaction form')",
                         timeout=8000)
        await browser.close()

    if kind == "jarvis":
        assert unwoken == [{}, 1] and sent == []
    if kind == "mic":
        assert asked == "" and sent == []
    else:
        assert "Which database should we use?" in asked
        assert "Option 1, Postgres." in asked and "Option 2, SQLite." in asked
    if kind == "finnish":
        assert read_in == ["en"]
    if kind == "midway":
        assert captured == {"answers": {"database": ["SQLite"], "cache": ["Redis"]},
                            "decision": "accept"}
        return
    assert captured == {"answers": {"database": ["SQLite"]}, "decision": "accept"}


@pytest.mark.browser
async def test_voice_mode_keeps_typing_and_stops_dictation(tmp_path):
    """🎤 listens to everything once Always is picked from its menu (#3787);
    words typed while it listens stay in the draft (#3401 review); opening another
    chat in place lets go of this chat's microphone and starts voice mode again there,
    as it was (#3548), while a navigation that leaves this chat in place (the desktop
    front page mounting beside it, #3408) leaves it alone; a full page load starts it
    again too, and once tapped off, the next chat opened starts with it off. Asked to,
    it goes back to the deck chat, and the deck chat moves it on once its reply was
    read, naming the chat it arrives in (#3816)."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server

    app = _host_voice_app(tmp_path, max_message_chars=1000)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser, page, host = await _voice_page(playwright, base_url)
        sent = []

        async def fulfil(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        await page.route("**/sessions/codex:test:sid/inject", fulfil)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.locator("form.inject-form [data-voice-button]").click()
        await _until(page, "it said it is on", lambda: host.said)
        await _pick(page, "open")
        await wait_until(page, "voice mode listens", _LISTENING)
        said = host.said[:]

        box = page.locator("#inject-message")
        await host.hear("fix the")
        await wait_until(page, "the words are in the draft",
                         "() => document.querySelector('#inject-message').value === 'fix the'")
        await box.press("End")
        await box.type(" login")
        await host.hear("bug")
        await wait_until(page, "the next words follow what was typed",
                         "() => document.querySelector('#inject-message').value"
                         " === 'fix the login bug'")
        await _until(page, "it was sent", lambda: sent)

        await page.evaluate(
            "document.dispatchEvent(new CustomEvent('agentdeck:session-navigation'))")
        kept = await page.locator("[data-voice-button]").get_attribute("aria-pressed")
        other_chat = """() => { window.__left = window.__mic;
            document.querySelector('.transcript').dataset.sessionKey = 'codex:test:other';
            document.dispatchEvent(new CustomEvent('agentdeck:session-navigation')); }"""
        await page.evaluate(other_chat)
        await wait_until(page, "voice mode listens in the chat opened", _LISTENING)
        navigated = await page.evaluate(
            """() => [document.querySelector('[data-voice-button]').getAttribute('aria-pressed'),
                      __left.stream.getTracks().map((t) => t.readyState), __mic !== __left]""")
        # A full load: 🎧 is on again.
        await page.reload()
        await _until(page, "it said it is on again", lambda: host.said.count("Voice mode on.") == 3)
        reloaded = await page.locator("[data-voice-button]").get_attribute("aria-pressed")
        await wait_until(page, "it listens after the reload", _LISTENING)

        # The way back to the deck chat (#3816): the fast agent's jump sends nothing and
        # opens the deck chat in place, where voice mode names it instead of "on".
        async def back_route(route):
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"say": "", "relay": None, "jump": {"path": "/chats/deck", "title": "Deck chat"}}))

        await page.route("**/api/voice/heard", back_route)
        await page.evaluate("""() => { window.__opened = [];
            AgentDeckSessionNavigation.open = (path) => { __opened.push(path);
              document.querySelector('.transcript').dataset.sessionKey =
                'codex:test:' + path.split('/').pop();
              document.dispatchEvent(new CustomEvent('agentdeck:session-navigation')); }; }""")
        await host.hear("take me back to the deck chat")
        await _until(page, "it named the chat it arrived in",
                     lambda: "Now in: Deck chat." in host.said)
        went_back = [await page.evaluate("__opened"), len(sent)]
        await page.unroute("**/api/voice/heard")
        # The deck chat's jump onward, made mid-turn: the page moves only once the turn's
        # reply was read, so that is heard first.
        await wait_until(page, "it listens in the deck chat", _LISTENING)
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate("""() => { const slot = document.getElementById('chat-jump-slot');
            slot.innerHTML = '<span data-chat-jump=\\'{"id": "j1", "path": "/chats/bug",'
              + ' "title": "🐞 The login bug"}\\'></span>';
            slot.dispatchEvent(new CustomEvent('htmx:sseMessage', {bubbles: true})); }""")
        await page.wait_for_timeout(300)
        waited = await page.evaluate("__opened.length")
        await page.evaluate(_TURN + "(['idle', '🔊 Taking you to the login bug.'])")
        await _until(page, "it named the chat it jumped to",
                     lambda: "Now in: The login bug." in host.said)
        heard_first = host.said[-2:]
        went_on = await page.evaluate("__opened")
        await wait_until(page, "it listens in the chat jumped to", _LISTENING)
        await page.locator("form.inject-form [data-voice-button]").click()
        await _pick(page, "off")
        await page.evaluate(other_chat)
        await page.wait_for_timeout(300)
        tapped_off = [await page.locator("[data-voice-button]").get_attribute("aria-pressed"),
                      await page.evaluate(
                          "() => document.documentElement.classList.contains('voice-on')")]
        await browser.close()

    assert said == ["Voice mode on."]
    assert len(sent) == 1
    assert "fix+the+login+bug" in sent[0] or "fix the login bug" in sent[0]
    assert kept == "true"
    assert navigated == ["true", ["ended"], True]
    assert host.said.count("Voice mode on.") == 3
    assert reloaded == "true"
    assert went_back == [["/chats/deck"], 1]
    assert went_on == ["/chats/deck", "/chats/bug"] and waited == 1
    assert heard_first == ["Taking you to the login bug.", "Now in: The login bug."]
    assert tapped_off == ["false", False]


# The frequency of every tone the page starts, the fake microphone's own included.
_TONES = """
(() => {
  window.__tones = [];
  const start = OscillatorNode.prototype.start;
  OscillatorNode.prototype.start = function (...args) {
    window.__tones.push(this.frequency.value);
    return start.apply(this, args);
  };
})();
"""

# A microphone for host listening: a tone the test turns up ("speech") and down.
_FAKE_MICROPHONE = """
(() => {
  navigator.mediaDevices.getUserMedia = async () => {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = 0;
    const dest = ctx.createMediaStreamDestination();
    osc.connect(gain).connect(dest);
    osc.start();
    window.__mic = {
      level(value) { gain.gain.cancelScheduledValues(0); gain.gain.value = value; },
      // Sound for exactly `ms`, by the audio clock: a level set and unset from the
      // test lasts its round trips longer.
      pulse(value, ms) {
        const at = ctx.currentTime;
        gain.gain.cancelScheduledValues(0);
        gain.gain.setValueAtTime(value, at);
        gain.gain.setValueAtTime(0, at + ms / 1000);
      },
      stream: dest.stream,
    };
    return dest.stream;
  };
})();
"""

def _silent_wav(seconds=0.2, rate=16000):
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * int(seconds * rate))
    return buffer.getvalue()


@pytest.mark.browser
async def test_voice_mode_listens_and_speaks_through_the_host(tmp_path):
    """With a speech service, in a browser with no speech of its own (Firefox), a phrase
    is cut at its pause, posted to the host and sent; Finnish heard asks for a Finnish
    answer, read with the Finnish voice; a dead service turns voice mode off (#3409).
    English words the host took for Finnish ask for no Finnish, and a reply is read in
    the language it is written in (#3417). Once no words come it waits for "Hey
    Jarvis": other words are let go of, "Hey Jarvis" alone listens saying nothing, and the
    words after it are sent (#3425, #3476). Noise that stays up after speech still
    ends the phrase, the wait for "Hey Jarvis" starts after the page's idle seconds, and a
    service that crashed and restarts is waited for (#3424). A cough or a fan after
    the words does not hold the send back for good, even with a slow host (#3449)."""
    import asyncio
    import json

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    from agentdeck.voice_mode import VOICE_LINE_FI

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    app.state.templates.env.globals["speech_language"] = "auto"
    app.state.templates.env.globals["voice_idle_seconds"] = 5
    wav = _silent_wav()
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_NO_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        page = await context.new_page()
        await page.clock.install()
        sent, said, phrases = [], [], []
        host = {"up": True, "fail": 0}
        heard = [{"text": "lisää testi", "language": "fi"}]

        async def inject(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        async def say(route):
            said.append(json.loads(route.request.post_data))
            await route.fulfill(status=200, body=wav, content_type="audio/wav")

        async def transcribe(route):
            query = route.request.url.partition("?")[2]
            phrases.append((query, route.request.post_data_buffer[:4]))
            if not host["up"] or host["fail"]:
                host["fail"] = max(0, host["fail"] - 1)
                await route.fulfill(status=503, body="{}", content_type="application/json")
                return
            if host.get("slow"):
                await asyncio.sleep(host["slow"])
            body = json.dumps(heard[-1])
            await route.fulfill(status=200, content_type="application/json", body=body)

        await page.route("**/sessions/codex:test:sid/inject", inject)
        await page.route("**/api/speech/say", say)
        await page.route("**/api/speech/transcribe*", transcribe)
        await page.route("**/api/voice/narrate", lambda route: route.fulfill(status=204))
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        button = page.locator("form.inject-form [data-voice-button]")
        await button.wait_for(state="visible")
        # A send mid-turn is acknowledged once the chat stays quiet for 0.3 s (#3799).
        await page.evaluate(_QUIET + "('0.3 600')")
        await _voice_on(page)
        listening = """() => document.querySelector('[data-voice-button]')
            .getAttribute('data-voice-state') === 'listening'"""
        await wait_until(page, "it listens after saying it is on", listening)

        # Half a second of speech, then quiet: one phrase goes to the host, and its
        # words are sent a second later with the Finnish line.
        await page.evaluate(_TURN + "(['thinking', null])")
        await wait_until(page, "it listens while the agent works", listening)
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(500)
        await page.evaluate("__mic.level(0)")
        for _ in range(40):
            if len(said) > 1:
                break
            await page.wait_for_timeout(100)

        # English words Whisper called Finnish (#3417), said while the agent works:
        # sent with the plain line.
        await wait_until(page, "it listens while the agent works", listening)
        # The room stays a little loud after the words (#3424): learned while it
        # lasts, that noise counts as quiet, and the phrase still ends.
        heard.append({"text": "What has happened in the last hour?", "language": "fi"})
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(500)
        await page.evaluate("__mic.level(0.05)")
        for _ in range(80):
            if len(sent) > 1:
                break
            await page.wait_for_timeout(100)
        await page.evaluate("__mic.level(0)")
        for _ in range(40):
            if len(said) > 2:
                break
            await page.wait_for_timeout(100)

        # The reply's 🔊 paragraph is read with the Finnish voice.
        await wait_until(page, "it listens after the acknowledgements", listening)
        await page.evaluate(
            _TURN + "(['idle', '🔊 Valmis, testi lisätty.<br><br>Lisää ruudulla.'])"
        )
        for _ in range(60):
            if len(said) > 3:
                break
            await page.wait_for_timeout(100)
        await wait_until(page, "it listens again", listening)

        # A reply in Finnish to the plain line is still read with the Finnish voice.
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 Virhe korjattu.'])")
        for _ in range(60):
            if len(said) > 4:
                break
            await page.wait_for_timeout(100)
        await wait_until(page, "it listens again", listening)

        # Nothing said for the idle seconds: it waits for "Hey Jarvis" (#3425).
        state = ("() => document.querySelector('[data-voice-button]')"
                 ".getAttribute('data-voice-state')")
        draft = "() => document.querySelector('form.inject-form textarea').value"

        async def say_phrase(text):
            heard.append({"text": text, "language": "en"})
            asked = len(phrases)
            await page.evaluate("__mic.level(0.5)")
            await page.wait_for_timeout(500)
            await page.evaluate("__mic.level(0)")
            for _ in range(40):
                if len(phrases) > asked:
                    break
                await page.wait_for_timeout(100)
            await page.wait_for_timeout(1500)

        await page.clock.fast_forward(31000)
        await wait_until(page, "it waits for the wake word",
                         f"() => ({state})() === 'waiting'")
        await say_phrase("What time is it?")
        chatter = [len(sent), await page.evaluate(f"({state})()"), await page.evaluate(draft)]
        quiet = len(said)
        await say_phrase("Hey Jarvis.")
        await wait_until(page, "it listens after the wake word", listening, timeout=5000)
        yes = said[quiet:]
        await page.clock.fast_forward(31000)
        await wait_until(page, "it waits again", f"() => ({state})() === 'waiting'")
        # Gone back to waiting, a turn and its 🔊 note do not open the microphone;
        # the turn's end does (#3456).
        await page.evaluate(_TURN + "(['thinking', null])")
        noting = len(said)
        await page.evaluate(_TURN + "(['thinking', '🔊 Checking the logs.'])")
        for _ in range(60):
            if len(said) > noting:
                break
            await page.wait_for_timeout(100)
        await wait_until(page, "the note leaves it waiting",
                         f"() => ({state})() === 'waiting'", timeout=5000)
        mid_turn = [said[noting:], await page.evaluate(f"({state})()")]
        await page.evaluate(_TURN + "(['idle', '🔊 Logs are clean.'])")
        await wait_until(page, "the turn's end listens", listening, timeout=8000)
        await page.clock.fast_forward(31000)
        await wait_until(page, "it waits again", f"() => ({state})() === 'waiting'")
        # Nothing is said at a send with no turn (#3799); it listens again after it.
        async def after_ack(count):
            await wait_until(page, "it listens after the send", listening)

        acked = len(said)
        await say_phrase("Hey, Jarvis. Run the tests.")
        await wait_until(page, "the words after it were sent", listening, timeout=5000)
        await after_ack(acked)

        # 🎤 tapped off lets go of the recording; on again, it opens a new one (#3540).
        await page.evaluate("window.__first = __mic.stream")
        mic_stopped = await page.evaluate(
            "() => __first.getTracks().map((t) => t.readyState)")
        await _pick(page, "off")
        mic_muted = await page.evaluate(
            "() => __first.getTracks().map((t) => t.readyState)")
        await _pick(page, "open")
        await wait_until(page, "on again, it listens on a new recording", """() =>
            __mic.stream !== __first && document.querySelector('[data-voice-button]')
            .getAttribute('data-voice-state') === 'listening'""")

        # No words for the page's 5 s (#3424): it waits for "Hey Jarvis"; Always
        # picked again listens.
        await wait_until(page, "it waits after the idle seconds",
                         f"() => ({state})() === 'waiting'", timeout=8000)
        idled = await page.locator(".voice-control .speech-status").text_content()
        await _pick(page, "open")
        await wait_until(page, "Always listens again", listening)

        # The service crashes once: the phrase is sent again once it is back.
        host["fail"] = 1
        acked = len(said)
        before_crash = len(phrases)
        heard.append({"text": "still there", "language": "en"})
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(300)
        await page.evaluate("__mic.level(0)")
        await wait_until(page, "it says the service is restarting", """() =>
            document.querySelector('.voice-control .speech-status').textContent
            === 'Speech service restarting · trying again…'""")
        for _ in range(60):
            if len(sent) > 3:
                break
            await page.wait_for_timeout(100)

        # One long steady sentence is one phrase: the room is not learned inside it,
        # so it is not cut once its own level would have become the room. Steady
        # noise at 60 % of it after the words is still a pause (#3424).
        await after_ack(acked)
        acked = len(said)
        long_asked = len(phrases)
        heard.append({"text": "a long sentence", "language": "en"})
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(6000)
        await page.evaluate("__mic.level(0.3)")
        for _ in range(50):
            if len(sent) > 4:
                break
            await page.wait_for_timeout(100)
        long_posts = len(phrases) - long_asked
        await after_ack(acked)
        acked = len(said)
        # Louder noise after a short phrase (0.5 then 0.2) ends it too; the noise
        # that ended it starts no phrase of its own.
        heard.append({"text": "over the noise", "language": "en"})
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(500)
        await page.evaluate("__mic.level(0.2)")
        for _ in range(50):
            if len(sent) > 5:
                break
            await page.wait_for_timeout(100)
        noisy_posts = len(phrases) - long_asked - long_posts
        await page.evaluate("__mic.level(0)")
        await after_ack(acked)

        # Sound starting while words wait to be sent holds them back, but coughs the
        # host hears no words in let them go, however many follow each other inside
        # the second, and so does noise that goes on: it is checked for words after
        # 2 s (#3449).
        async def words_then_noise(text, coughs, slow=0):
            heard.append({"text": text, "language": "en"})
            asked, count = len(phrases), len(sent)
            await page.evaluate("__mic.level(0.5)")
            await page.wait_for_timeout(500)
            await page.evaluate("__mic.level(0)")
            for _ in range(40):
                if len(phrases) > asked:
                    break
                await page.wait_for_timeout(50)
            # Posted early in its pause (#3765): a cough before the pause ends would
            # still join the phrase.
            await page.wait_for_timeout(600)
            heard.append({"text": "", "language": "en"})
            host["slow"] = slow
            # A cough every 1.2 s: each phrase ends, and the next starts within the
            # second that used to start the wait again.
            for _ in range(coughs):
                await page.evaluate("__mic.level(0.4)")
                await page.wait_for_timeout(300)
                await page.evaluate("__mic.level(0)")
                await page.wait_for_timeout(900)
                if len(sent) > count:
                    break
            # Coughs: sent while they still go on, not only once they stop.
            if coughs:
                during = len(sent) > count
                await page.wait_for_timeout(1500 + slow * 2000)
                host["slow"] = 0
                return during
            await page.evaluate("__mic.level(0.4)")
            for _ in range(50):
                if len(sent) > count:
                    break
                await page.wait_for_timeout(100)
            await page.evaluate("__mic.level(0)")
            await page.wait_for_timeout(1500)
            return len(sent) > count

        # A slow host: the next cough is kept while the last is transcribed, and
        # replayed into the next phrase, which must not cancel a send already due.
        # Last, as the wait for its slow answers outlasts the idle seconds.
        noise_sends = [await words_then_noise("after a cough", 8),
                       await words_then_noise("over a fan", 0)]

        # Sent at once as that cough ends, the phrase waits on the fast agent: a
        # cough then heard as words is not written into the draft it is sent from,
        # in its place (#3514).
        async def fast_slowly(route):
            heard.append({"text": "spoken over it", "language": "en"})
            await asyncio.sleep(4)
            heard.append({"text": "", "language": "en"})
            await route.fulfill(status=503, body="{}", content_type="application/json")

        await page.route("**/api/voice/heard", fast_slowly)
        noise_sends.append(await words_then_noise("after slow coughs", 8, slow=1.5))
        await page.unroute("**/api/voice/heard")

        # The service goes away: it turns off once the retries run out.
        # A sound as short as a clipped "ei" (120 ms) is enough to reach the host.
        before_down = len(phrases)
        host["up"] = False
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(120)
        await page.evaluate("__mic.level(0)")
        await wait_until(page, "voice mode turned off", """() => document.querySelector(
            '[data-voice-button]').getAttribute('aria-pressed') === 'false'""", timeout=15000)
        status = await page.locator(".voice-control .speech-status").text_content()
        await browser.close()

    assert said[0] == {"text": "Voice mode on.", "language": "en"}
    # Each send mid-turn is acknowledged once the chat stays quiet, in the language it
    # asked for (#3444, #3799).
    assert said[1:3] == [{"text": "Selvä, työn alla.", "language": "fi"},
                         {"text": "Got it, working on it.", "language": "en"}]
    assert said[3] == {"text": "Valmis, testi lisätty.", "language": "fi"}
    assert said[4] == {"text": "Virhe korjattu.", "language": "fi"}
    # The first phrase is the host's own call; later ones carry what it heard, by
    # the words.
    assert phrases[:3] == [("", b"RIFF"), ("language=fi", b"RIFF"), ("language=en", b"RIFF")]
    # The crashed phrase is sent twice; the last one once and three retries.
    assert [long_asked - before_crash, long_posts, noisy_posts] == [2, 1, 1]
    assert len(phrases) - before_down == 4
    assert noise_sends == [True, True, True]
    assert len(sent) == 9
    assert "after+a+cough" in sent[6] or "after a cough" in sent[6]
    assert "over+a+fan" in sent[7] or "over a fan" in sent[7]
    assert "after+slow+coughs" in sent[8] or "after slow coughs" in sent[8]
    assert "still+there" in sent[3] or "still there" in sent[3]
    assert "long+sentence" in sent[4] or "long sentence" in sent[4]
    assert "over+the+noise" in sent[5] or "over the noise" in sent[5]
    assert idled == "Say “Hey Jarvis” · tap 🎤 to talk"
    assert "lis%C3%A4%C3%A4+testi" in sent[0] or "lisää testi" in sent[0]
    assert "Answer+in+Finnish" in sent[0] or VOICE_LINE_FI in sent[0]
    assert "last+hour" in sent[1] or "last hour" in sent[1]
    assert "Finnish" not in sent[1]
    assert chatter == [2, "waiting", ""]
    assert yes == []
    assert mid_turn == [[{"text": "Checking the logs.", "language": "en"}], "waiting"]
    assert "Run+the+tests" in sent[2] or "Run the tests" in sent[2]
    assert "Jarvis" not in sent[2] and "Hey" not in sent[2]
    assert status == "Speech service unreachable"
    assert mic_stopped == ["live"] and mic_muted == ["ended"]


# Every buffer the page plays, by its length: a "Mm." would be told apart from a reading.
_PLAYED = """
(() => {
  window.__played = [];
  const start = AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start = function (...args) {
    if (this.buffer) window.__played.push(Math.round(this.buffer.duration * 10) / 10);
    return start.apply(this, args);
  };
})();
"""


@pytest.mark.browser
async def test_voice_mode_answers_sooner_after_the_owner_stops_talking(tmp_path):
    """#3765: the phrase goes to the host early in its ending pause, and that pass is
    dropped when sound comes back; the fast agent is asked as soon as the words come
    back, before the send; more words throw its answer away and the next ask names
    it; the waits are saved. No "Mm." is fetched or played while it answers (#3780)."""
    import io
    import wave

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    app.state.templates.env.globals["speech_language"] = "auto"
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        for script in (_NO_RECOGNITION, _FAKE_MICROPHONE, _PLAYED):
            await context.add_init_script(script)
        page = await context.new_page()
        log, words, asks, reports, lengths = [], ["Fix the bug."], [], [], []

        def mark(what):
            log.append((what, time.monotonic()))

        async def inject(route):
            mark("sent")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"})

        async def transcribe(route):
            mark("transcribe")
            with wave.open(io.BytesIO(route.request.post_data_buffer)) as posted:
                lengths.append(posted.getnframes() / posted.getframerate())
            # Whisper doubts the second "test." (#3817).
            parts = [{"text": words[0], "unsure": words[0] == "test."}]
            body = json.dumps({"text": words[0], "language": "en", "segments": parts})
            await route.fulfill(status=200, content_type="application/json", body=body)

        async def heard(route):
            asks.append(json.loads(route.request.post_data))
            mark("ask")
            await asyncio.sleep(1.5)
            body = json.dumps({"say": "On it.", "relay": asks[-1]["text"]})
            await route.fulfill(status=200, content_type="application/json", body=body)

        async def sent(route):
            reports.append(json.loads(route.request.post_data))
            await route.fulfill(status=204)

        async def audio(seconds):
            async def fulfill(route):
                mark(route.request.url.rpartition("/")[2].partition("?")[0])
                await route.fulfill(status=200, body=_silent_wav(seconds),
                                    content_type="audio/wav")
            return fulfill

        await page.route("**/sessions/codex:test:sid/inject", inject)
        await page.route("**/api/speech/transcribe*", transcribe)
        await page.route("**/api/voice/heard", heard)
        await page.route("**/api/voice/sent", sent)
        reading = {"seconds": 0.2}

        async def say(route):
            mark("say")
            await route.fulfill(status=200, body=_silent_wav(reading["seconds"]),
                                content_type="audio/wav")

        await page.route("**/api/speech/say", say)
        await page.route("**/api/voice/filler*", await audio(0.6))
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.locator("form.inject-form [data-voice-button]").wait_for(state="visible")
        await _voice_on(page)
        listening = """() => document.querySelector('[data-voice-button]')
            .getAttribute('data-voice-state') === 'listening'"""
        await wait_until(page, "it listens", listening)

        async def count(what):
            return sum(1 for name, _ in log if name == what)

        async def until(what, n, timeout=8.0):
            end = time.monotonic() + timeout
            while await count(what) < n and time.monotonic() < end:
                await asyncio.sleep(0.05)
            assert await count(what) >= n, (what, n, log)

        # One phrase: one pass, made inside the pause; the ask goes before the send.
        await page.evaluate("__mic.pulse(0.5, 500)")
        await until("sent", 1)
        first = list(log)
        # Nothing is said at the send (#3799): the first voice is the chat's reply.
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 Fixed it.'])")
        await until("say", 1)
        await wait_until(page, "it listens after the answer", listening, timeout=8000)
        played_first = await page.evaluate("__played.slice()")

        # Sound back after 0.5 s of quiet: the early pass is dropped, and the one
        # made in the next pause is the phrase's.
        log.clear()
        await page.evaluate("__mic.pulse(0.5, 400)")
        await page.wait_for_timeout(900)
        await page.evaluate("__mic.pulse(0.5, 400)")
        await until("sent", 1)
        second = [name for name, _ in log]
        await wait_until(page, "it listens again", listening, timeout=8000)

        # More words while the first piece's answer is on its way: thrown away.
        log.clear()
        words[0] = "And the test."
        await page.evaluate("__mic.pulse(0.5, 500)")
        await until("ask", 1)
        words[0] = "test."
        await page.evaluate("__mic.pulse(0.5, 500)")
        await until("sent", 1)
        await page.wait_for_timeout(500)
        third = [name for name, _ in log]
        played = await page.evaluate("__played.slice()")

        # Talk over a reading is heard out as before: its check and its one pass,
        # nothing early in its pause.
        await wait_until(page, "it listens after the third", listening, timeout=8000)
        reading["seconds"] = 6
        words[0] = ""
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 A long reply to talk over.'])")
        await wait_until(page, "it reads", """() => document.querySelector(
            '[data-voice-button]').getAttribute('data-voice-state') === 'speaking'""")
        await page.wait_for_timeout(300)
        log.clear()
        lengths.clear()
        await page.evaluate("__mic.pulse(0.5, 500)")
        await page.wait_for_timeout(2000)
        overheard = [name for name, _ in log]
        await browser.close()

    times = {name: at for name, at in reversed(first)}
    names = [name for name, _ in first]
    assert names.count("transcribe") == 1 and names.count("ask") == 1
    # Asked about a second before the send, with the send's own wait unchanged.
    assert times["sent"] - times["ask"] > 0.7
    assert "filler" not in names and 0.6 not in played_first
    assert second.count("transcribe") == 2 and second.count("ask") == 1
    assert third.count("ask") == 2 and "filler" not in third
    # The part Whisper doubted is asked about, and sent, marked: the second "test",
    # not the first (#3817).
    doubted = "And the test. [unsure: test.]"
    assert asks[-2]["text"] == "And the test." and asks[-1]["text"] == doubted
    assert [r["text"] for r in reports if r.get("id") == asks[-1]["id"] and "how" in r] == [
        doubted]
    assert asks[-1]["drop"] == asks[-2]["id"] and not asks[-2]["drop"]
    dropped = [r for r in reports if r.get("id") == asks[-2]["id"]]
    assert dropped == [{"session_key": "codex:test:sid", "id": asks[-2]["id"], "text": None,
                        "how": "not sent: more words followed"}]
    timing = [r["timing"] for r in reports if r.get("id") == asks[0]["id"] and "timing" in r]
    # The first voice is the chat's reply, after the send.
    assert len(timing) == 1 and timing[0]["words"] < timing[0]["sent"] < timing[0]["voice"]
    # Its check, then the whole phrase with the 0.8 s pause that ended it.
    assert overheard.count("transcribe") == 2 and lengths[-1] > lengths[0] + 0.7, lengths
    assert played.count(0.6) == 0


@pytest.mark.browser
async def test_voice_mode_for_an_owner_who_always_speaks_finnish(tmp_path):
    """With ``[speech] language = "fi"`` every message asks for a Finnish answer and
    the Finnish voice reads replies, however English their words look, and voice
    mode's own lines are Finnish (#3525). A first phrase spoken while the speech
    service is still loading its model, just started, waits for it and says so, past
    the retries that would otherwise turn voice mode off (#3766)."""
    import asyncio
    import json

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    app.state.templates.env.globals["speech_language"] = "fi"
    wav = _silent_wav()
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_NO_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        page = await context.new_page()
        await page.clock.install()
        sent, said = [], []
        # Just started: the model loads, and the first phrases fail as it does, more
        # often than the restart retries allow for (#3766).
        host = {"loaded": False, "fail": 4}

        async def inject(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        async def say(route):
            said.append(json.loads(route.request.post_data))
            await route.fulfill(status=200, body=wav, content_type="audio/wav")

        async def transcribe(route):
            if host["fail"]:
                host["fail"] -= 1
                await route.fulfill(status=503, body="{}", content_type="application/json")
                return
            await asyncio.sleep(2.5)  # the model finishing its load
            host["loaded"] = True
            body = json.dumps({"text": "Okay, deploy the pull request.", "language": "fi"})
            await route.fulfill(status=200, content_type="application/json", body=body)

        async def status(route):
            body = json.dumps({"loaded": host["loaded"]})
            await route.fulfill(status=200, content_type="application/json", body=body)

        await page.route("**/sessions/codex:test:sid/inject", inject)
        await page.route("**/api/speech/say", say)
        await page.route("**/api/speech/transcribe*", transcribe)
        await page.route("**/api/speech/status", status)
        # The fast agent has nothing to say on the quiet turn (#3799).
        await page.route("**/api/voice/narrate", lambda route: route.fulfill(status=204))
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.evaluate("""() => document.querySelector('form.inject-form .voice-control')
            .setAttribute('data-voice-quiet-seconds', '0.5 30')""")
        await _voice_on(page)
        listening = """() => document.querySelector('[data-voice-button]')
            .getAttribute('data-voice-state') === 'listening'"""
        await wait_until(page, "it listens after saying it is on", listening)
        await page.evaluate("""() => {
            window.__shown = [];
            const bubble = document.querySelector('.voice-control .speech-status');
            new MutationObserver(() => window.__shown.push(bubble.textContent))
                .observe(bubble, {childList: true, characterData: true, subtree: true});
        }""")
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(500)
        await page.evaluate("__mic.level(0)")
        for _ in range(250):
            if sent:
                break
            await page.wait_for_timeout(100)
        shown = await page.evaluate("window.__shown")
        # A quiet turn: once its quiet spell is over, the acknowledgement.
        await page.evaluate(_TURN + "(['thinking', null])")
        for _ in range(60):
            if len(said) > 1:
                break
            await page.wait_for_timeout(100)
        await wait_until(page, "it listens after the acknowledgement", listening)
        await page.evaluate(_TURN + "(['idle', '🔊 Done, the pull request is merged.'])")
        for _ in range(60):
            if len(said) > 2:
                break
            await page.wait_for_timeout(100)
        await browser.close()

    assert "Answer in Finnish." in sent[0]
    # The phrase waited for the model, saying so, and voice mode stayed on.
    assert "Loading speech model…" in shown and "Speech service unreachable" not in shown
    assert "Listening…" in shown[shown.index("Loading speech model…"):]
    # Voice mode's own lines are Finnish too.
    assert said[0] == {"text": "Äänitila päällä.", "language": "fi"}
    assert said[1:3] == [{"text": "Selvä, työn alla.", "language": "fi"},
                         {"text": "Done, the pull request is merged.", "language": "fi"}]


@pytest.mark.browser
async def test_voice_mode_waits_on_the_wake_word_detector(tmp_path, monkeypatch):
    """With `[speech] wake = "detector"` (#3476), a waiting phrase goes to the wake
    word detector first: one it does not wake on is never transcribed, "Hey Jarvis"
    alone beeps and listens untranscribed, saying nothing (#3781), and a phrase with
    words after the wake word is transcribed whole and sent without it, even when
    Whisper misspells the name.
    What Whisper made of a waiting phrase is reported for the wake log.
    In Jarvis mode a reply being read goes on through talk the detector does not
    wake on; "Hey Jarvis" stops it (#3798), sending the words after the name with
    the line saying the reply was not heard in full, or beeping and listening with
    none."""
    import json

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    from agentdeck import issue_post

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    # A crowded phone composer, the fullest row #3545 keeps on a 360 px screen: 🎤,
    # 🎧 and an issue chip left of Stop and Send. The waiting bubble wraps into the
    # little room left of 🎧 rather than leaving the screen (#3497).
    chip = issue_post.shown_target("eerovil/agentdeck#3545")
    monkeypatch.setattr(issue_post, "issue_post_target", lambda state, key: chip)
    app.state.templates.env.globals["voice_idle_seconds"] = 5
    app.state.templates.env.globals["voice_wake"] = "detector"
    wav, long = _silent_wav(), _silent_wav(seconds=20)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_NO_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        await context.add_init_script(_TONES)
        page = await context.new_page()
        await page.clock.install()
        sent, said, woken, transcribed, logged = [], [], [], [], []
        found = [{}]
        heard = [""]

        async def inject(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        async def say(route):
            said.append(json.loads(route.request.post_data)["text"])
            body = long if said[-1].startswith("The tests passed") else wav
            await route.fulfill(status=200, body=body, content_type="audio/wav")

        async def wake(route):
            woken.append(route.request.url.partition("?id=")[2])
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(found[-1]))

        async def transcribe(route):
            transcribed.append(heard[-1])
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"text": heard[-1], "language": "en"}))

        async def wake_log(route):
            logged.append(json.loads(route.request.post_data))
            await route.fulfill(status=204)

        await page.route("**/sessions/codex:test:sid/inject", inject)
        await page.route("**/api/speech/say", say)
        await page.route("**/api/speech/wake?*", wake)
        await page.route("**/api/speech/wake-log", wake_log)
        await page.route("**/api/speech/transcribe*", transcribe)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        button = page.locator("form.inject-form [data-voice-button]")
        await button.wait_for(state="visible")
        await _voice_on(page)
        await wait_until(page, "it listens after saying it is on", _LISTENING)
        waiting = f"() => ({_VOICE_STATE})() === 'waiting'"
        bubble = """() => { const s = document.querySelector('.voice-control .speech-status');
            const r = s.getBoundingClientRect();
            return [r.left >= 0 && r.right <= innerWidth, s.scrollWidth <= s.clientWidth,
                    s.textContent]; }"""

        async def say_phrase(answer, text="", settle=1500):
            found.append(answer)
            heard.append(text)
            asked = len(woken)
            await page.evaluate("__mic.level(0.5)")
            await page.wait_for_timeout(500)
            await page.evaluate("__mic.level(0)")
            for _ in range(40):
                if len(woken) > asked:
                    break
                await page.wait_for_timeout(100)
            await page.wait_for_timeout(settle)

        await page.clock.fast_forward(31000)
        await wait_until(page, "it waits for the wake word", waiting)
        on_screen = await page.evaluate(bubble)
        await say_phrase({"woken": False, "score": 0.01, "speech_after": False})
        chatter = [len(woken), list(transcribed), len(sent), await page.evaluate(
            f"({_VOICE_STATE})()")]
        quiet = [len(said), await page.evaluate("__tones.length")]
        await say_phrase({"woken": True, "score": 0.98, "speech_after": False}, settle=0)
        # At once, not after the second a message waits for more words (#3793).
        await wait_until(page, "it listens after the wake word", _LISTENING, timeout=600)
        bare = [list(transcribed), said[quiet[0]:],
                await page.evaluate(f"__tones.slice({quiet[1]})")]

        await page.clock.fast_forward(31000)
        await wait_until(page, "it waits again", waiting)
        await say_phrase({"woken": True, "score": 0.97, "speech_after": True},
                         "Hey, Jarvis. Run the tests.")
        await wait_until(page, "the words after it were sent", _LISTENING, timeout=5000)
        for _ in range(40):
            if sent:
                break
            await page.wait_for_timeout(100)

        await page.clock.fast_forward(31000)
        await wait_until(page, "it waits once more", waiting)
        await say_phrase({"woken": True, "score": 0.91, "speech_after": True},
                         "Hey Travis, check the logs.")
        for _ in range(40):
            if len(sent) > 1:
                break
            await page.wait_for_timeout(100)
        named, logs, words = list(woken), list(logged), list(transcribed)

        # Jarvis mode, reading a reply: talk the detector does not wake on goes
        # untranscribed and the reading goes on (#3798).
        speaking = f"() => ({_VOICE_STATE})() === 'speaking'"
        await _pick(page, "jarvis")
        await wait_until(page, "it waits in Jarvis mode", waiting)

        async def read(reply):
            await page.evaluate(_TURN + "(['thinking', null])")
            await page.evaluate(_TURN + f"(['idle', {json.dumps(reply)}])")
            # Not the short "Got it" still read for the message sent before.
            for _ in range(40):
                if said[-1] == reply[2:]:
                    break
                await page.wait_for_timeout(100)
            await page.wait_for_timeout(300)
            await wait_until(page, "it reads the reply", speaking)

        await read("🔊 The tests passed and the branch is merged.")
        heard_before = len(transcribed)
        await say_phrase({"woken": False, "score": 0.02, "speech_after": False}, "Nice day.")
        read_on = [len(woken) - len(named), transcribed[heard_before:],
                   await page.evaluate(f"({_VOICE_STATE})()")]
        await say_phrase({"woken": True, "score": 0.96, "speech_after": True},
                         "Hey Jarvis, what changed?", settle=0)
        for _ in range(40):
            if len(sent) > 2:
                break
            await page.wait_for_timeout(100)
        # With nothing after the name it beeps and listens.
        await read("🔊 The tests passed once more.")
        tones = await page.evaluate("__tones.length")
        await say_phrase({"woken": True, "score": 0.98, "speech_after": False}, settle=0)
        await wait_until(page, "the name alone stops it and listens", _LISTENING, timeout=2000)
        alone = [transcribed[heard_before:], await page.evaluate(f"__tones.slice({tones})")]
        await browser.close()

    assert on_screen == [True, True, "Say “Hey Jarvis” · tap 🎤 to talk"]
    assert chatter == [1, [], 0, "waiting"]
    assert bare == [[], [], [880]]
    woken, logged, transcribed = named, logs, words
    assert transcribed == ["Hey, Jarvis. Run the tests.", "Hey Travis, check the logs."]
    assert "Run+the+tests" in sent[0] or "Run the tests" in sent[0]
    assert "Jarvis" not in sent[0]
    assert "check+the+logs" in sent[1] or "check the logs" in sent[1]
    assert read_on == [1, [], "speaking"]
    assert alone == [["Hey Jarvis, what changed?"], [880]]
    cut = sent[2].replace("+", " ") if len(sent) == 3 else ""
    assert "what changed" in cut and "Jarvis" not in cut and "had not heard" in cut
    # Each phrase is named, and Whisper's words for the woken ones are logged.
    assert len(set(woken)) == 4 and all(woken)
    phrases = [entry for entry in logged if "id" in entry]
    assert [(entry["id"], entry["matched"], entry["detected"]) for entry in phrases] == [
        (woken[2], True, True), (woken[3], False, True)
    ]
    # Each spell of waiting is logged as it starts and stops, for the report's hours.
    spells = [(entry["waiting"], entry["spell"]) for entry in logged if "waiting" in entry]
    assert [state for state, _ in spells] == ["start", "stop"] * 3
    assert len({spell for _, spell in spells}) == 3
    assert all(spells[i][1] == spells[i + 1][1] for i in range(0, 6, 2))


@pytest.mark.browser
async def test_voice_mode_reads_with_headphones_and_listens_with_the_mic_its_way(tmp_path):
    """🎧 only reads aloud and 🎤 only listens (#3540). With 🎧 alone a reply is read
    and nothing listens. 🎤 starts in Jarvis (#3787): a tap listens for one message,
    then it waits for "Hey Jarvis", letting talk in the room go and sending the words
    after the name; a tap while it waits listens for one message again, and a tap
    while it listens sends what was heard and goes back to Jarvis. A long press opens
    Jarvis, Always and Off: Always listens after each reply, with 🎧 off too, and then
    asks for no 🔊 line; Off lets go of the microphone. A fresh page starts in Jarvis,
    and the mode the old menu kept on the device is dropped."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server

    from agentdeck.voice_mode import VOICE_LINE

    app = _host_voice_app(tmp_path, max_message_chars=1000)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser, page, host = await _voice_page(playwright, base_url)
        said = host.said
        sent = []

        async def fulfil(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        async def sends(count):
            for _ in range(80):
                if len(sent) >= count:
                    return
                await page.wait_for_timeout(50)

        async def reply(text):
            await page.evaluate(_TURN + "(['thinking', null])")
            await page.evaluate(_TURN + f"(['idle', '🔊 {text}'])")

        menus = []

        async def pick(mode):
            await _hold(page, mic)
            menus.append(await page.evaluate("""() => { const r = document.querySelector(
                '[data-voice-mic-menu]').getBoundingClientRect();
                return r.left >= 0 && r.right <= innerWidth; }"""))
            await page.locator(f'[data-voice-mic-mode="{mode}"]').click()

        def state_is(state):
            return f"() => ({_VOICE_STATE})() === '{state}'"

        tracks = "() => window.__mic ? __mic.stream.getTracks().map((t) => t.readyState) : []"
        await page.route("**/sessions/codex:test:sid/inject", fulfil)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        button = page.locator("form.inject-form [data-voice-button]")
        mic = page.locator("form.inject-form [data-voice-mic]")
        await button.wait_for(state="visible")
        # A crowded phone composer (an issue chip, Stop) puts 🎤 far left; its menu
        # still opens on the screen (#3540 live check).
        await page.evaluate("""() => document.querySelector('form.inject-form .voice-control')
            .insertAdjacentHTML('afterend', '<span style="flex: none; width: 150px"></span>')""")

        # 🎧 alone: it says it is on, reads a reply, and nothing listens after it.
        await button.click()
        await _until(page, "it said it is on", lambda: said == ["Voice mode on."])
        await reply("Read with the mic off.")
        await _until(page, "the reply was read", lambda: "Read with the mic off." in said)
        await wait_until(page, "nothing listens", state_is("idle"), timeout=6000)
        reads_only = [await page.evaluate(tracks), await page.evaluate(
            "document.querySelector('[data-voice-mic]').hasAttribute('data-voice-hears')")]

        # 🎧 tapped while a reply is read stops only that reading: 🎧 stays on and
        # voice mode with it, and the bubble says what the tap did (#3779).
        host.hold = True
        await reply("A long reply to stop.")
        await wait_until(page, "it reads the long reply", state_is("speaking"), timeout=6000)
        await button.click()
        host.hold = False
        await wait_until(page, "the reading stopped", state_is("idle"))
        hushed = [await button.get_attribute("aria-pressed"),
                  await page.locator(".voice-control .speech-status").text_content(),
                  await page.evaluate("document.documentElement.classList.contains('voice-on')")]

        # 🎤 tapped: it listens for one message at once, which asks for a 🔊 line
        # while 🎧 reads, and then waits for "Hey Jarvis".
        await mic.click()
        await wait_until(page, "🎤 listens", _LISTENING)
        await host.hear("add a test")
        await sends(1)
        await wait_until(page, "it waits for Hey Jarvis", state_is("waiting"), timeout=8000)
        # Talk in the room is let go of while the agent works, and after the reply it
        # waits again; the words after the name are sent.
        await page.evaluate(_TURN + "(['thinking', null])")
        await host.hear("what shall we eat today")
        await page.wait_for_timeout(1500)
        await page.evaluate(_TURN + "(['idle', 'Done.'])")
        await page.wait_for_timeout(1500)
        chatter = [len(sent), await page.evaluate(f"({_VOICE_STATE})()")]
        await host.hear("Hey Jarvis, add a label.")
        await sends(2)
        await wait_until(page, "it waits after the send", state_is("waiting"), timeout=8000)
        # A tap while it waits listens for one message without the name; a tap while
        # it listens sends what was heard at once and goes back to waiting.
        # Tapped while the words are still with the host, they go once back.
        await mic.click()
        await wait_until(page, "a tap listens", _LISTENING)
        host.wait = asyncio.Event()
        await host.hear("run the tests")
        await mic.click()
        host.wait.set()
        host.wait = None
        await sends(3)
        await wait_until(page, "a tap sent it and waits", state_is("waiting"), timeout=8000)
        # Tapped while still talking, the phrase ends there and is sent.
        await mic.click()
        await wait_until(page, "a tap listens again", _LISTENING)
        host.queue.append("and the lint")
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(500)
        await mic.click()
        await page.evaluate("__mic.level(0)")
        await sends(4)
        await wait_until(page, "it waits again", state_is("waiting"), timeout=8000)

        # Always, from the menu: 🎧 off, a reply is not read, and it listens once the
        # turn ends; the message then asks for no 🔊 line.
        await pick("open")
        await wait_until(page, "Always listens", _LISTENING)
        await button.click()
        silent_tap = await button.get_attribute("aria-pressed")
        said_before = len(said)
        await reply("Not read.")
        await wait_until(page, "it listens after the unread reply", _LISTENING, timeout=6000)
        await host.hear("and the docs")
        await sends(5)
        unread = said[said_before:]

        # Off, from the menu: the microphone is let go of, and with 🎧 off voice mode ends.
        await wait_until(page, "it listens after the send", _LISTENING, timeout=8000)
        await pick("off")
        voice_off = "() => !document.documentElement.classList.contains('voice-on')"
        await wait_until(page, "voice mode ended", voice_off)
        off = await page.evaluate(tracks)

        # A fresh page starts in Jarvis, and the device's old mode is dropped.
        await page.evaluate("localStorage.setItem('agentdeck.voiceListenMode', 'dictate')")
        await page.reload()
        await mic.wait_for(state="visible")
        await mic.click()
        await wait_until(page, "a tap listens through voice mode", _LISTENING)
        fresh = [await page.evaluate("localStorage.getItem('agentdeck.voiceListenMode')"),
                 await mic.get_attribute("aria-pressed")]
        await _hold(page, mic)
        ticked = await page.evaluate("""() => [...document.querySelectorAll(
            '[data-voice-mic-mode]')].map((b) => b.dataset.voiceMicMode + ':'
            + b.getAttribute('aria-checked'))""")
        await browser.close()

    def has(body, words):
        return words in body or words.replace(" ", "+") in body

    assert reads_only == [[], False]
    assert hushed == ["true", "Stopped reading", True]
    assert menus == [True, True]
    assert VOICE_LINE.split(" ")[0] in sent[0] and has(sent[0], "add a test")
    assert chatter == [1, "waiting"]
    assert has(sent[1], "add a label") and "Jarvis" not in sent[1]
    assert has(sent[2], "run the tests")
    assert has(sent[3], "and the lint")
    assert silent_tap == "false"
    assert unread == []
    assert VOICE_LINE.split(" ")[0] not in sent[4] and has(sent[4], "and the docs")
    assert len(sent) == 5
    assert off == ["ended"]
    assert fresh == [None, "false"]
    assert ticked == ["jarvis:true", "open:false", "off:false"]


@pytest.mark.browser
async def test_voice_mode_can_be_talked_over_while_the_host_reads(tmp_path):
    """Reading with the host's voice, a cough or held noise does not pause it, nor
    does its own echo (#3467): sound held 0.4 s is checked for words, and only words
    pause it, while the sound still goes on. Words heard out as echo resume it a
    second back; "stop" alone stops it and sends nothing; and other words, even
    mostly the reading's own, stop it and are sent as the person's message (#3426).
    Hidden, a slow transcription is not taken for stalled speech. A reply is asked
    for a sentence at a time: the first plays before the next is made, and the next
    plays once it is (#3480)."""
    import asyncio
    import json

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    app.state.templates.env.globals["speech_language"] = "auto"
    short, long = _silent_wav(), _silent_wav(seconds=20)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_NO_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        await context.add_init_script(_VISIBILITY)
        # Where each playing of the host's voice starts, in seconds.
        await context.add_init_script("""
            // and how far in each stop came, by the audio clock.
            window.__plays = [];
            window.__starts = 0;
            window.__stops = 0;
            window.__stoppedAt = [];
            const start = AudioBufferSourceNode.prototype.start;
            AudioBufferSourceNode.prototype.start = function (when, offset) {
              __starts += 1;
              if (this.buffer && this.buffer.duration > 1) {
                __plays.push(offset || 0);
                this.__from = this.context.currentTime - (offset || 0);
              }
              return start.apply(this, arguments);
            };
            const stop = AudioBufferSourceNode.prototype.stop;
            AudioBufferSourceNode.prototype.stop = function () {
              if (this.buffer && this.buffer.duration > 1) {
                __stops += 1;
                __stoppedAt.push(this.context.currentTime - this.__from);
              }
              return stop.apply(this, arguments);
            };
        """)
        page = await context.new_page()
        sent, phrases = [], []
        # What each transcription hears, in turn: a check of the sound so far, then
        # the phrase once it ends. With none left, nothing.
        answers = []
        slow = {"s": 0}
        said, made = [], asyncio.Event()

        async def inject(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(
                status=202, body='<div id="inject-result" class="inject-result"></div>',
                content_type="text/html", headers={"X-AgentDeck-Action-State": "accepted"},
            )

        async def say(route):
            text = json.loads(route.request.post_data)["text"]
            said.append(text)
            if text == "Made later.":
                await made.wait()
            body = long if text.startswith("The tests passed") else short
            await route.fulfill(status=200, body=body, content_type="audio/wav")

        async def transcribe(route):
            text = answers.pop(0) if answers else ""
            phrases.append(text)
            await asyncio.sleep(slow["s"])
            await route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"text": text, "language": "en"}),
            )

        await page.route("**/sessions/codex:test:sid/inject", inject)
        await page.route("**/api/speech/say", say)
        await page.route("**/api/speech/transcribe*", transcribe)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        button = page.locator("form.inject-form [data-voice-button]")
        await button.wait_for(state="visible")
        await _voice_on(page)
        state = ("() => document.querySelector('[data-voice-button]')"
                 ".getAttribute('data-voice-state')")
        listening = f"() => ({state})() === 'listening'"
        speaking = f"() => ({state})() === 'speaking'"
        await wait_until(page, "it listens after saying it is on", listening)

        # The first sentence plays while the next is still being made; the next
        # plays once it arrives, and then it listens.
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate("__starts = 0")
        await page.evaluate(_TURN + "(['idle', '🔊 First one. Made later.'])")
        for _ in range(40):
            if "Made later." in said:
                break
            await page.wait_for_timeout(100)
        await page.wait_for_timeout(500)
        split = [said[-2:], await page.evaluate("__starts"), await page.evaluate(f"({state})()")]
        made.set()
        await wait_until(page, "it listens after both sentences", listening)
        both = await page.evaluate("__starts")

        # Sound for `ms`, transcribed as `texts` in turn; returns once the phrase's
        # own transcription is asked for and whether the reading paused meanwhile.
        # Timed in the page: from here a 250 ms "stop" could outlast the 0.4 s check
        # under load, which then took its words (#3551).
        async def talk_over(ms, *texts):
            answers[:] = list(texts)
            asked = len(phrases)
            stops = await page.evaluate("__stops")
            await page.evaluate(f"__mic.pulse(0.5, {ms})")
            await page.wait_for_timeout(ms)
            paused = await page.evaluate("__stops") > stops
            for _ in range(40):
                if len(phrases) > asked + len(texts) - 1:
                    break
                await page.wait_for_timeout(100)
            return paused

        reply = "🔊 The tests passed and the branch is merged. Nothing else changed."
        echo = "the tests passed and the branch"

        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + f"(['idle', {json.dumps(reply)}])")
        await wait_until(page, "it reads the reply", "() => __plays.length === 1")
        await page.wait_for_timeout(1800)
        # A cough, again and again, held noise, and the reading's own echo: it is
        # never paused.
        quiet = [await talk_over(150, "") for _ in range(3)]
        quiet.append(await talk_over(1300, "", ""))
        quiet.append(await talk_over(1000, echo, echo))
        await page.wait_for_timeout(300)
        unpaused = [await page.evaluate("__plays.length"), await page.evaluate("__stops"),
                    await page.evaluate(f"({state})()")]
        # Words that turn out to be echo once heard out: it pauses while they are
        # said, then resumes a second before the pause.
        early = await talk_over(1000, "hello there", echo)
        await wait_until(page, "it reads on", "() => __plays.length === 2")
        resumed = [await page.evaluate("__stoppedAt[0] - __plays[1]"),
                   await page.evaluate(f"({state})()")]
        # Hidden, a pause the host takes 6 s to hear out is not taken for a browser
        # that stopped speaking: it reads on.
        await page.evaluate(_HIDE + "(true)")
        answers[:] = ["hello there", echo]
        await page.evaluate("__mic.level(0.5)")
        await wait_until(page, "it pauses hidden", "() => __stops === 2", timeout=5000)
        slow["s"] = 6
        await page.evaluate("__mic.level(0)")
        await wait_until(page, "it reads on after a slow pause hidden",
                         "() => __plays.length === 3", timeout=10000)
        slow["s"] = 0
        hidden = await page.evaluate(f"({state})()")
        await page.evaluate(_HIDE + "(false)")

        # "stop" alone, too short to be checked: the reading stops, nothing is sent,
        # and it listens.
        await talk_over(250, "Stop.")
        await wait_until(page, "it listens after stop", listening, timeout=5000)
        stopped = [len(sent), await page.evaluate(
            "() => document.querySelector('form.inject-form textarea').value")]

        # Other words, even mostly the reading's own: the reading stops and they are
        # sent as the message.
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + f"(['idle', {json.dumps(reply + ' Again.')}])")
        await wait_until(page, "it reads the next reply", speaking)
        await page.wait_for_timeout(1000)
        await talk_over(1000, "The branch is", "The branch is not merged.")
        await wait_until(page, "it listens for the rest", listening, timeout=5000)
        for _ in range(40):
            if sent:
                break
            await page.wait_for_timeout(100)
        await browser.close()

    assert split == [["First one.", "Made later."], 1, "speaking"] and both == 2
    assert quiet == [False] * 5 and unpaused == [1, 0, "speaking"]
    assert early is True
    assert phrases[:7] == ["", "", "", "", "", echo, echo]
    assert phrases[-1] == "The branch is not merged."
    assert 0.9 < resumed[0] < 1.1 and resumed[1] == "speaking"
    assert stopped == [0, ""]
    assert hidden == "speaking"
    assert len(sent) == 1
    assert "branch+is+not+merged" in sent[0] or "branch is not merged" in sent[0]
    # The reply talked over was not heard: the message says so, naming the last line
    # heard in full, not the one cut off (#3520).
    assert "which was: “First one. Made later.”." in sent[0]


@pytest.mark.browser
async def test_dictation_listens_through_the_host_and_falls_back_to_the_browser(tmp_path):
    """With a speech service, 🎤 shows even with no browser recognizer (Firefox): each
    phrase cut at a pause goes to the host and lands in the draft unsent, and the
    phrase being spoken when 🎤 is tapped off still lands. A dead service hands
    listening to the browser's recognizer where there is one (#3422)."""
    import asyncio
    import json

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        sent, phrases = [], []
        heard = ["Fix the login bug.", "and add a test"]
        host = {"up": True}

        async def inject(route):
            sent.append(route.request.post_data or "")
            await route.fulfill(status=202, body="", content_type="text/html")

        async def transcribe(route):
            phrases.append(route.request.post_data_buffer[:4])
            if not host["up"]:
                await route.fulfill(status=503, body="{}", content_type="application/json")
                return
            if host.get("hold"):
                await host["hold"].wait()
            await page.wait_for_timeout(300)
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"text": heard[len(phrases) - 1], "language": "en"}))

        cut = asyncio.Event()

        async def quick(route):
            # The first is held until the next phrase is cut, so the two land together.
            phrases.append(route.request.post_data_buffer[:4])
            if heard[len(phrases) - 1] == "it fails":
                await cut.wait()
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"text": heard[len(phrases) - 1], "language": "en"}))

        async def speak(ms=400):
            await page.evaluate("__mic.level(0.5)")
            await page.wait_for_timeout(ms)
            await page.evaluate("__mic.level(0)")

        draft = "() => document.querySelector('form.inject-form textarea').value"
        bubble = "form.inject-form .speech-control:not(.voice-control) .speech-status"
        context = await browser.new_context(service_workers="block")
        await context.add_init_script(_NO_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        await context.route(_VOICE_MODE_JS, lambda route: route.abort())
        page = await context.new_page()
        await page.route("**/sessions/codex:test:sid/inject", inject)
        await page.route("**/api/speech/transcribe*", transcribe)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        mic = page.locator("form.inject-form [data-speech-button]")
        await mic.click()
        await wait_until(page, "the microphone is recording", "() => !!window.__mic")
        status = await page.locator(bubble).text_content()
        # A cold start (#3722): a phrase slow to come back says the service is
        # starting while it loads, and the bubble goes back to listening after.
        host["hold"] = asyncio.Event()
        await page.route("**/api/speech/status", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"loaded": false}'))
        await speak()
        await wait_until(page, "the bubble says the service is starting",
                         f"() => document.querySelector('{bubble}').textContent"
                         ".startsWith('Starting speech service… ')", timeout=5000)
        host["hold"].set()
        host["hold"] = None
        await wait_until(page, "the first phrase landed",
                         draft + " === 'Fix the login bug.'", timeout=5000)
        listening_again = await page.locator(bubble).text_content()
        # Tapped off mid-phrase: what was being said is still transcribed, and an
        # unpunctuated phrase gets its full stop and capital at once.
        # Two quick phrases, the second landing well inside 0.6 s of the first, each
        # unpunctuated, still end as two sentences.
        heard[1:1] = ["it fails", "on mobile"]
        await page.route("**/api/speech/transcribe*", quick)
        # The pause between them is well past the 0.8 s that cuts a phrase: at 0.9 s
        # a slow machine heard one phrase (#3433).
        await speak()
        await page.wait_for_timeout(1500)
        await speak()
        await page.wait_for_timeout(1500)
        cut.set()
        await wait_until(page, "two quick phrases landed",
                         draft + " === 'Fix the login bug. It fails. On mobile.'", timeout=5000)
        await page.unroute("**/api/speech/transcribe*", quick)
        await page.evaluate("__mic.level(0.5)")
        await page.wait_for_timeout(400)
        await mic.click()
        pressed = await mic.get_attribute("aria-pressed")
        await wait_until(page, "the last phrase landed after the tap",
                         draft + " === 'Fix the login bug. It fails. On mobile. And add a test.'",
                         timeout=5000)
        tracks = await page.evaluate("() => __mic.stream.getTracks().map((t) => t.readyState)")

        # A dead service says so, and with no browser recognizer the next tap tries
        # the host again rather than doing nothing.
        host["up"] = False
        await mic.click()
        await wait_until(page, "recording again", "() => __mic.stream.active")
        await speak()
        await wait_until(page, "the failure is shown with a Retry",
                         f"() => document.querySelector('{bubble}').textContent"
                         " === 'Speech service unreachable · Retry'", timeout=5000)
        # The next tap tries the host again, sending the phrase it kept first (#3722).
        await mic.click()
        await wait_until(page, "the kept phrase was sent again",
                         "() => document.querySelector('" + bubble + " [data-speech-retry]')",
                         timeout=5000)
        retried = len(phrases)
        # The service is back: Retry lands the lost phrase and listens on.
        heard[:] = heard + [""] * (len(phrases) - len(heard)) + [
            "it was lost", "and this too", "sent too soon"]
        host["up"] = True
        await page.locator(f"{bubble} [data-speech-retry]").click()
        await wait_until(page, "the kept phrase landed",
                         draft + " === 'Fix the login bug. It fails. On mobile. And add a test."
                         " It was lost.'", timeout=5000)
        await speak()
        await wait_until(page, "listening went on",
                         draft + " === 'Fix the login bug. It fails. On mobile. And add a test."
                         " It was lost. And this too.'", timeout=5000)
        recovered = await mic.get_attribute("aria-pressed")
        # A send while a phrase is still being transcribed (a cold start) drops its
        # late words but keeps its audio for a Retry.
        host["hold"] = asyncio.Event()
        await speak()
        for _ in range(50):
            if len(phrases) == 9:
                break
            await page.wait_for_timeout(100)
        async with page.expect_response("**/inject"):
            await page.locator("form.inject-form textarea").press("Enter")
        kept = await page.locator(bubble).text_content()
        host["hold"].set()
        await page.wait_for_timeout(500)
        after_send = await page.evaluate(draft)
        host["up"] = False
        await context.close()

        # Chrome, with a dead service: the browser's recognizer takes over.
        context = await browser.new_context(service_workers="block")
        await context.add_init_script(_FAKE_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        await context.route(_VOICE_MODE_JS, lambda route: route.abort())
        page = await context.new_page()
        await page.route("**/api/speech/transcribe*", transcribe)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.locator("form.inject-form [data-speech-button]").click()
        await wait_until(page, "the microphone is recording", "() => !!window.__mic")
        await speak()
        await wait_until(page, "the browser's recognizer took over",
                         "() => __speech.made.length === 1", timeout=5000)
        fallback = await page.locator(bubble).text_content()
        await page.evaluate("__speech.emit([['add a test', true]])")
        typed = await page.evaluate(draft)
        await browser.close()

    assert status == "Listening"
    assert listening_again == "Listening"
    assert pressed == "false"
    assert tracks == ["ended"]
    assert phrases[:4] == [b"RIFF"] * 4 and len(phrases) == 10
    assert retried == 6
    assert recovered == "true"
    assert len(sent) == 1 and "It was lost. And this too." in sent[0]
    assert kept == "Not transcribed · Retry"
    assert after_send == ""
    assert fallback == "Host unreachable · Suomi · Retry"
    assert typed == "Add a test"


@pytest.mark.browser
async def test_voice_mode_never_uses_the_browsers_own_speech(tmp_path):
    """Voice mode listens and speaks only through the deck's speech service (#3470),
    in a browser with speech of its own too (Chrome). With no service there is no 🎧.
    A service that cannot speak, or cannot hear once its retries run out, turns voice
    mode off and says why; the browser's voice and recognizer are never touched."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=1000)
    globals_ = app.state.templates.env.globals
    wav = _silent_wav()
    got = {}
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        current = {}
        for case in ("no service", "cannot speak", "cannot hear"):
            current["case"] = case
            globals_["host_speech"] = case != "no service"
            context = await browser.new_context(
                viewport={"width": 360, "height": 780}, service_workers="block"
            )
            await context.add_init_script(_FAKE_RECOGNITION)
            await context.add_init_script(_FAKE_SYNTHESIS)
            await context.add_init_script(_FAKE_MICROPHONE)
            page = await context.new_page()

            async def say(route):
                if current["case"] == "cannot speak":
                    await route.fulfill(status=503, body="{}", content_type="application/json")
                else:
                    await route.fulfill(status=200, body=wav, content_type="audio/wav")

            async def transcribe(route):
                await route.fulfill(status=503, body="{}", content_type="application/json")

            await page.route("**/api/speech/say", say)
            await page.route("**/api/speech/transcribe*", transcribe)
            await page.goto(f"{base_url}/sessions/codex:test:sid")
            button = page.locator("form.inject-form [data-voice-button]")
            if case == "no service":
                await page.locator("form.inject-form [data-speech-button]").wait_for()
                got[case] = await button.count()
                await context.close()
                continue
            await _voice_on(page)
            if case == "cannot hear":
                await wait_until(page, "it listens", _LISTENING)
                await page.evaluate("__mic.level(0.5)")
                await page.wait_for_timeout(400)
                await page.evaluate("__mic.level(0)")
            await wait_until(page, "voice mode turned off", """() => document.querySelector(
                '[data-voice-button]').getAttribute('aria-pressed') === 'false'""", timeout=15000)
            got[case] = await page.evaluate(
                """() => [document.querySelector('.voice-control .speech-status').textContent,
                          __voice.spoken.length, __speech.made.length]""")
            await context.close()
        await browser.close()

    assert got == {
        "no service": 0,
        "cannot speak": ["Speech service unreachable", 0, 0],
        "cannot hear": ["Speech service unreachable", 0, 0],
    }


@pytest.mark.browser
async def test_the_deck_chat_loads_and_listens_with_the_browsers_own_speech_api(tmp_path):
    """No fakes: Chromium's own speech objects. Asking for on-device recognition
    (`SpeechRecognition.available()`, `processLocally`) crashed every page in Chromium
    149 (#3403 live check), and every test above replaces the API, so none saw it.
    Only 🎤 uses them since #3470: voice mode needs the deck's speech service."""
    from conftest import async_playwright
    from test_inject import _live_server, _web_app

    from agentdeck import meta_chat

    app = _web_app(tmp_path)
    app.state.db.record_deck_chat_start("first", meta_chat.FIRST)
    app.state.app_state.budget_db.record_session_token("first", "codex:test:sid")
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(service_workers="block")
        page = await context.new_page()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.wait_for_timeout(500)
        await page.locator("form.inject-form [data-speech-button]").click(timeout=5000)
        await page.wait_for_timeout(500)
        alive = await page.evaluate("1 + 1")
        await browser.close()

    assert alive == 2


@pytest.mark.browser
async def test_host_listening_pauses_when_a_background_tab_loses_its_audio(tmp_path):
    """A phone suspends a hidden tab's audio: the host recording gets no samples, and
    voice mode pauses with the background reason instead of seeming to listen
    (#3408 with #3409). Before that, a short reply read while hidden waits out a
    restarting speech service to its last retry rather than pausing (#3424 review)."""
    import json

    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    wav = _silent_wav()
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_NO_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        await context.add_init_script(
            """(() => {
              const Real = window.AudioContext;
              window.__contexts = [];
              window.AudioContext = class extends Real {
                constructor(...a) { super(...a); window.__contexts.push(this); }
              };
            })();"""
        )
        page = await context.new_page()

        says = []

        async def say(route):
            says.append(json.loads(route.request.post_data)["text"])
            if says[-1] == "OK." and says.count("OK.") <= 3:
                await route.fulfill(status=503, body="{}", content_type="application/json")
                return
            await route.fulfill(status=200, body=wav, content_type="audio/wav")

        await page.route("**/api/speech/say", say)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await _voice_on(page)
        await wait_until(page, "it listens through the host", """() => document
            .querySelector('[data-voice-button]').getAttribute('data-voice-state')
            === 'listening' && window.__mic""")
        await page.evaluate(
            """() => {
              Object.defineProperty(document, 'hidden', { value: true, configurable: true });
              document.dispatchEvent(new Event('visibilitychange'));
            }"""
        )
        await page.evaluate(_TURN + "(['thinking', null])")
        await page.evaluate(_TURN + "(['idle', '🔊 OK.'])")
        await wait_until(page, "the short reply was read on the last retry", """() =>
            document.querySelector('[data-voice-button]').getAttribute('data-voice-state')
            === 'listening' && document.querySelector('.voice-control .speech-status')
            .textContent === 'Listening…'""", timeout=16000)
        await page.evaluate(
            """() => {
              Object.defineProperty(document, 'hidden', { value: false, configurable: true });
              document.dispatchEvent(new Event('visibilitychange'));
            }"""
        )
        await page.evaluate(
            """() => {
              Object.defineProperty(document, 'hidden', { value: true, configurable: true });
              document.dispatchEvent(new Event('visibilitychange'));
              __contexts.forEach((ctx) => ctx.suspend());
            }"""
        )
        await wait_until(page, "it paused with the reason", """() => document
            .querySelector('[data-voice-button]').getAttribute('data-voice-state')
            === 'resting'""", timeout=8000)
        got = await page.evaluate(
            """() => [document.querySelector('[data-voice-button]').getAttribute('aria-pressed'),
                      document.querySelector('.voice-control .speech-status').textContent]"""
        )
        await browser.close()

    assert says.count("OK.") == 4
    assert got == ["true", "Paused · can’t listen in background"]


@pytest.mark.browser
async def test_a_microphone_granted_after_a_mute_is_let_go_of_at_once(tmp_path):
    """The microphone opens with the 🎤 tap and may answer late; 🎤 tapped off meanwhile,
    the late stream is stopped rather than left recording (#3409 review, #3540)."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=1000)
    app.state.templates.env.globals["host_speech"] = True
    wav = _silent_wav()
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        await context.add_init_script(_NO_RECOGNITION)
        await context.add_init_script(_FAKE_MICROPHONE)
        # The permission answers only when the test says so.
        await context.add_init_script(
            """(() => {
              const open = navigator.mediaDevices.getUserMedia;
              navigator.mediaDevices.getUserMedia = (c) => new Promise((resolve) => {
                window.__grant = () => open(c).then(resolve);
              });
            })();"""
        )
        page = await context.new_page()

        async def say(route):
            await route.fulfill(status=200, body=wav, content_type="audio/wav")

        await page.route("**/api/speech/say", say)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await _voice_on(page)
        state = ("() => document.querySelector('[data-voice-button]')"
                 ".getAttribute('data-voice-state')")
        await wait_until(page, "it listens, the microphone still pending",
                         f"() => ({state})() === 'listening' && window.__grant && !window.__mic")
        await _pick(page, "off")
        await wait_until(page, "listening is off", f"() => ({state})() === 'idle'")
        await page.evaluate("__grant()")
        await wait_until(page, "the late stream arrived", "() => !!window.__mic")
        await page.wait_for_timeout(200)
        got = await page.evaluate(
            f"() => [({state})(), __mic.stream.getTracks().map((t) => t.readyState)]")
        await browser.close()

    assert got == ["idle", ["ended"]]



def test_status_marks_a_chat_kept_working_only_by_background_work():
    """#3529: voice mode reads a reply whose turn ended with a helper agent still running."""
    from dataclasses import replace

    from fastapi.templating import Jinja2Templates

    from agentdeck.models import Session, SessionStatus
    from agentdeck.web import render

    templates = Jinja2Templates(directory=str(Path(render.__file__).parent / "templates"))
    render.register_filters(templates)
    status = render.SESSION_FRAGMENTS_BY_NAME["status"]
    turn = Session(
        key="claude_code:main:sid",
        account_key="claude_code:main",
        session_id="sid",
        status=SessionStatus.LIVE,
        thinking=True,
        activity="Working",
    )
    # The chat's own turn ended; the deck still shows it working through a helper
    # agent or a queued job it counts as the chat's (the live case on #3529).
    ended = replace(turn, thinking=False, activity=None)
    seen: dict = {}
    tags = []
    for current, effective in (
        (turn, turn),
        (replace(turn, activity="Background agent"),) * 2,
        (replace(turn, activity="Background task"),) * 2,
        (ended, replace(ended, thinking=True, activity="Working")),
        (turn, turn),
        # A turn just started: busy from its first writes before the snapshot says
        # the chat is thinking, with no background work. Not ended.
        (ended, ended),
    ):
        view = render.SessionView(current=current, effective=effective, busy=True)
        tags.append((status.changed(view, seen), status.render(templates, view)))

    assert [changed for changed, _ in tags] == [True, True, False, False, True, False]
    assert "data-background" not in tags[0][1] + tags[4][1] + tags[5][1]
    assert all('class="status-tag thinking" data-background>' in html for _, html in tags[1:4])
