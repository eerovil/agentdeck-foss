"""Voice mode's wake word detector on the AgentDeck host (#3476).

While voice mode waits for its wake word, the page posts every phrase it cuts here
first, and Whisper runs only on a phrase this hears "Hey Jarvis" in. It is
openWakeWord's ready-made ``hey_jarvis`` model on the CPU: a 2 s phrase takes about
0.1 s, and the graphics card stays idle. The wake word is "Hey Jarvis" by the
owner's choice; a custom "Hey deck" would have needed a model trained for it.
A page may ask for "Alexa" instead (``?word=alexa``), openWakeWord's ready-made
``alexa`` model, so a second device in the same house does not wake on the first
one's word. Both models are in openwakeword 0.4.0's wheel.

It runs in the speech service's virtualenv (``scripts/speech_setup.sh`` adds
``openwakeword``), but in a process of its own, so Whisper's service can still exit
when idle and give back its graphics memory while voice mode waits.

    POST /wake    body: 16-bit mono WAV   -> {"woken", "score", "speech_after"}
                  ?threshold=0.5  the score that wakes (``[speech] wake_threshold``)
                  &word=jarvis    which wake word: ``jarvis`` (default) or ``alexa``
    POST /stream  Upgrade: agentdeck-wake-stream  -> 101, then two-way (#4539)
                  ?threshold=0.5&word=jarvis  as for /wake
                  &sure=0.99  a second, higher score to tell of (none without it)
    GET  /status                          -> {"model"}

``speech_after`` says whether speech follows the wake word in the same phrase
("Hey Jarvis, run the tests"), so the page knows to have that phrase transcribed.

``/stream`` is the waiting microphone frame by frame (#4539), so the beep need not
wait for the page's 0.8 s pause: after the ``101`` the deck writes records, each a
type byte, a 4-byte big-endian length and its payload: ``P`` a phrase starts (its
id), ``A`` 16 kHz 16-bit mono audio, ``E`` the phrase ended (its id). This writes
one JSON line per event: ``{"crossed": id, "score", "at"}`` on the first 80 ms frame
of a phrase at or over the threshold, ``at`` its end in seconds into the phrase,
``{"sure": id, "score", "at"}`` likewise for ``sure``, after the crossing, and
``{"end": id, "score", "at"}`` once every frame of the phrase is scored, ``score``
its peak and ``at`` the crossing or null. Records are handled in order, so a
crossing in a phrase's last frames always comes before its ``end``. Each stream has
an openWakeWord model of its own, holding only its word and reset at each phrase,
so streams never share a model's buffers and a phrase scores as the same audio
sent as a clip does. ``STREAMS`` at once; one more is answered 503, and the page
sends clips as before.

It starts at login (or on the first request to ``agentdeck-wake.socket``), loads
the model and Silero at once and stays up: by the owner's decision on #3793, as on
#3766 for Whisper, "Hey Jarvis" must work at once even after an hour's break. A
start costs 3 to 9 s and the first Silero call another 1 to 15 s, which every first
wake after five quiet minutes used to wait for. ``--idle-exit`` (0, never, by
default) still exits after that many idle seconds. Run by hand with
``--socket PATH``.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import socketserver
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from speech_server import SAMPLE_RATE, listening_socket, read_wav

log = logging.getLogger("agentdeck-wake")

MODEL = "hey_jarvis_v0.1"
# The wake words a page may ask for, by the name it asks with: each a model
# openwakeword 0.4.0 ships in its wheel.
MODELS = {"jarvis": MODEL, "alexa": "alexa_v0.1"}
# openWakeWord scores 80 ms frames; a second of silence after the phrase lets the
# score of a wake word said at its very end rise.
FRAME = 1280
TAIL_FRAMES = 12
# Speech after the frame the score peaked on, at least this long, is a message said
# in the same breath as the wake word. Silero closes a stretch after this much quiet;
# shorter than the page's own 0.8 s pause, so a phrase's quiet tail is not speech.
SPEECH_AFTER_MS = 250
VAD_SILENCE_MS = 300
MAX_BODY = 30 * 1024 * 1024
# Pages streaming at once (#4539): the phone, the kitchen phone and the kiosk, and one
# spare for a page that reconnects before the deck notices its old socket is gone.
STREAMS = 4
STREAM_UPGRADE = "agentdeck-wake-stream"
# A stream's longest record: half a second of audio, more than one page chunk.
MAX_RECORD = SAMPLE_RATE


def model_path(model: str = MODEL) -> str:
    """A model openwakeword 0.4.0 ships in its wheel."""
    from pathlib import Path

    import openwakeword

    return str(Path(openwakeword.__file__).parent / "resources" / "models" / f"{model}.onnx")


class Detector:
    def __init__(self) -> None:
        from openwakeword.model import Model

        self.model = Model(wakeword_model_paths=[model_path(m) for m in MODELS.values()])
        # The model's own name for each word's scores (the file name without ".onnx").
        self.names = {word: model for word, model in MODELS.items()}
        missing = set(self.names.values()) - set(self.model.models)
        if missing:
            raise RuntimeError(f"wake word models not loaded: {sorted(missing)}")
        # One phrase at a time: the model keeps its audio buffer between frames.
        self.lock = threading.Lock()
        # Streams' own models (#4539): those in use, and idle ones kept by word.
        self.streams = 0
        self.idle: dict[str, list] = {}

    def stream(self, word: str, threshold: float, sure: float | None = None) -> Stream:
        """A model of its own for one page's stream, or ``Busy`` past ``STREAMS``."""
        with self.lock:
            if self.streams >= STREAMS:
                raise Busy(f"{STREAMS} wake streams already")
            self.streams += 1
            idle = self.idle.get(word) or []
            model = idle.pop() if idle else None
        try:
            if model is None:
                from openwakeword.model import Model

                model = Model(wakeword_model_paths=[model_path(MODELS[word])])
        except BaseException:
            with self.lock:
                self.streams -= 1
            raise
        return Stream(
            model, MODELS[word], threshold, lambda: self._release(word, model), sure=sure
        )

    def _release(self, word: str, model) -> None:
        with self.lock:
            self.streams -= 1
            self.idle.setdefault(word, []).append(model)

    def scores(self, samples, word: str = "jarvis") -> list[float]:
        import numpy as np

        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        pcm = np.concatenate([pcm, np.zeros(FRAME * TAIL_FRAMES, np.int16)])
        with self.lock:
            self.model.reset()
            return [
                float(self.model.predict(pcm[start : start + FRAME])[self.names[word]])
                for start in range(0, len(pcm) - FRAME + 1, FRAME)
            ]

    def wake(self, data: bytes, threshold: float, word: str = "jarvis") -> dict:
        started = time.monotonic()
        samples = read_wav(data)
        scores = self.scores(samples, word) or [0.0]
        peak = max(range(len(scores)), key=scores.__getitem__)
        woken = scores[peak] >= threshold
        after = woken and speech_after(samples[(peak + 1) * FRAME :])
        log.info(
            "%.1fs phrase: %s score %.3f%s in %.0f ms",
            len(samples) / SAMPLE_RATE,
            word,
            scores[peak],
            " (woken, speech after)" if after else " (woken)" if woken else "",
            (time.monotonic() - started) * 1000,
        )
        return {"woken": woken, "score": round(scores[peak], 4), "speech_after": after}

    def warm(self) -> None:
        """Runs both models once, so the first phrase never waits for them to load:
        Silero's import alone took 12 to 15 s on the first wake of a new process. One
        ``predict`` runs every loaded wake word, so one pass warms them all."""
        import numpy as np

        started = time.monotonic()
        silence = np.zeros(SAMPLE_RATE, np.float32)
        self.scores(silence)
        speech_after(silence)
        log.info("models warm in %.1f s", time.monotonic() - started)


