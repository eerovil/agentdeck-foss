"""Voice mode's speech service on the AgentDeck host (#3409).

Speech to text and text to speech with Piper on the CPU, over plain HTTP on a Unix
socket that only the deck talks to. Nothing leaves the host: the models are files on
disk. Speech to text is one of two engines, chosen by the deck config's
``[speech] engine`` (#3509; ``--engine`` overrides it):

- ``whisper``: faster-whisper on the graphics card, transcribing as one language,
  with the model ``[speech] whisper_model`` and beam ``whisper_beam`` (large-v3
  turbo with beam 1 and ``language = "fi"`` since #3517). A phrase it
  cannot transcribe there (the card missing, out of memory, a CUDA error) is
  transcribed with the Parakeet model below instead, so voice mode keeps hearing;
- ``parakeet``: an NVIDIA Parakeet TDT model on the CPU through ``transformers``,
  from the folder ``[speech] parakeet_model`` (Orukeet, picked in #3509). It writes
  each word in the language spoken, so a Finnish phrase keeps its English tech
  words, and it reports the language the deck asked for or heard last.

It runs in its own virtualenv (``scripts/speech_setup.sh``), never in the deck's,
because the GPU libraries are large and pinned (CTranslate2 3.24 with CUDA 11).

It starts at login (or on the first request to ``agentdeck-speech.socket``) and
loads its speech-to-text model at once, behind the socket, and keeps it loaded:
by the owner's decision on #3766, a phrase after an hour's break must be heard at
once, so the model stays on the card (about 1.1 GB). ``/warm``, which the deck
posts as voice mode turns on, loads it again should a start have failed to.
``--idle-exit`` (0, never, by default) still exits after that many idle seconds,
which frees every byte of graphics memory the model and the CUDA context held.

Recognition and Piper each have their own lock (#3766): one model call at a time
per engine, and a phrase heard while the deck reads a reply aloud is not held
behind Piper. Whisper and Parakeet share the recognition lock.

    POST /transcribe   body: 16-bit mono WAV   -> {"text", "language", "seconds", "segments"}
                       segments: ``text`` in its parts, in order, each
                       ``{"text", "unsure"}``: whether Whisper doubted it (#3817);
                       Parakeet has no doubt scores and gives none
                       ?language=en|fi  the language heard last (a hint)
                       ?only=en|fi      the one language to transcribe as (#3417)
    POST /say          {"text", "language", "voice", "speed"} -> audio/wav
                       voice: a Piper voice name, ``[speech] voice_en|fi`` (#3455)
                       speed: how much faster than the voice's own pace (#3543)
    POST /warm                                  -> 202, the model loading behind it
    GET  /status                                -> {"engine", "loaded", "gpu_mib", ...}

Run by hand with ``--socket PATH``; under systemd the socket arrives as fd 3.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import io
import json
import logging
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

log = logging.getLogger("agentdeck-speech")

LANGUAGES = ("en", "fi")
# The deck sends ``[speech] voice_en|fi`` with each request; these are for one that
# names none. Keep them equal to that setting's defaults (#3455).
DEFAULT_VOICES = {"en": "en_US-ryan-high", "fi": "fi_FI-harri-medium"}
VOICE_NAME = re.compile(r"[A-Za-z0-9_-]+")
SAMPLE_RATE = 16000
# Whisper's habit on noise and breath: closing lines of the videos it learned from.
# Nobody says these to the deck, so a segment that is one of them, give or take
# punctuation, is dropped.
PHANTOM_LINES = {
    "thanks for watching",
    "thank you for watching",
    "thanks for watching and ill see you in the next video",
    "see you in the next video",
    "please subscribe",
    "subtitles by the amaraorg community",
    "kiitos katsomisesta",
    "kiitos kun katsoit",
}
# Short words Whisper also makes up, but that a person may really say: dropped
# only when Whisper itself is unsure there was speech.
PHANTOM_WORDS = {"you", "thank you", "thanks", "mmhmm", "kiitos", "tekstitys"}
# Hums and hesitation sounds: Whisper writes noise as "MMM!" (#3439), and none of
# these is an answer on its own, so a segment of nothing else is always dropped.
# "Mm-hmm" stays a word above, however it is spaced: a person may mean yes by it.
HUM = re.compile(r"h?m+|hm+|u+[hm]+|a+h+|e+[hr]+m*|ö+h?|ä+h?")
YES_HUM = re.compile(r"m+h+m+")
UNSURE_NO_SPEECH = 0.2
# Whisper's own doubt in its words. Below it, misheard Finnish comes out as made-up
# words ("Igu su aseto sorriva.", -0.73), while clear phrases sit above -0.4; a
# segment below it is kept but named unsure, so the agent can ask (#3817). Every
# temperature fallback in two days of the owner's speech already scored below it.
UNSURE_LOGPROB = -0.6
# A phrase changes language only when Whisper is sure of it: that language holds
# this share of the English-plus-Finnish odds and this much of all odds. Otherwise
# it keeps the last language heard.
SURE_LANGUAGE = 0.7
SURE_LANGUAGE_FLOOR = 0.3
# Silero VAD closes a stretch of speech after this much quiet. It must be shorter
# than the page's own pause (`HOST_PAUSE_MS` in voice_mode.js, 800 ms), or the
# quiet tail every phrase ends with is kept for Whisper to make words of.
VAD_SILENCE_MS = 400
VAD_PAD_MS = 200
# Silero's own odds reject noise and clicks (measured: none kept even with no
# minimum); this only drops slivers, so a clipped "ei" or "stop" still counts.
VAD_MIN_SPEECH_MS = 100
MAX_BODY = 30 * 1024 * 1024
ENGINES = ("whisper", "parakeet")
CONFIG = "~/.config/agentdeck/config.toml"
# Keep equal to ``[speech] whisper_model``, ``whisper_beam`` and ``whisper_prompt``'s
# defaults. The prompt, the words the owner says most, took the owner's mixed phrases
# from 11.8 % to 10.4 % wrong at the same speed, and stopped turbo turning English
# sentences into Finnish; the same words in a Finnish sentence scored 9.0 % but made
# "Tee pull request" up out of room noise (#3791).
WHISPER_MODEL = "large-v3-turbo"
WHISPER_BEAM = 1
WHISPER_PROMPT = (
    "AgentDeck, Haiku, Codex, Opus, pull request, deploy, staging, review, merge, issue, "
    "wake word, voice mode, Sentry, webhook, dashboard, commit, push, branch, master, runtime."
)
# Fastest first. The GTX 1660 Super has no fast float16: Whisper large-v3 turbo took
# 4.5 s a phrase in float16 and 0.6 s in int8_float32 (#3517); a card with no int8
# (the GTX 970) gets float32.
COMPUTE_TYPES = ("int8_float32", "float32")
# Keep equal to ``[speech] parakeet_model``'s default.
PARAKEET_MODEL = "~/.local/share/agentdeck/speech/models/orukeet"
# The CPU threads Parakeet runs on; measured at about 0.7 s a phrase on this host.
PARAKEET_THREADS = 4


def preload_cuda_libraries() -> None:
    """Load the pip-installed CUDA 11 libraries by path.

    CTranslate2 opens them by soname, and the loader only searches
    ``LD_LIBRARY_PATH`` as it stood at process start; a library already loaded
    under that soname is used instead, so loading them first is enough.
    """
    site = Path(sys.prefix) / "lib"
    for pattern in ("libcudart.so.11*", "libcublasLt.so.11*", "libcublas.so.11*", "libcudnn*.so.8"):
        for path in sorted(glob.glob(str(site / "python3*/site-packages/nvidia/*/lib" / pattern))):
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError as exc:  # pragma: no cover - only on a broken venv
                log.warning("could not load %s: %s", path, exc)


def gpu_mib() -> int | None:
    """This process's graphics memory in MiB, or None when it holds none."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        pid, _, used = line.partition(",")
        if pid.strip() == str(os.getpid()):
            return int(used.strip())
    return None


