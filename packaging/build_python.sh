#!/bin/bash
# Build the self-contained Python runtime for the macOS app: build/python
# (a relocatable python-build-standalone CPython with all recorder/server deps).
#   ARCH=x64 packaging/build_python.sh  → build/python-x64 for Intel Macs (runs under Rosetta here)
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION="${PY_VERSION:-3.12.11}"
ARCH="${ARCH:-arm64}"
if [[ "$ARCH" == x64 ]]; then
  OUT=build/python-x64
  PY_REQ="cpython-$PY_VERSION-macos-x86_64-none"
  # Prebuilt wheels only: some newest releases (e.g. cryptography) have no x86_64 macOS wheel
  # and would be compiled from source; this picks the last release that has one.
  OVERRIDES=(--override packaging/requirements-x64-overrides.txt --only-binary :all:)
  RUN=(arch -x86_64)
else
  OUT=build/python
  PY_REQ="$PY_VERSION"
  OVERRIDES=()
  RUN=()
fi

uv python install "$PY_REQ"
SRC="$(uv python find --managed-python "$PY_REQ")"
SRC="$(cd "$(dirname "$SRC")/.." && pwd -P)"

rm -rf "$OUT"
mkdir -p build
cp -cR "$SRC" "$OUT" 2>/dev/null || cp -R "$SRC" "$OUT"
rm -f "$OUT"/lib/python3.*/EXTERNALLY-MANAGED

uv pip install --python "$OUT/bin/python3" --link-mode copy -r packaging/requirements-app.txt ${OVERRIDES[@]+"${OVERRIDES[@]}"}

# Drop what the app never uses (headers, tests, Tk, docs) to save ~0.5 GB.
SP="$(${RUN[@]+"${RUN[@]}"} "$OUT/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
rm -rf "$OUT/include" "$OUT/share" \
       "$OUT"/lib/python3.*/{test,idlelib,tkinter,turtledemo,ensurepip} \
       "$OUT"/lib/libtcl* "$OUT"/lib/libtk* "$OUT"/lib/tcl* "$OUT"/lib/tk* \
       "$SP"/torch/include "$SP"/torch/share/cmake

# Intel: torch, functorch and ctranslate2 each bundle libiomp5 (Intel OpenMP); two copies in
# one process abort it (OMP Error #15). Point the others at torch's copy.
if [[ "$ARCH" == x64 ]]; then
  for d in "$SP"/ctranslate2/.dylibs "$SP"/functorch/.dylibs; do
    if [[ -f "$d/libiomp5.dylib" && ! -L "$d/libiomp5.dylib" ]]; then
      rm "$d/libiomp5.dylib"
      ln -s "$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$SP/torch/lib/libiomp5.dylib" "$d")" "$d/libiomp5.dylib"
    fi
  done
fi

# Static ffmpeg from imageio-ffmpeg, under a name main.py finds on PATH.
FF="$(ls "$SP"/imageio_ffmpeg/binaries/ffmpeg-* | head -1)"
mkdir -p "$OUT/extra-bin"
cp "$FF" "$OUT/extra-bin/ffmpeg"
chmod +x "$OUT/extra-bin/ffmpeg"

${RUN[@]+"${RUN[@]}"} "$OUT/bin/python3" -m compileall -q -j 0 "$SP" >/dev/null || true
${RUN[@]+"${RUN[@]}"} "$OUT/bin/python3" -c "import faster_whisper, onnx_asr, sounddevice, easyocr, torch, speechbrain, transformers, fastapi, uvicorn, cv2, yt_dlp; print('python ok:', torch.__version__)"
du -sh "$OUT"