class Busy(Exception):
    """Every stream is taken."""


class Stream:
    """One page's waiting microphone, 80 ms frame by frame (#4539)."""

    def __init__(
        self,
        model,
        name: str,
        threshold: float,
        release,
        decode=None,
        sure: float | None = None,
    ) -> None:
        self.model = model
        self.decode = decode or int16_frame
        self.name = name
        self.threshold = threshold
        # The higher score told of on its own, or None.
        self.sure_at = sure
        self.release = release
        self.phrase = ""
        self.start("")

    def start(self, phrase: str) -> None:
        """A new phrase: the model forgets the last one, as a clip's reset does."""
        self.model.reset()
        self.phrase = phrase
        self.pending = b""
        self.frames = 0
        self.peak = 0.0
        self.crossed: float | None = None
        self.sure = False

    def feed(self, audio: bytes) -> list[dict]:
        """Scores every whole frame in ``audio``; the phrase's first crossing, and its
        first frame at or over ``sure``, when they are in it."""
        data = self.pending + audio
        whole = len(data) // (FRAME * 2) * FRAME * 2
        self.pending = data[whole:]
        events: list[dict] = []
        for start in range(0, whole, FRAME * 2):
            frame = self.decode(data[start : start + FRAME * 2])
            score = float(self.model.predict(frame)[self.name])
            self.frames += 1
            self.peak = max(self.peak, score)
            if self.crossed is None and score >= self.threshold:
                self.crossed = round(self.frames * FRAME / SAMPLE_RATE, 2)
                events.append(
                    {"crossed": self.phrase, "score": round(score, 4), "at": self.crossed}
                )
            crossed = self.crossed is not None
            if crossed and not self.sure and self.sure_at is not None and score >= self.sure_at:
                self.sure = True
                at = round(self.frames * FRAME / SAMPLE_RATE, 2)
                events.append({"sure": self.phrase, "score": round(score, 4), "at": at})
        return events

    def end(self) -> list[dict]:
        """The phrase's last frames, as a clip scores them: the part frame and the
        tail of silence, which a name said at the very end still rises in. Returns
        the events in that tail, if any, then the phrase's end."""
        events = self.feed(bytes(FRAME * 2 * TAIL_FRAMES))
        return [*events, {"end": self.phrase, "score": round(self.peak, 4), "at": self.crossed}]