def read_wav(data: bytes):
    """16 kHz mono float32 samples from a 16-bit PCM WAV of any rate."""
    import numpy as np

    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM")
        rate, channels = w.getframerate(), w.getnchannels()
        samples = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    samples /= 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(samples):
        count = int(len(samples) * SAMPLE_RATE / rate)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples
        ).astype(np.float32)
    return samples


def speech_only(samples):
    """``samples`` with the stretches Silero VAD hears no speech in cut out, or None.

    Whisper turns noise, breath and clicks into confident sentences; it never sees
    them. faster-whisper ships the model, which runs on the CPU in milliseconds.
    """
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(
        min_speech_duration_ms=VAD_MIN_SPEECH_MS,
        min_silence_duration_ms=VAD_SILENCE_MS,
        speech_pad_ms=VAD_PAD_MS,
    )
    chunks = get_speech_timestamps(samples, options)
    if not chunks:
        return None
    return np.concatenate([samples[chunk["start"] : chunk["end"]] for chunk in chunks])


def choose_language(scores: dict, previous: str = "") -> tuple[str, float]:
    """English or Finnish for a phrase, and the winner's share of the two.

    Only the likelier of the two, so a short "okay" is never taken for Japanese. A
    phrase that is a close call, or that Whisper hears as some third language,
    keeps ``previous``.
    """
    language = max(LANGUAGES, key=lambda code: scores.get(code, 0.0))
    odds = scores.get(language, 0.0)
    share = odds / (sum(scores.get(code, 0.0) for code in LANGUAGES) or 1)
    sure = share >= SURE_LANGUAGE and odds >= SURE_LANGUAGE_FLOOR
    if previous in LANGUAGES and not sure:
        return previous, share
    return language, share


