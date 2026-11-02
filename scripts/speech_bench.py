#!/usr/bin/env python3
"""Compare speech-to-text models on the owner's own phrases, quiet and noisy apart (#3934).

With ``[speech] keep_audio_days`` set, the deck keeps every transcribed phrase's audio,
words and the speech service's noise reading (``snr_db``) in ``[speech]
keep_audio_dir`` (``kept.jsonl`` beside one WAV per phrase). The owner reads
``speech_bench_phrases.txt`` through voice mode or 🎤 dictation, one line per pause,
once somewhere noisy (a car, a café, the TV on) and, if they like, once somewhere
quiet. Each ``--reading START/END`` pairs the phrases kept in that window with the
list's lines in order, all of them. A count that does not match is refused: leave a
clip of room noise out with ``--skip ID``, and name lines never captured as
``START/END#3,7``. Every paired phrase is scored on:

- ``live``: the speech service as it runs now (Whisper large-v3 turbo on the card
  since #3517), posted to ``[speech] socket``;
- ``large-v3``: the full Whisper large-v3 on the card, loaded here and run the way
  the service runs Whisper (the same Silero cut, language, beam, word list and
  phantom check);
- ``live-cleaned``: the live service on audio DeepFilterNet cleaned first, on the
  CPU in its own virtualenv (``scripts/speech_bench_setup.sh``), so the service's
  own packages never change before it wins;
- ``orukeet``: Orukeet on the CPU, the service's fallback.

The report gives word errors per noise band (from each phrase's ``snr_db``: quiet at
20 dB and over, some noise from 10 dB, loud under 10 dB), tech terms heard, time per
phrase (any noise removal included), and whether each candidate beats ``live``:
fewer word errors in noise, at most two more words wrong in quiet, and a median
phrase under 1.5 s. Phrases kept in each ``--everyday START/END``, all of them, are
shown with each model's words, unscored, marked when Silero found no speech in them.
Run it in the speech venv, with the speech service up:

    speech-venv/bin/python scripts/speech_bench.py --label car \\
        --reading 2026-11-02T08:10/2026-11-02T08:25 > report.md

Nothing here costs money: every model runs on this host (owner, #3934).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHRASES = HERE / "speech_bench_phrases.txt"
CONFIG = Path("~/.config/agentdeck/config.toml").expanduser()
BENCH_VENV = Path("~/.local/share/agentdeck/speech-bench-venv").expanduser()
MODELS = ("live", "large-v3", "live-cleaned", "orukeet")
BASELINE = "live"
# Noise bands by the phrase's noise reading, loudest noise last.
BANDS = (("quiet", 20.0), ("some", 10.0), ("loud", float("-inf")))
NOISY = ("some", "loud")
# A candidate replaces ``live`` when it has fewer word errors in noise, at most this
# many more words wrong in quiet (the list is short, so a point is under a word), and
# a median phrase under this many seconds.
QUIET_SLACK_WORDS = 2
MAX_MEDIAN_S = 1.5
# Tech terms, as a pattern over the words with spaces removed, so "agent deck",
# "AgentDeck" and the Finnish "AgentDeckin" all count; the Finnish endings ride on.
TERMS = {
    "AgentDeck": r"agentdeck",
    "Haiku": r"haiku",
    "Codex": r"codex",
    "Opus": r"opus|opuks",
    "pull request": r"pullrequest",
    "deploy": r"deploy",
    "staging": r"staging",
    "review": r"review",
    "merge": r"merge",
    "issue": r"issue",
    "speech server": r"speechserver",
    "runtime": r"runtime",
    "chat": r"chat",
    "commit": r"commit",
    "push": r"push",
    "spec": r"spec(?!h)",
    "queue": r"queue",
    "live check": r"livecheck",
    "dashboard": r"dashboard",
    "voice mode": r"voicemode",
    "token": r"token",
    "Sentry": r"sentry",
    "wake word": r"wakeword",
    "webhook": r"webhook",
    "master": r"master",
    "branch": r"branch",
    "feature": r"feature",
    "restart": r"restart",
    "Work": r"work",
}
# One-word terms a model may write as two.
SPLIT_TERMS = ["agent deck", "web hook", "dash board", "code x"]
# DeepFilterNet's own example, run in its virtualenv: WAV paths in, cleaned WAVs out,
# and each phrase's cleaning time on a `took` line of stdout, the model's load left out
# as for every model compared.
CLEAN = """
import sys, time
from df.enhance import enhance, init_df, load_audio, save_audio
model, state, _ = init_df()
for source, target in zip(sys.argv[1::2], sys.argv[2::2]):
    started = time.monotonic()
    audio, _ = load_audio(source, sr=state.sr())
    save_audio(target, enhance(model, state, audio), state.sr())
    print("took", time.monotonic() - started, flush=True)
