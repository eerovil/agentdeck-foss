#!/usr/bin/env bash
# Build voice mode's speech service on this host (#3409): its own virtualenv, the
# Piper voices and the Whisper model, then the systemd socket that starts it. The
# wake word detector (#3476, openWakeWord's "hey jarvis" on the CPU) shares the
# virtualenv and gets a socket of its own.
#
# The versions are pinned to what a GTX 970 (Maxwell, compute 5.2) still runs.
# CTranslate2 4.x wheels ship no kernels for it ("no kernel image is available"),
# so this takes CTranslate2 3.24 with the CUDA 11 / cuDNN 8 libraries, and
# faster-whisper without its ">=4" pin. 3.24's library asks for an executable
# stack, which current glibc refuses to load; patchelf clears the flag.
# The GTX 1660 Super that replaced it (#3517) runs the same pins; Whisper runs there
# as int8_float32, its fastest. The Whisper model is `[speech] whisper_model`, else
# AGENTDECK_SPEECH_MODEL; Orukeet is downloaded either way, as the fallback.
#
# CPU-only torch, transformers and librosa (Parakeet's feature extractor needs it)
# are for NVIDIA Parakeet TDT and Orukeet (#3499); they add no CUDA library
# and change none of the packages above. With the deck config's
# `[speech] engine = "parakeet"` (#3509) the service runs Orukeet on the CPU from
# `[speech] parakeet_model`, which this downloads (2.4 GB); Parakeet is the default,
# and with `engine = "whisper"` it hears what Whisper cannot (#3517).
#
# The Piper voices are the deck config's `[speech] voice_en` and `voice_fi` (#3455),
# or speech_server.py's defaults when unset; run this again after changing them.
#
# `--no-units` stops before the systemd units, for trying the service by hand
# (`speech_server.py --socket PATH`). Safe to run again. Afterwards set
# `[speech] socket` in the deck config (the path is printed at the end) and
# restart agentdeck.service.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agentdeck"
VENV="$DATA/speech-venv"
VOICES="$DATA/speech/voices"
PYTHON="${PYTHON:-python3.12}"
MODEL="${AGENTDECK_SPEECH_MODEL:-large-v3-turbo}"

[[ -x "$VENV/bin/python" ]] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade \
  'ctranslate2==3.24.0' 'nvidia-cublas-cu11' 'nvidia-cuda-runtime-cu11' \
  'nvidia-cudnn-cu11==8.9.*' 'piper-tts==1.8.0' patchelf numpy tokenizers \
  huggingface_hub av onnxruntime tqdm 'openwakeword==0.4.0'
"$VENV/bin/pip" install -q --no-deps 'faster-whisper==1.2.1'
"$VENV/bin/pip" install -q --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple 'torch==2.8.0' 'transformers==5.19.0' \
  'librosa==0.11.0'
for lib in "$VENV"/lib/python3*/site-packages/ctranslate2.libs/*.so*; do
  "$VENV/bin/patchelf" --clear-execstack "$lib"
done

CONFIG="${AGENTDECK_CONFIG:-$HOME/.config/agentdeck/config.toml}"
VOICE_NAMES="$("$VENV/bin/python" - "$REPO/scripts" "$CONFIG" <<'PY'
import sys, tomllib
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from speech_server import DEFAULT_VOICES, VOICE_NAME
path = Path(sys.argv[2]).expanduser()
speech = tomllib.loads(path.read_text()).get("speech", {}) if path.exists() else {}
names = [str(speech.get(f"voice_{lang}") or DEFAULT_VOICES[lang]) for lang in DEFAULT_VOICES]
if not all(VOICE_NAME.fullmatch(name) for name in names):
    sys.exit(f"not a Piper voice name: {names}")
print(*names)
PY
)"
mkdir -p "$VOICES"
# shellcheck disable=SC2086 # names are [A-Za-z0-9_-]+, one word each
(cd "$VOICES" && "$VENV/bin/python" -m piper.download_voices $VOICE_NAMES)
"$VENV/bin/python" - "$REPO/scripts" "$CONFIG" "$MODEL" <<'PY'
import sys
from pathlib import Path
from faster_whisper.utils import download_model
from huggingface_hub import hf_hub_download
sys.path.insert(0, sys.argv[1])
from speech_server import PARAKEET_MODEL, configured_engine
engine = configured_engine(sys.argv[2])
download_model(engine.get("whisper_model", sys.argv[3]))
# Orukeet is the Parakeet engine's model and the Whisper engine's fallback (#3517).
folder = Path(engine.get("parakeet_model", PARAKEET_MODEL))
for name in (
    "config.json",
    "generation_config.json",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "model.safetensors",
):
    hf_hub_download("oruk/orukeet", name, local_dir=folder.expanduser())
PY

[[ "${1:-}" == --no-units ]] && exit 0

UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNITS"
for unit in agentdeck-speech.socket agentdeck-speech.service \
  agentdeck-wake.socket agentdeck-wake.service; do
  ln -sfn "$REPO/systemd/$unit" "$UNITS/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now agentdeck-speech.socket agentdeck-wake.socket
# The speech service keeps its model loaded from login on (#3766), and the wake word
# detector stays up the same way (#3793).
systemctl --user enable --now agentdeck-speech.service agentdeck-wake.service

echo "Speech service ready. In the deck config add:"
echo "  [speech]"
echo "  socket = \"${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/agentdeck-speech.sock\""
echo "  wake_socket = \"${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/agentdeck-wake.sock\""
