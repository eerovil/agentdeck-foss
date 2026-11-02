#!/usr/bin/env bash
# The noise remover scripts/speech_bench.py tries before the live Whisper (#3934):
# DeepFilterNet in a virtualenv of its own, so the speech service's pinned packages
# (CTranslate2 3.24, torch 2.8) never change before it wins. DeepFilterNet 0.5.6 still
# imports torchaudio's old backend module, so this pins torch 2.0 with Python 3.11;
# the bench runs it on the CPU. Safe to run again.
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agentdeck"
VENV="$DATA/speech-bench-venv"

[[ -x "$VENV/bin/python" ]] || uv venv -q -p 3.11 "$VENV"
VIRTUAL_ENV="$VENV" uv pip install -q --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple \
  'torch==2.0.1' 'torchaudio==2.0.2' 'deepfilternet==0.5.6' 'numpy<2'
CUDA_VISIBLE_DEVICES= "$VENV/bin/python" -I -c 'from df.enhance import init_df; init_df()'
echo "DeepFilterNet ready in $VENV"
