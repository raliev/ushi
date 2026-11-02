#!/bin/bash
# Build the Windows app (x64, Windows 10/11): app/dist/ushi-Setup-<version>-x64.exe
# Run in Git Bash (bash, curl, tar and cp come with Git for Windows); needs uv and Node 18+.
#
#   packaging/build_app_win.sh                  # GPU=auto: Whisper on NVIDIA GPUs if this PC has one
#   GPU=1 packaging/build_app_win.sh            # bundle cuBLAS + cuDNN for Whisper on NVIDIA GPUs (+1.8 GB)
#   GPU=0 packaging/build_app_win.sh            # CPU only (smallest)
#   MODELS=core packaging/build_app_win.sh      # bundle models (see fetch_models.py); default none:
#                                               # each model is downloaded on first use
#   REBUILD_PYTHON=1 packaging/build_app_win.sh # after changing packaging/requirements-windows*.txt
#
# The app is not code-signed: on first start Windows SmartScreen says "Windows protected
# your PC" -- More info → Run anyway.
set -euo pipefail
cd "$(dirname "$0")/.."
MODELS="${MODELS:-none}"
GPU="${GPU:-auto}"
if [[ "$GPU" == auto ]]; then
  GPU=0; command -v nvidia-smi >/dev/null 2>&1 && GPU=1
fi
PY_VERSION="${PY_VERSION:-3.12.13}"
PYDIR=build/python-win
PY="$PYDIR/python.exe"

# 1. Python runtime: relocatable python-build-standalone CPython + dependencies.
#    PyTorch from PyPI is the CPU build on Windows: it runs VAD, diarization, OCR and
#    CLIP. Whisper on the GPU goes through CTranslate2, which only needs cuBLAS + cuDNN.
if [[ "${REBUILD_PYTHON:-0}" == 1 || ! -x $PY ]]; then
  uv python install "cpython-$PY_VERSION-windows-x86_64-none"
  SRC="$(uv python find --managed-python "cpython-$PY_VERSION-windows-x86_64-none")"
  SRC="$(cd "$(dirname "$SRC")" && pwd -W)"
  rm -rf "$PYDIR"
  mkdir -p build
  cp -R "$SRC" "$PYDIR"
  rm -f "$PYDIR"/Lib/EXTERNALLY-MANAGED
  uv pip install --python "$PY" --link-mode copy -r packaging/requirements-windows.txt
  # Drop what the app never uses (headers, tests, docs). tkinter stays: the area / window pickers use it.
  SP="$PYDIR/Lib/site-packages"
  rm -rf "$PYDIR/include" "$PYDIR"/Lib/{test,idlelib,turtledemo,ensurepip} \
         "$SP"/torch/include "$SP"/torch/share/cmake
  # Static ffmpeg from imageio-ffmpeg, as ffmpeg.exe on the app's PATH (extra-bin, see app/main.js).
  mkdir -p "$PYDIR/extra-bin"
  cp "$(ls "$SP"/imageio_ffmpeg/binaries/ffmpeg-*.exe | head -1)" "$PYDIR/extra-bin/ffmpeg.exe"
  "$PY" -m compileall -q -j 0 "$SP" >/dev/null || true
fi
# yt-dlp changes every few weeks: bring a cached runtime up to the version required too
uv pip install --python "$PY" --link-mode copy "$(grep -E '^yt-dlp' packaging/requirements-windows.txt)"
if [[ "$GPU" == 1 ]]; then
  uv pip install --python "$PY" --link-mode copy -r packaging/requirements-windows-cuda.txt
else
  uv pip uninstall --python "$PY" nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12 2>/dev/null || true
fi
"$PY" -c "import faster_whisper, onnx_asr, sounddevice, soundcard, mss, comtypes, pycaw, easyocr, torch, speechbrain, transformers, fastapi, uvicorn, cv2, tkinter, silero_vad, yt_dlp; print('python ok:', torch.__version__)"
[[ -f "$PYDIR/Scripts/deno.exe" ]] || { echo "deno.exe is missing (yt-dlp[deno])" >&2; exit 1; }
rm -rf build/app-python
cp -R "$PYDIR" build/app-python

# 2. Models (default none: downloaded on first use; Silero VAD comes with the silero-vad package)
rm -rf build/models
if [[ "$MODELS" == none ]]; then
  mkdir -p build/models
else
  "$PY" packaging/fetch_models.py --set "$MODELS"
  cp -R "build/models-$MODELS" build/models
fi

# 3. App sources (the Windows helpers are Python scripts: nothing to compile)
STAGE=build/stage/voice-rec
rm -rf build/stage && mkdir -p "$STAGE"
cp ./*.py "$STAGE"/
cp -R web templates "$STAGE"/
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
"$PY" -m compileall -q "$STAGE" >/dev/null

# 4. Electron
cd app
npm install --no-audit --no-fund
SUFFIX=""
[[ "$GPU" == 1 ]] && SUFFIX="$SUFFIX-gpu"
[[ "$MODELS" != none ]] && SUFFIX="$SUFFIX-$MODELS-models"
rm -rf dist/win-unpacked
npx electron-builder --win nsis --x64 --publish never \
  "-c.nsis.artifactName=\${productName}-Setup-\${version}-\${arch}$SUFFIX.\${ext}"
ls -lh dist/*.exe