def _said(text: str) -> str:
    words = "".join(ch for ch in text.lower() if ch.isalnum() or ch == " ")
    return " ".join(words.split())


def is_phantom_text(text: str) -> bool:
    """Words no one says to the deck: a known closing line, or only hums. The one
    check both engines make; Parakeet has no doubt scores for the rest (#3509)."""
    said = _said(text)
    if said in PHANTOM_LINES:
        return True
    hums = said and all(HUM.fullmatch(word) for word in said.split())
    return bool(hums) and not YES_HUM.fullmatch(said.replace(" ", ""))


def is_phantom(segment) -> bool:
    """A segment Whisper made up: rated as no speech, a known closing line, only
    hums, or a short stock word it was unsure was spoken."""
    if segment.no_speech_prob > 0.6 and segment.avg_logprob < -0.5:
        return True
    if is_phantom_text(segment.text):
        return True
    unsure = segment.no_speech_prob > UNSURE_NO_SPEECH or segment.avg_logprob < UNSURE_LOGPROB
    return _said(segment.text) in PHANTOM_WORDS and unsure


def compute_type(device: str) -> str:
    """The fastest of ``COMPUTE_TYPES`` CTranslate2 can run on ``device``."""
    import ctranslate2

    supported = ctranslate2.get_supported_compute_types(device)
    return next((name for name in COMPUTE_TYPES if name in supported), "float32")


def configured_engine(path: str) -> dict:
    """``[speech] engine``, ``whisper_model``, ``whisper_beam``, ``whisper_prompt`` and
    ``parakeet_model`` from the deck config at ``path``,
    as defaults for the command line; nothing when the file or keys are missing."""
    import tomllib

    config = Path(path).expanduser()
    if not config.exists():
        return {}
    speech = tomllib.loads(config.read_text()).get("speech", {})
    found = {}
    if speech.get("engine") in ENGINES:
        found["engine"] = speech["engine"]
    if speech.get("parakeet_model"):
        found["parakeet_model"] = str(speech["parakeet_model"])
    if speech.get("whisper_model"):
        found["whisper_model"] = str(speech["whisper_model"])
    if type(speech.get("whisper_beam")) is int and speech["whisper_beam"] > 0:
        found["whisper_beam"] = speech["whisper_beam"]
    # An empty prompt turns it off, so only a missing key keeps the default.
    if isinstance(speech.get("whisper_prompt"), str):
        found["whisper_prompt"] = speech["whisper_prompt"]
    return found


