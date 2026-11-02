"""Native helpers per platform: screen capture + hotkeys, and app/system audio capture.

macOS: the compiled Swift binaries next to this file (screenshot_helper, audio_helper).
Windows: Python scripts with the same command line and stdout protocol
(screenshot_helper_win.py, audio_helper_win.py), run with the current interpreter.

Callers get a command prefix (a list) or None when the helper isn't available.
"""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def _cmd(binary: str, script: str) -> list[str] | None:
    if IS_WINDOWS:
        path = ROOT / script
        return [sys.executable, "-u", str(path)] if path.exists() else None
    path = ROOT / binary
    return [str(path)] if path.exists() else None


def screenshot_cmd() -> list[str] | None:
    """screenshot_helper: hotkeys, screenshots, auto frames, area/window pickers."""
    return _cmd("screenshot_helper", "screenshot_helper_win.py")


def audio_cmd() -> list[str] | None:
    """audio_helper: --list (apps playing audio) and --capture <id> (16 kHz mono float32 on stdout)."""
    return _cmd("audio_helper", "audio_helper_win.py")


def screenshot_build_hint() -> str:
    return "pip install mss pillow" if IS_WINDOWS else "run ./compile.sh"


def audio_build_hint() -> str:
    return "pip install soundcard" if IS_WINDOWS else "run ./build_audio_helper.sh"


def ffmpeg_exe() -> str | None:
    """ffmpeg on PATH, or the static binary from imageio-ffmpeg when that is installed."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_app_names: dict[str, str] = {}


def app_display_name(exe: str) -> str:
    """Windows: an app's display name from its exe's version resource ("Google Chrome",
    "Slack"), else the file name without .exe."""
    if exe in _app_names:
        return _app_names[exe]
    name = ""
    if IS_WINDOWS and exe:
        import ctypes
        from ctypes import wintypes as wt
        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
        ver.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wt.UINT)]
        try:
            size = ver.GetFileVersionInfoSizeW(exe, None)
            data = ctypes.create_string_buffer(size) if size else None
            if data and ver.GetFileVersionInfoW(exe, 0, size, data):
                ptr, n = ctypes.c_void_p(), wt.UINT()
                if ver.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) \
                        and n.value >= 4:
                    lang, cp = ctypes.cast(ptr, ctypes.POINTER(wt.WORD * 2)).contents
                    key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
                    if ver.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(n)) and n.value:
                        name = ctypes.wstring_at(ptr, n.value).rstrip("\0").strip()
        except OSError:
            pass
    _app_names[exe] = name or Path(exe).stem
    return _app_names[exe]


def add_cuda_dll_dirs():
    """Windows: the cuBLAS / cuDNN DLLs from the nvidia-* pip packages live in
    site-packages/nvidia/*/bin, which the DLL loader doesn't search. CTranslate2 (faster-whisper
    on cuda) looks them up by name, so register those folders (and PATH, for LoadLibrary
    calls made without the search flags)."""
    if not IS_WINDOWS:
        return
    import site
    roots = [Path(p) / "nvidia" for p in site.getsitepackages() + [site.getusersitepackages()]]
    for root in roots:
        if not root.is_dir():
            continue
        for bin_dir in root.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                continue
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def utf8_stdio():
    """Windows pipes default to the ANSI code page (cp1251 etc.): Cyrillic and symbols like
    '●' would raise or turn into mojibake. Everything here reads our output as UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def child_env(**extra) -> dict:
    """Environment for Python children whose output we parse: unbuffered, UTF-8."""
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **extra}


def reveal(path: Path):
    """Show a file in the system file manager: selected in Finder / Explorer, or its folder elsewhere."""
    import subprocess
    path = Path(path).resolve()
    if IS_MAC:
        subprocess.Popen(["open", "-R", str(path)])
    elif IS_WINDOWS:
        # explorer.exe parses its command line itself: it wants /select,"<path>" with only the
        # path quoted. A list argument gets quoted whole ("/select,C:\a b\c") when the path
        # has spaces, and explorer then ignores it and opens Documents. Its exit code is meaningless.
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])