"""

# Voice mode's own tests of which language words are in (``voice_mode.js``), copied
# so the next phrase's hint is chosen as the page chooses it; a test keeps them equal.
FI_WORDS = re.compile(
    r"^(ja|ei|se|ne|että|kun|mitä|mikä|miten|missä|onko|olen|olet|ole|tämä|tuo|nyt|myös|mutta|vain|jo|vielä|kyllä|joo|en|et|hän|minä|sinä|voi|voit|sitten|jos|niin|tai|siitä|sen|kaikki|tee|katso|kerro|valmis|selvä)$"
)
EN_WORDS = re.compile(
    r"^(the|a|an|and|is|are|was|were|be|been|what|which|who|how|why|where|when|has|have|had|do|does|did|of|to|in|it|its|this|that|these|those|with|for|you|your|i|we|they|she|not|yes|can|will|would|should|could|there|here|from|about|please|done|ready|okay|ok|all|any|some|my|our|one|two|now|no|so|up|out|just|new|see|need)$"
)
FI_SHAPE = re.compile(
    r"[åäö]|aa|ii|uu|yy|[bdfjklmnrv]h|h[bdfgjklmnprstv]|(ssa|sta|lla|lta|lle|ksi|nen|tiin|aan|iin|sti|ttu|tty|tu|ty|ut|yt|vat|mme|tte)$"
)
EN_SHAPE = re.compile(
    r"th|sh|ch|wh|ph|gh|ck|ee|oo|ea|ou|[cqwxzfb]|(ing|ed|ly|tion|ness|ment|ful|ous)$|[bcdfghjkmpqvwxz]$"
)


def words_language(text: str) -> str:
    """``fi`` or ``en`` for what ``text`` is written in, '' when its words do not tell,
    as ``wordsLanguage`` in ``voice_mode.js``."""
    fi = en = 0
    for word in re.split(r"[^a-zåäö']+", text.lower()):
        if not word:
            continue
        if FI_WORDS.search(word):
            fi += 1
        elif EN_WORDS.search(word):
            en += 1
        else:
            finnish, english = bool(FI_SHAPE.search(word)), bool(EN_SHAPE.search(word))
            fi += finnish and not english
            en += english and not finnish
    if fi == en:
        return ""
    return "fi" if fi > en else "en"


def next_hint(text: str, language: str) -> str:
    """The hint voice mode sends with the next phrase once it has heard ``text``,
    which the model called ``language``: the words win (#3934 review)."""
    return words_language(text) or ("fi" if language == "fi" else "en")


def words(text: str) -> list[str]:
    """Lower case, no punctuation, hyphens as spaces, each term as one word."""
    text = text.lower().replace("-", " ")
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    for name in [*TERMS, *SPLIT_TERMS]:
        if " " in name:
            text = re.sub(r"\b" + r"\s*".join(name.split()), name.replace(" ", ""), text)
    return text.split()


def errors(said: list[str], heard: list[str]) -> int:
    """Word edits (substitutions, deletions, insertions) from ``said`` to ``heard``."""
    row = list(range(len(heard) + 1))
    for i, word in enumerate(said, 1):
        previous, row[0] = row[0], i
        for j, other in enumerate(heard, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (word != other))
    return row[-1]


def terms_in(text: str) -> set[str]:
    joined = "".join(words(text))
    return {name for name, pattern in TERMS.items() if re.search(pattern, joined)}


def band(snr_db: object) -> str:
    """The noise band of a phrase's reading; ``unknown`` when it has none."""
    if not isinstance(snr_db, (int, float)) or isinstance(snr_db, bool):
        return "unknown"
    return next(name for name, floor in BANDS if snr_db >= floor)


def load_phrases(path: Path = PHRASES) -> list[tuple[str, str]]:
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            group, _, text = line.partition("\t")
            found.append((group.strip(), text.strip()))
    return found


def reading(value: str) -> tuple[float, float]:
    """``START/END`` as two ISO times; END may be left out for now. A ``#LINES``
    after it is the reading's missing lines (``missing_lines``)."""
    start, _, end = value.partition("#")[0].partition("/")
    return when(start), when(end) if end else time.time()


def missing_lines(value: str) -> set[int]:
    """The list's lines, counted from 1, a reading has no phrase for: ``START/END#3,7``
    when lines 3 and 7 were never captured."""
    _, _, lines = value.partition("#")
    return {int(number) for number in lines.split(",") if number.strip()}


def when(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def kept_between(folder: Path, since: float, until: float) -> list[dict]:
    """The kept phrases in the window that still have their audio, oldest first."""
    lines = []
    for raw in (folder / "kept.jsonl").read_text(encoding="utf-8").splitlines():
        line = json.loads(raw)
        if since <= line["at"] <= until and (folder / f"{line['id']}.wav").exists():
            lines.append(line)
    return sorted(lines, key=lambda line: line["at"])


def everyday_rows(lines: list[dict], folder: Path, has_speech) -> list[dict]:
    """Every everyday phrase, each marked with whether ``has_speech`` (Silero) found
    speech in it: one it missed in noise is exactly a failure worth seeing, so it is
    shown, never dropped (#3934 review)."""
    return [
        {"id": line["id"], "speech": has_speech((folder / f"{line['id']}.wav").read_bytes())}
        for line in lines
    ]


def pair(phrases: list[tuple[str, str]], kept: list[dict], missing: set[int]) -> list[dict]:
    """One reading's kept phrases against the list's lines in order, its ``missing``
    lines left out. Every phrase is paired, one no speech was found in too: in noise
    that is often a real line the models missed (#3934 review). A count that does not
    match means a line was never captured or a clip of room noise came in between,
    and every later line would be scored against the wrong words, so it is refused
    until ``--skip`` or ``#LINES`` aligns it."""
    lines = [phrase for number, phrase in enumerate(phrases, 1) if number not in missing]
    if len(kept) != len(lines):
        raise ValueError(
            f"{len(kept)} phrases kept for {len(lines)} lines: leave room noise out with"
            " --skip ID, or name the lines never captured as START/END#LINES"
        )
    return [
        {"group": group, "said": said, "id": line["id"], "band": band(line.get("snr_db"))}
        for (group, said), line in zip(lines, kept, strict=True)
    ]


def score(rows: list[dict], model: str) -> dict:
    """One model's numbers: word errors and words said per band, terms, times."""
    result = {}
    for name in [*(b for b, _ in BANDS), "unknown"]:
        mine = [row for row in rows if row["band"] == name]
        said = sum(len(words(row["said"])) for row in mine)
        wrong = sum(errors(words(row["said"]), words(row[model])) for row in mine)
        result[name] = {"said": said, "wrong": wrong, "phrases": len(mine)}
    wanted = [(term, row) for row in rows for term in terms_in(row["said"])]
    result["terms"] = sum(term in terms_in(row[model]) for term, row in wanted)
    result["terms_of"] = len(wanted)
    times = [row[f"{model}_s"] for row in rows]
    result["median_s"] = statistics.median(times) if times else 0.0
    return result


def rate(part: dict) -> str:
    return f"{100 * part['wrong'] / part['said']:.1f}" if part["said"] else "–"


def noisy(scores: dict) -> dict:
    return {key: sum(scores[name][key] for name in NOISY) for key in ("said", "wrong", "phrases")}


def verdict(scores: dict[str, dict], model: str) -> str:
    if BASELINE not in scores:
        return f"no {BASELINE} run to compare with"
    mine, live = scores[model], scores[BASELINE]
    if not noisy(live)["phrases"]:
        return "no noisy phrases to judge on"
    checks = [
        (
            noisy(mine)["wrong"] < noisy(live)["wrong"],
            f"noisy words wrong {noisy(mine)['wrong']} < {noisy(live)['wrong']}",
        ),
        (
            mine["quiet"]["wrong"] <= live["quiet"]["wrong"] + QUIET_SLACK_WORDS,
            f"quiet words wrong {mine['quiet']['wrong']} ≤ {live['quiet']['wrong']}"
            f" + {QUIET_SLACK_WORDS}",
        ),
        (mine["median_s"] < MAX_MEDIAN_S, f"median {mine['median_s']:.2f} s < {MAX_MEDIAN_S} s"),
    ]
    passed = all(ok for ok, _ in checks)
    detail = "; ".join(("✓ " if ok else "✗ ") + text for ok, text in checks)
    return f"**{'beats' if passed else 'does not beat'}** {BASELINE}: {detail}"


def report(rows: list[dict], extra: list[dict], models: list[str], label: str) -> str:
    scores = {model: score(rows, model) for model in models}
    counts = {name: scores[models[0]][name]["phrases"] for name in (*dict(BANDS), "unknown")}
    out = [
        f"### Speech-to-text in noise: {len(rows)} read phrases, {label} (#3934)",
        "",
        "Word errors in %, lower is better, by each phrase's noise reading: quiet "
        f"20 dB and over ({counts['quiet']} phrases), some noise 10–20 dB "
        f"({counts['some']}), loud under 10 dB ({counts['loud']}); "
        f"{counts['unknown']} had no reading.",
        "",
        "Times are seconds a phrase, the noise removal before `live-cleaned` included.",
        "",
        "| Model | Quiet | Some noise | Loud | Terms heard | Median s |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for model in models:
        s = scores[model]
        out.append(
            f"| {model} | {rate(s['quiet'])} | {rate(s['some'])} | {rate(s['loud'])} "
            f"| {s['terms']}/{s['terms_of']} | {s['median_s']:.2f} |"
        )
    out.append("")
    out += [f"- {model}: {verdict(scores, model)}" for model in models if model != BASELINE]
    out += ["", "<details><summary>Every phrase</summary>", ""]
    out.append("| # | Noise | Said | " + " | ".join(models) + " |")
    out.append("|---|---|---|" + "---|" * len(models))
    for number, row in enumerate(rows, 1):
        cells = " | ".join(heard(row, model) for model in models)
        out.append(f"| {number} | {row['band']} | {row['said']} | {cells} |")
    out += ["", "</details>"]
    if extra:
        out += ["", f"<details><summary>{len(extra)} other phrases, unscored</summary>", ""]
        out.append("| Kept | " + " | ".join(models) + " |")
        out.append("|---|" + "---|" * len(models))
        for row in extra:
            cells = " | ".join(heard(row, model) for model in models)
            name = row["id"] if row.get("speech", True) else f"{row['id']} (no speech found)"
            out.append(f"| {name} | {cells} |")
        out += ["", "</details>"]
    return "\n".join(out)


def heard(row: dict, model: str) -> str:
    """One model's words for a phrase in a table cell, with the seconds they took."""
    return f"{row[model].replace('|', '/')} · {row[f'{model}_s']:.2f} s"


def speech_server():
    spec = importlib.util.spec_from_file_location("speech_server", HERE / "speech_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_runner(socket_path: str, only: str, post=None):
    """The live speech service, asked as voice mode asks it: pinned to ``only`` when
    the deck pins a language, else with the language it heard last as the hint for a
    phrase too short to tell (``voice_mode.js``), so it is compared under the same
    rule as ``large-v3`` (#3934 review). ``post`` stands in for the socket in tests."""
    import http.client
    import socket

    class Unix(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(socket_path)

    def over_socket(path: str, audio: bytes) -> dict:
        connection = Unix("speech", timeout=120)
        connection.request("POST", path, audio, {"Content-Type": "audio/wav"})
        reply = connection.getresponse()
        body = reply.read()
        if reply.status != 200:
            raise RuntimeError(f"speech service answered {reply.status}: {body[:200]!r}")
        return json.loads(body)

    post = post or over_socket
    heard = {"language": ""}

    def run(audio: bytes) -> str:
        if only:
            path = f"/transcribe?only={only}"
        elif heard["language"]:
            path = f"/transcribe?language={heard['language']}"
        else:
            path = "/transcribe"
        reply = post(path, audio)
        if reply.get("text", "").strip():
            heard["language"] = next_hint(reply["text"].strip(), reply.get("language", ""))
        return reply["text"]

    run.reset = lambda: heard.update(language="")
    return run


def cleaned(folder: Path, ids: list[str], into: Path) -> tuple[dict[str, bytes], dict[str, float]]:
    """Each phrase's audio after DeepFilterNet, in one run of its virtualenv, and the
    seconds cleaning it took, which count in that phrase's time."""
    python = BENCH_VENV / "bin/python"
    if not python.exists():
        raise SystemExit(f"no {python}: run scripts/speech_bench_setup.sh first")
    pairs = [str(p) for i in ids for p in (folder / f"{i}.wav", into / f"{i}.wav")]
    # On the CPU, so the card keeps its memory for the Whisper models being compared.
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": ""}
    done = subprocess.run(
        [str(python), "-I", "-c", CLEAN, *pairs],
        check=True,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    # DeepFilterNet logs to stdout too, so only the lines marked as times count.
    took = [float(line.split()[1]) for line in done.stdout.splitlines() if line.startswith("took ")]
    return {i: (into / f"{i}.wav").read_bytes() for i in ids}, dict(zip(ids, took, strict=True))


def transcribe_all(rows: list[dict], model: str, run, audio: dict, before_s: dict) -> None:
    """Each row's words from ``run`` and its time: the model's, plus any work done to
    the phrase before it (``live-cleaned``'s noise removal), as voice mode would wait
    for both. Each reading is its own voice mode session, so a ``run`` that keeps the
    language heard starts over at each one's first phrase (#3934 review)."""
    session = object()
    for row in rows:
        if row.get("session") != session and hasattr(run, "reset"):
            run.reset()
        session = row.get("session")
        started = time.monotonic()
        row[model] = run(audio[row["id"]])
        row[f"{model}_s"] = time.monotonic() - started + before_s.get(row["id"], 0.0)


def large_runner(engine: dict, only: str):
    # The speech service's allocator setting, which costs time on the card (#3424).
    os.environ.setdefault("CT2_CUDA_CACHING_ALLOCATOR_CONFIG", "4,3,12,0")
    server = speech_server()
    server.preload_cuda_libraries()
    from faster_whisper import WhisperModel

    model = WhisperModel("large-v3", device="cuda", compute_type=server.compute_type("cuda"))
    beam = engine.get("whisper_beam", server.WHISPER_BEAM)
    prompt = engine.get("whisper_prompt", server.WHISPER_PROMPT)
    heard = {"language": ""}

    def run(audio: bytes) -> str:
        speech = server.speech_only(server.read_wav(audio))
        if speech is None:
            return ""
        language = only
        if not language:
            _, _, probabilities = model.detect_language(speech)
            language, _ = server.choose_language(dict(probabilities), heard["language"])
        segments, _ = model.transcribe(
            speech,
            language=language,
            beam_size=beam,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        text = " ".join(s.text.strip() for s in segments if not server.is_phantom(s)).strip()
        if text:  # voice mode's hint for the next phrase
            heard["language"] = next_hint(text, language)
        return text

    run.reset = lambda: heard.update(language="")
    return run


def orukeet_runner(engine: dict):
    server = speech_server()
    args = argparse.Namespace(parakeet_model=engine.get("parakeet_model", server.PARAKEET_MODEL))
    parakeet = server.Speech(args).load_parakeet()

    def run(audio: bytes) -> str:
        speech = server.speech_only(server.read_wav(audio))
        text = "" if speech is None else parakeet(speech)
        return "" if server.is_phantom_text(text) else text

    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reading",
        action="append",
        default=[],
        help="START/END in ISO time of one reading of the list; give it once per reading",
    )
    parser.add_argument(
        "--everyday",
        action="append",
        default=[],
        help="START/END of everyday phrases to show unscored beside the readings",
    )
    parser.add_argument("--label", default="", help="where it was read, for the report title")
    parser.add_argument("--skip", default="", help="kept ids to leave out, comma-separated")
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--dir", default="", help="kept phrases; default [speech] keep_audio_dir")
    args = parser.parse_args(argv)
    speech = tomllib.loads(CONFIG.read_text()).get("speech", {}) if CONFIG.exists() else {}
    folder = Path(args.dir or speech.get("keep_audio_dir", "~/.local/share/agentdeck/speech/kept"))
    folder = folder.expanduser()
    only = speech.get("language") if speech.get("language") in ("en", "fi") else ""
    skip = set(filter(None, args.skip.split(",")))
    server = speech_server()
    engine = server.configured_engine(str(CONFIG))
    phrases = load_phrases()
    rows, extra = [], []
    for window in args.reading:
        kept = [line for line in kept_between(folder, *reading(window)) if line["id"] not in skip]
        missing = missing_lines(window)
        # The pairing first, with the live words, so a skipped or repeated line shows.
        listed = [p for number, p in enumerate(phrases, 1) if number not in missing]
        for (group, said), line in zip(listed, kept, strict=False):
            print(f"{line['id']} {group}: {said!r} <- {line['text']!r}", file=sys.stderr)
        try:
            rows += [
                {**row, "session": ("reading", window)} for row in pair(phrases, kept, missing)
            ]
        except ValueError as exc:
            print(f"{window}: {exc}", file=sys.stderr)
            return 1
    # Everyday phrases have no words to score against: each model's are shown.
    for window in args.everyday:
        kept = [line for line in kept_between(folder, *reading(window)) if line["id"] not in skip]
        found = everyday_rows(
            kept, folder, lambda audio: server.speech_only(server.read_wav(audio)) is not None
        )
        extra += [{**row, "session": ("everyday", window)} for row in found]
    if not rows and not extra:
        print(f"no kept phrases in {folder} for those windows", file=sys.stderr)
        return 1
    models = [m for m in args.models.split(",") if m in MODELS]
    every = rows + extra
    for model in models:
        audio = {row["id"]: (folder / f"{row['id']}.wav").read_bytes() for row in every}
        before_s = {}
        if model == "live":
            run = live_runner(str(Path(speech["socket"]).expanduser()), only)
        elif model == "live-cleaned":
            run = live_runner(str(Path(speech["socket"]).expanduser()), only)
            with tempfile.TemporaryDirectory(dir="/var/tmp") as into:
                audio, before_s = cleaned(folder, list(audio), Path(into))
        elif model == "large-v3":
            run = large_runner(engine, only)
        else:
            run = orukeet_runner(engine)
        run(audio[every[0]["id"]])  # loads the model, so no phrase's time includes it
        transcribe_all(every, model, run, audio, before_s)
        print(f"{model} done", file=sys.stderr)
    print(report(rows, extra, models, args.label or ", ".join(args.reading)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
