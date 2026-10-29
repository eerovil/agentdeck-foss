"""Voice mode's wake word detector on the AgentDeck host (#3476).

While voice mode waits for its wake word, the page posts every phrase it cuts here
first, and Whisper runs only on a phrase this hears "Hey Jarvis" in. It is
openWakeWord's ready-made ``hey_jarvis`` model on the CPU: a 2 s phrase takes about
0.1 s, and the graphics card stays idle. The wake word is "Hey Jarvis" by the
owner's choice; a custom "Hey deck" would have needed a model trained for it.

It runs in the speech service's virtualenv (``scripts/speech_setup.sh`` adds
``openwakeword``), but in a process of its own, so Whisper's service can still exit
when idle and give back its graphics memory while voice mode waits.

    POST /wake    body: 16-bit mono WAV   -> {"woken", "score", "speech_after"}
                  ?threshold=0.5  the score that wakes (``[speech] wake_threshold``)
    GET  /status                          -> {"model"}

``speech_after`` says whether speech follows the wake word in the same phrase
("Hey Jarvis, run the tests"), so the page knows to have that phrase transcribed.

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


def model_path() -> str:
    """The ``hey_jarvis`` model openwakeword 0.4.0 ships in its wheel."""
    from pathlib import Path

    import openwakeword

    return str(Path(openwakeword.__file__).parent / "resources" / "models" / f"{MODEL}.onnx")


class Detector:
    def __init__(self) -> None:
        from openwakeword.model import Model

        self.model = Model(wakeword_model_paths=[model_path()])
        self.name = next(iter(self.model.models))
        # One phrase at a time: the model keeps its audio buffer between frames.
        self.lock = threading.Lock()

    def scores(self, samples) -> list[float]:
        import numpy as np

        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        pcm = np.concatenate([pcm, np.zeros(FRAME * TAIL_FRAMES, np.int16)])
        with self.lock:
            self.model.reset()
            return [
                float(self.model.predict(pcm[start : start + FRAME])[self.name])
                for start in range(0, len(pcm) - FRAME + 1, FRAME)
            ]

    def wake(self, data: bytes, threshold: float) -> dict:
        started = time.monotonic()
        samples = read_wav(data)
        scores = self.scores(samples) or [0.0]
        peak = max(range(len(scores)), key=scores.__getitem__)
        woken = scores[peak] >= threshold
        after = woken and speech_after(samples[(peak + 1) * FRAME :])
        log.info(
            "%.1fs phrase: score %.3f%s in %.0f ms",
            len(samples) / SAMPLE_RATE,
            scores[peak],
            " (woken, speech after)" if after else " (woken)" if woken else "",
            (time.monotonic() - started) * 1000,
        )
        return {"woken": woken, "score": round(scores[peak], 4), "speech_after": after}

    def warm(self) -> None:
        """Runs both models once, so the first phrase never waits for them to load:
        Silero's import alone took 12 to 15 s on the first wake of a new process."""
        import numpy as np

        started = time.monotonic()
        silence = np.zeros(SAMPLE_RATE, np.float32)
        self.scores(silence)
        speech_after(silence)
        log.info("models warm in %.1f s", time.monotonic() - started)


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
            self._json(200, {"model": MODEL})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self.server.touch()
        try:
            path, _, query = self.path.partition("?")
            if path != "/wake":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("body too large")
            threshold = float(parse_qs(query).get("threshold", ["0.5"])[0])
            if not 0 < threshold < 1:
                raise ValueError("threshold must be between 0 and 1")
            self._json(200, self.server.detector.wake(self.rfile.read(length), threshold))
        except (ValueError, wave.Error, EOFError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the service
            log.exception("wake request failed")
            self._json(500, {"error": str(exc)})
        finally:
            self.server.touch()


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