class Speech:
    """The speech-to-text engine and the Piper voices, each loaded on first use."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.whisper = None
        self.parakeet = None
        # Why Whisper could not load, once it failed: Parakeet hears every phrase
        # until the service exits and the next start tries the card again.
        self.whisper_failed = ""
        self.voices: dict[str, object] = {}
        # Whisper and Parakeet hold the recognition lock, Piper the voice lock: a
        # phrase is never held behind a reply being read (#3766).
        self.recognition_lock = threading.Lock()
        self.voice_lock = threading.Lock()
        self.warming: threading.Thread | None = None

    def transcribe(self, data: bytes, previous: str = "", only: str = "") -> dict:
        only = only if only in LANGUAGES else ""
        if self.args.engine == "parakeet" or self.whisper_failed:
            return self.transcribe_parakeet(data, previous, only)
        try:
            return self.transcribe_whisper(data, previous, only)
        except Exception as exc:  # CUDA errors come as RuntimeError, or worse
            log.warning("Whisper failed (%s); Parakeet hears this phrase", exc)
            return self.transcribe_parakeet(data, previous, only)

    def warm(self) -> None:
        """Load the speech-to-text model behind the request, so the first phrase
        after voice mode turns on does not wait for it (#3766). The page asks on
        every phrase too, so a loaded model starts nothing."""
        if self.loaded() or (self.warming is not None and self.warming.is_alive()):
            return
        self.warming = threading.Thread(target=self.load_recognizer, daemon=True)
        self.warming.start()

    def load_recognizer(self) -> None:
        """The engine ``transcribe`` would use, loaded; Parakeet when Whisper fails."""
        try:
            with self.recognition_lock:
                if self.args.engine == "whisper" and not self.whisper_failed:
                    if self.whisper is None:
                        try:
                            self.load_whisper()
                        except Exception as exc:  # the card missing, out of memory
                            log.warning("Whisper failed to load (%s); Parakeet hears", exc)
                if self.args.engine == "parakeet" or self.whisper_failed:
                    if self.parakeet is None:
                        self.parakeet = self.load_parakeet()
        except Exception:  # noqa: BLE001 - the phrase that needs it reports it
            log.exception("speech model warm-up failed")

    def load_whisper(self) -> None:
        started = time.monotonic()
        # Any failure here, the import and the card's probe included, sends every
        # phrase to Parakeet, so a warm-up loads Parakeet next (#3766).
        try:
            from faster_whisper import WhisperModel

            kind = compute_type(self.args.device)
            self.whisper = WhisperModel(self.args.model, device=self.args.device, compute_type=kind)
        except Exception as exc:
            self.whisper_failed = str(exc) or type(exc).__name__
            raise
        log.info(
            "loaded %s on %s as %s in %.1fs, %s MiB of graphics memory",
            self.args.model,
            self.args.device,
            kind,
            time.monotonic() - started,
            gpu_mib(),
        )

    def transcribe_whisper(self, data: bytes, previous: str = "", only: str = "") -> dict:
        samples = read_wav(data)
        with self.recognition_lock:
            if self.whisper is None:
                self.load_whisper()
            started = time.monotonic()
            speech = speech_only(samples)
            if speech is None:
                log.info("no speech in %.1fs of audio", len(samples) / SAMPLE_RATE)
                return {"text": "", "language": only or previous or "en", "seconds": 0.0}
            if only:
                # The deck listens for one language only (#3417): no guess to get wrong.
                language, share = only, 1.0
            else:
                _, _, probabilities = self.whisper.detect_language(speech)
                language, share = choose_language(dict(probabilities), previous)
            segments, _ = self.whisper.transcribe(
                speech,
                language=language,
                beam_size=self.args.beam,
                condition_on_previous_text=False,
                initial_prompt=self.args.prompt or None,
            )
            kept = []
            parts = []
            for segment in segments:
                # The words and Whisper's doubt, so a phantom that got through
                # can be told apart from speech afterwards (#3439).
                phantom = is_phantom(segment)
                log.info(
                    "%s %r (no speech %.2f, logprob %.2f, temperature %.1f)",
                    "dropped" if phantom else "kept",
                    segment.text.strip(),
                    segment.no_speech_prob,
                    segment.avg_logprob,
                    segment.temperature or 0.0,
                )
                if not phantom:
                    kept.append(segment.text.strip())
                    unsure = segment.avg_logprob < UNSURE_LOGPROB
                    parts.append({"text": segment.text.strip(), "unsure": unsure})
            text = " ".join(kept).strip()
        log.info(
            "heard %s (%.0f%% of en+fi) in %.2fs",
            language,
            share * 100,
            time.monotonic() - started,
        )
        return {
            "text": text,
            "language": language,
            "seconds": round(time.monotonic() - started, 2),
            "segments": parts,
        }

    def transcribe_parakeet(self, data: bytes, previous: str = "", only: str = "") -> dict:
        """Parakeet's words for a phrase. It needs no language, so the reply's is the
        one the deck pinned, else the one heard last."""
        language = only or (previous if previous in LANGUAGES else "en")
        samples = read_wav(data)
        with self.recognition_lock:
            if self.parakeet is None:
                self.parakeet = self.load_parakeet()
            started = time.monotonic()
            speech = speech_only(samples)
            if speech is None:
                log.info("no speech in %.1fs of audio", len(samples) / SAMPLE_RATE)
                return {"text": "", "language": language, "seconds": 0.0}
            text = self.parakeet(speech)
            phantom = is_phantom_text(text)
            log.info("%s %r", "dropped" if phantom else "kept", text)
        seconds = round(time.monotonic() - started, 2)
        log.info("heard %.1fs of speech in %.2fs", len(speech) / SAMPLE_RATE, seconds)
        return {"text": "" if phantom else text, "language": language, "seconds": seconds}

    def load_parakeet(self):
        """Samples to text with the Parakeet model in ``--parakeet-model``, on the CPU."""
        import numpy as np
        import torch
        from transformers import AutoProcessor, ParakeetForTDT

        started = time.monotonic()
        folder = Path(self.args.parakeet_model).expanduser()
        torch.set_num_threads(PARAKEET_THREADS)
        model = ParakeetForTDT.from_pretrained(folder).eval()
        processor = AutoProcessor.from_pretrained(folder)
        log.info("loaded %s on the CPU in %.1fs", folder.name, time.monotonic() - started)
        lead = np.zeros(SAMPLE_RATE // 4, np.float32)

        def run(speech) -> str:
            # The quarter second of leading quiet the model was compared with (#3499).
            inputs = processor(
                [np.concatenate([lead, speech])], sampling_rate=SAMPLE_RATE, return_tensors="pt"
            )
            with torch.inference_mode():
                found = model.generate(**inputs)
            return processor.batch_decode(found.sequences, skip_special_tokens=True)[0].strip()

        return run

    def say(self, text: str, language: str, name: str = "", speed: float = 1.0) -> bytes:
        language = language if language in LANGUAGES else "en"
        if not VOICE_NAME.fullmatch(name):
            name = DEFAULT_VOICES[language]
        speed = min(max(speed, 0.5), 3.0)
        with self.voice_lock:
            voice = self.voices.get(name)
            if voice is None:
                from piper import PiperVoice

                path = Path(self.args.voices).expanduser() / f"{name}.onnx"
                voice = self.voices[name] = PiperVoice.load(str(path))
            from piper import SynthesisConfig

            # A shorter phoneme length is a faster voice; scale the voice's own.
            pace = SynthesisConfig(length_scale=voice.config.length_scale / speed)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as out:
                voice.synthesize_wav(text, out, syn_config=pace)
        return buffer.getvalue()

    def hears_with_parakeet(self) -> bool:
        return self.args.engine == "parakeet" or bool(self.whisper_failed)

    def loaded(self) -> bool:
        return (self.parakeet if self.hears_with_parakeet() else self.whisper) is not None

    def status(self) -> dict:
        parakeet = self.hears_with_parakeet()
        return {
            "engine": self.args.engine,
            "whisper_failed": self.whisper_failed,
            "model": self.args.parakeet_model if parakeet else self.args.model,
            "device": "cpu" if parakeet else self.args.device,
            "loaded": self.loaded(),
            "voices_loaded": sorted(self.voices),
            "gpu_mib": gpu_mib(),
            "idle_exit_seconds": self.args.idle_exit,
        }


class Handler(BaseHTTPRequestHandler):
    server: SpeechServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.debug(format, *args)

    def address_string(self) -> str:
        return "unix"

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._reply(status, json.dumps(payload).encode(), "application/json")

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self.server.touch()
        if self.path == "/status":
            self._json(200, self.server.speech.status())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self.server.touch()
        try:
            body = self._body()
            path, _, query = self.path.partition("?")
            if path == "/transcribe":
                params = parse_qs(query)
                previous = params.get("language", [""])[0]
                only = params.get("only", [""])[0]
                self._json(200, self.server.speech.transcribe(body, previous, only))
            elif path == "/warm":
                self.server.speech.warm()
                self._json(202, {"warming": True})
            elif path == "/say":
                request = json.loads(body or b"{}")
                text = str(request.get("text") or "").strip()
                if not text:
                    self._json(400, {"error": "no text"})
                    return
                wav = self.server.speech.say(
                    text,
                    str(request.get("language") or "en"),
                    str(request.get("voice") or ""),
                    float(request.get("speed") or 1.0),
                )
                self._reply(200, wav, "audio/wav")
            else:
                self._json(404, {"error": "not found"})
        except (ValueError, wave.Error, EOFError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the service
            log.exception("speech request failed")
            self._json(500, {"error": str(exc)})
        finally:
            self.server.touch()


class SpeechServer(socketserver.ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(self, sock: socket.socket, speech: Speech) -> None:
        super().__init__("", Handler, bind_and_activate=False)
        self.socket = sock
        self.speech = speech
        self.last_used = time.monotonic()

    def server_bind(self) -> None:  # the socket arrives bound
        pass

    def touch(self) -> None:
        self.last_used = time.monotonic()


def listening_socket(path: str) -> socket.socket:
    """systemd's socket (fd 3) when activated, else a fresh one at ``path``."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3)
    if not path:
        raise SystemExit("no socket: run under agentdeck-speech.socket or pass --socket")
    Path(path).unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    os.chmod(path, 0o600)
    sock.listen(8)
    return sock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default="")
    parser.add_argument("--config", default=os.environ.get("AGENTDECK_CONFIG", CONFIG))
    parser.add_argument("--engine", choices=ENGINES, default=None, help="default [speech] engine")
    parser.add_argument("--parakeet-model", default=None, help="default [speech] parakeet_model")
    parser.add_argument("--model", default=None, help="default [speech] whisper_model")
    parser.add_argument("--device", default="cuda", help="Whisper's device")
    parser.add_argument(
        "--voices", default="~/.local/share/agentdeck/speech/voices", help="Piper voice files"
    )
    parser.add_argument("--idle-exit", type=int, default=0, help="seconds; 0 never exits")
    args = parser.parse_args(argv)
    # The unit names only the device; the engine and models come from the deck
    # config, Parakeet unless it names Whisper (#3509, #3517).
    configured = configured_engine(args.config)
    args.engine = args.engine or configured.get("engine", "parakeet")
    args.parakeet_model = args.parakeet_model or configured.get("parakeet_model", PARAKEET_MODEL)
    args.model = args.model or configured.get("whisper_model", WHISPER_MODEL)
    args.beam = configured.get("whisper_beam", WHISPER_BEAM)
    args.prompt = configured.get("whisper_prompt", WHISPER_PROMPT)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # The setup script downloaded the model; never ask the Hub about it again.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    # Keep no freed GPU memory cached (#3424). When the card runs out of memory,
    # the cub allocator CTranslate2 3.24 ships frees its cache in a loop that steps
    # past an entry it just erased, and the service segfaulted mid-transcription
    # whenever another program held the card's memory. With nothing cached that loop
    # never runs, and running out of memory is an error the request reports. It
    # costs about 0.25 s a phrase with Whisper small on the GTX 970.
    os.environ.setdefault("CT2_CUDA_CACHING_ALLOCATOR_CONFIG", "4,3,12,0")
    if args.engine == "whisper" and args.device == "cuda":
        preload_cuda_libraries()
    server = SpeechServer(listening_socket(args.socket), Speech(args))
    server.timeout = 5
    if args.engine == "parakeet":
        log.info("speech service ready (Parakeet %s on the CPU)", args.parakeet_model)
    else:
        log.info("speech service ready (Whisper %s on %s)", args.model, args.device)
    # Loaded now, not on the first phrase, so that phrase never waits for it (#3766).
    server.speech.warm()
    while True:
        server.handle_request()
        idle = time.monotonic() - server.last_used
        if args.idle_exit and idle >= args.idle_exit:
            log.info("idle for %ds; exiting to free the models", int(idle))
            logging.shutdown()
            # Skip interpreter teardown: CTranslate2 3.24 crashes freeing a CUDA model
            # at exit, which systemd would log as a failed unit.
            os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