def int16_frame(data: bytes):
    import numpy as np

    return np.frombuffer(data, "<i2")


def serve_stream(stream: Stream, rfile, wfile, touch=lambda: None) -> None:
    """Records from the deck until it closes the stream; a JSON line per event."""

    def send(event: dict) -> None:
        wfile.write(json.dumps(event).encode() + b"\n")
        wfile.flush()

    while True:
        head = rfile.read(5)
        if len(head) < 5:
            return
        kind, length = head[:1], int.from_bytes(head[1:], "big")
        if length > MAX_RECORD:
            raise ValueError("record too long")
        body = rfile.read(length)
        if len(body) < length:
            return
        touch()
        if kind == b"P":
            stream.start(body.decode("ascii", "replace"))
        elif kind == b"A":
            for event in stream.feed(body):
                log.info(
                    "stream: %s %s %.3f at %.2fs",
                    stream.name,
                    "crossed" if "crossed" in event else "sure",
                    event["score"],
                    event["at"],
                )
                send(event)
        elif kind == b"E":
            for event in stream.end():
                send(event)
            stream.start("")
        else:
            raise ValueError(f"unknown record {kind!r}")


def speech_after(samples) -> bool:
    """Whether Silero hears ``SPEECH_AFTER_MS`` of speech in ``samples``."""
    if len(samples) < SAMPLE_RATE * SPEECH_AFTER_MS // 1000:
        return False
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(min_silence_duration_ms=VAD_SILENCE_MS, speech_pad_ms=0)
    spoken = sum(chunk["end"] - chunk["start"] for chunk in get_speech_timestamps(samples, options))
    return spoken >= SAMPLE_RATE * SPEECH_AFTER_MS // 1000


class Handler(BaseHTTPRequestHandler):
    server: WakeServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.debug(format, *args)

    def address_string(self) -> str:
        return "unix"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self.server.touch()
        if self.path == "/status":
            self._json(200, {"model": MODEL, "models": MODELS})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self.server.touch()
        try:
            path, _, query = self.path.partition("?")
            if path == "/stream":
                self._stream(parse_qs(query))
                return
            if path != "/wake":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("body too large")
            params = parse_qs(query)
            threshold = float(params.get("threshold", ["0.5"])[0])
            if not 0 < threshold < 1:
                raise ValueError("threshold must be between 0 and 1")
            word = params.get("word", ["jarvis"])[0]
            if word not in MODELS:
                raise ValueError(f"unknown wake word {word!r}")
            self._json(200, self.server.detector.wake(self.rfile.read(length), threshold, word))
        except (ValueError, wave.Error, EOFError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the service
            log.exception("wake request failed")
            self._json(500, {"error": str(exc)})
        finally:
            self.server.touch()

    def _stream(self, params: dict) -> None:
        """The waiting microphone streamed (#4539): ``101``, then records both ways
        until the deck closes it; a 503 when every stream is taken."""
        threshold = float(params.get("threshold", ["0.5"])[0])
        word = params.get("word", ["jarvis"])[0]
        if not 0 < threshold < 1 or word not in MODELS:
            raise ValueError("a threshold between 0 and 1 and a known wake word")
        sure = float(params["sure"][0]) if "sure" in params else None
        if sure is not None and not 0 < sure < 1:
            raise ValueError("sure must be between 0 and 1")
        if self.headers.get("Upgrade", "").lower() != STREAM_UPGRADE:
            raise ValueError(f"expected Upgrade: {STREAM_UPGRADE}")
        try:
            stream = self.server.detector.stream(word, threshold, sure)
        except Busy as exc:
            self._json(503, {"error": str(exc)})
            return
        self.close_connection = True
        try:
            self.send_response(101)
            self.send_header("Upgrade", STREAM_UPGRADE)
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.flush()
            log.info("stream open: %s", word)
            serve_stream(stream, self.rfile, self.wfile, self.server.touch)
        except (OSError, ValueError) as exc:
            log.info("stream ended: %s", exc)
        except Exception:  # noqa: BLE001 - one stream must not end the service
            log.exception("stream failed")
        finally:
            stream.release()
            log.info("stream closed: %s", word)


class WakeServer(socketserver.ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(self, sock: socket.socket, detector: Detector) -> None:
        super().__init__("", Handler, bind_and_activate=False)
        self.socket = sock
        self.detector = detector
        self.last_used = time.monotonic()

    def server_bind(self) -> None:  # the socket arrives bound
        pass

    def touch(self) -> None:
        self.last_used = time.monotonic()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default="")
    parser.add_argument("--idle-exit", type=int, default=0, help="seconds; 0 never exits")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    server = WakeServer(listening_socket(args.socket), Detector())
    server.timeout = 5
    log.info("wake word service ready (%s)", MODEL)
    server.detector.warm()
    while True:
        server.handle_request()
        idle = time.monotonic() - server.last_used
        if args.idle_exit and idle >= args.idle_exit:
            log.info("idle for %ds; exiting", int(idle))
            return 0


if __name__ == "__main__":
    sys.exit(main())
