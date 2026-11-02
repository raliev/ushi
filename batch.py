#!/usr/bin/env python3
"""Batch: a queue of videos (YouTube, or HLS .m3u8 streams), each  download (yt-dlp) → transcribe → Beautify.

Every task gets its own project (projects/<project>/), the video goes to its audio/ folder.
Stages:
  download    yt-dlp, up to --parallel (3) at once
  recognize   main.py on the video (background mode), one at a time, in the order the
              downloads finished; an interrupted run is continued where it stopped
  beautify    the final Beautify of the session (optional), one at a time
A failed or interrupted stage puts the task on hold (with the reason); restarting the stage
takes it off hold. When nothing is left to do, `run` exits.

Tasks are CSV rows:  url[,project[,auto_frames[,beautify]]]   (a header row is optional;
project defaults to the video id, auto_frames / beautify to 1; 1/0, yes/no, true/false).
url is a YouTube link, or a stream (.m3u8) / video file link. A whole cURL command works as
well (the browser's DevTools → Network → the .m3u8 request → Copy as cURL): its headers
(Referer, Cookie, ...) go along with every request, so videos behind a login download too.

  python batch.py run tasks.csv [--parallel 3]   # standalone; state in tasks.csv.state.json,
                                                 # running it again resumes / retries held stages
  python batch.py submit tasks.csv [--server http://127.0.0.1:8765]   # into the web UI's queue
  python batch.py list [--server ...]            # the web UI's queue

The web UI (server.py) runs the same Orchestrator with its own runners, so the transcription
shows live on the main screen; its queue is .webui/batch.json, the page is /batch.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import sys
import sysconfig
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import external
import native_helpers
import sessions as S

STAGES = ("download", "recognize", "beautify")
DEFAULT_PARALLEL = 3
LOG_LINES = 200
VIDEO_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".m4v")

YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/|v/)|youtu\.be/|youtube-nocookie\.com/embed/)"
    r"([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str | None:
    url = (url or "").strip()
    if YT_ID_RE.match(url):
        return url
    m = YT_URL_RE.search(url)
    return m.group(1) if m else None


def _bool(v, default=True) -> bool:
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "yes", "y", "true", "on", "x", "+")


# Request headers of a copied cURL that must not go along: conditional / range requests
# (a 304 instead of the playlist), transport details, the browser's own fingerprinting.
DROP_HEADERS = ("if-", "range", "accept-encoding", "content-length", "content-type", "host", "connection",
                "sec-", "priority", "cache-control", "pragma", "upgrade-insecure-requests", "te")
SECRET_HEADERS = ("cookie", "authorization", "x-csrf-token", "x-xsrf-token")
STREAM_SUFFIXES = (".m3u8",) + VIDEO_SUFFIXES
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# curl options that take a value we don't need (the value must be skipped, not taken for the url)
_CURL_ARG_OPTS = {"-X", "--request", "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
                  "-o", "--output", "-u", "--user", "-x", "--proxy", "-m", "--max-time", "--connect-timeout",
                  "-F", "--form", "-w", "--write-out", "-r", "--range", "--retry", "-c", "--cookie-jar"}


def parse_curl(cmd: str) -> dict:
    """A 'Copy as cURL (bash)' command → {"url", "headers"}; raises ValueError."""
    try:
        args = shlex.split(cmd.replace("\\\r\n", " ").replace("\\\n", " ").rstrip().rstrip("\\"))
    except ValueError as e:
        raise ValueError(f"Cannot read the cURL command: {e}")
    url, headers = "", {}

    def header(name, value):
        name = name.strip()
        if name and not any(name.lower() == d or (d.endswith("-") and name.lower().startswith(d))
                            for d in DROP_HEADERS):
            headers[name] = value.strip()
    i = 1
    while i < len(args):
        a, nxt = args[i], (args[i + 1] if i + 1 < len(args) else "")
        if a in ("-H", "--header"):
            name, _, value = nxt.partition(":")
            header(name, value)
            i += 2
        elif a in ("-b", "--cookie"):
            header("Cookie", nxt)
            i += 2
        elif a in ("-A", "--user-agent"):
            header("User-Agent", nxt)
            i += 2
        elif a in ("-e", "--referer"):
            header("Referer", nxt)
            i += 2
        elif a == "--url":
            url = nxt
            i += 2
        elif a in _CURL_ARG_OPTS:
            i += 2
        elif a.startswith("-"):
            i += 1                  # --compressed, -s, -L, ...
        else:
            url = url or a
            i += 1
    if not url:
        raise ValueError("The cURL command has no URL")
    return {"url": url, "headers": headers}


def split_entries(text: str) -> list[str]:
    """Pasted text → entries: a cURL command (with its \\ continuation lines) or a single line."""
    out, cur = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if cur is not None and (not stripped or re.match(r"(curl(\.exe)?\s|https?://|#)", stripped, re.I)):
            out.append("\n".join(cur))      # a stray "\" on the command's last line
            cur = None
        if cur is not None:
            cur.append(stripped)
            if not stripped.endswith("\\"):
                out.append("\n".join(cur))
                cur = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"curl(\.exe)?\s", stripped, re.I) and stripped.endswith("\\"):
            cur = [stripped]
        else:
            out.append(stripped)
    if cur:
        out.append("\n".join(cur))
    return out


def parse_rows(text: str) -> list[dict]:
    """CSV text / links / cURL commands → task specs {url, headers, project, auto_frames, beautify}.
    Skips a header and blanks."""
    out = []
    for entry in split_entries(text):
        if re.match(r"curl(\.exe)?\s", entry, re.I):
            try:
                out.append({**parse_curl(entry), "project": "", "auto_frames": True, "beautify": True})
            except ValueError as e:
                out.append({"url": entry.split()[1] if len(entry.split()) > 1 else entry, "error": str(e)})
            continue
        row = [c.strip() for c in next(csv.reader([entry]), [])]
        if not row or not row[0] or row[0].lower() == "url":
            continue
        row += [""] * (4 - len(row))
        out.append({"url": row[0], "project": row[1], "auto_frames": _bool(row[2]), "beautify": _bool(row[3])})
    return out


def stream_id(url: str) -> str:
    """A stable id for a stream link: the video's UUID in the path (Bunny, Vimeo-like CDNs keep one
    per video, the same for its playlist.m3u8 and 480p/video.m3u8), else a hash of the link."""
    path = urlsplit(url).path
    m = UUID_RE.search(path)
    if m:
        return m.group(0).lower()
    return hashlib.sha1(f"{urlsplit(url).netloc}{path}".encode()).hexdigest()[:12]


def check_spec(spec: dict) -> dict:
    """Validated task spec; raises ValueError with a readable reason."""
    if spec.get("error"):
        raise ValueError(spec["error"])
    url = str(spec.get("url") or "").strip()
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    vid = youtube_id(url)
    if vid:
        kind = "youtube"
        if YT_ID_RE.match(url):
            url = f"https://www.youtube.com/watch?v={vid}"
    else:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"Not a YouTube, .m3u8 or video link: {url!r}")
        if not parts.path.lower().endswith(STREAM_SUFFIXES) and not headers:
            raise ValueError(f"Not a YouTube, .m3u8 or video link: {url!r} "
                             "(for another page, copy its video request as cURL)")
        kind, vid = "stream", stream_id(url)
    project = str(spec.get("project") or "").strip() or (vid if kind == "youtube" else vid[:8])
    S.safe_name(project)        # letters, digits, . _ - and spaces
    out = {"url": url, "kind": kind, "video_id": vid, "project": project,
           "auto_frames": bool(spec.get("auto_frames", True)), "beautify": bool(spec.get("beautify", True))}
    if headers:
        out["headers"] = headers
    return out


def preview_rows(text: str) -> list[dict]:
    """parse_rows + check_spec for the Add form: every row with its id/kind, or its error.
    Header values are kept (the form sends them back), the names are listed for display."""
    out = []
    for spec in parse_rows(text):
        try:
            c = check_spec(spec)
            out.append({**c, "header_names": list(c.get("headers", {}))})
        except ValueError as e:
            out.append({"url": spec.get("url", ""), "error": str(e)})
    return out


def header_args(task: dict) -> list[str]:
    return [a for k, v in (task.get("headers") or {}).items() for a in ("--add-header", f"{k}:{v}")]


def masked(cmd: list[str]) -> str:
    """A command line for the log, without cookies / tokens."""
    out = []
    for a in cmd:
        name, sep, _ = a.partition(":")
        out.append(shlex.quote(f"{name}: …" if sep and name.lower() in SECRET_HEADERS else a))
    return " ".join(out)


def public_task(task: dict) -> dict:
    """A task for the UI / API: header names only, never their values."""
    out = {k: v for k, v in task.items() if k != "headers"}
    if task.get("headers"):
        out["header_names"] = list(task["headers"])
    return out


def new_task(spec: dict) -> dict:
    spec = check_spec(spec)
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:10], **spec, "title": "", "created": now,
        "stages": {
            "download": {"status": "pending", "ready": now},
            "recognize": {"status": "pending"},
            "beautify": {"status": "pending" if spec["beautify"] else "skipped"},
        },
    }


def task_state(task: dict) -> str:
    """One word for the whole task: hold / done / <stage running> / waiting-<stage> / queued."""
    st = task["stages"]
    if any(s["status"] == "hold" for s in st.values()):
        return "hold"
    if all(s["status"] in ("done", "skipped") for s in st.values()):
        return "done"
    for name in STAGES:
        if st[name]["status"] == "running":
            return name
    for name in STAGES:
        if st[name]["status"] == "pending":
            return "queued" if name == "download" else f"waiting-{name}"
    return "queued"


YTDLP_STALE_DAYS = 60       # YouTube changes often: an older yt-dlp tends to stop working
# How to install / update yt-dlp and deno (the JavaScript runtime yt-dlp needs for YouTube) here.
# The apps bundle both (pip yt-dlp[default,deno]); a newer yt-dlp installed this way wins over
# theirs, so the update hint is an install (winget and brew upgrade what is already there).
if native_helpers.IS_WINDOWS:
    HINTS = {"ytdlp": "winget install yt-dlp.yt-dlp", "ytdlp_update": "winget install yt-dlp.yt-dlp",
             "deno": "winget install DenoLand.Deno"}
elif native_helpers.IS_MAC:
    HINTS = {"ytdlp": "brew install yt-dlp", "ytdlp_update": "brew install yt-dlp", "deno": "brew install deno"}
else:
    HINTS = {"ytdlp": "pip install yt-dlp", "ytdlp_update": "pip install -U yt-dlp",
             "deno": "curl -fsSL https://deno.land/install.sh | sh"}
_ytdlp_found: tuple[float, dict | None] = (0.0, None)


def ytdlp_env() -> dict:
    """Environment for yt-dlp: this Python's scripts folder on PATH, where pip puts deno.
    yt-dlp run as `python -m yt_dlp` looks there itself; a standalone yt-dlp.exe does not."""
    env = native_helpers.child_env()
    scripts = sysconfig.get_path("scripts")
    if scripts and Path(scripts).is_dir():
        env["PATH"] = scripts + os.pathsep + env.get("PATH", "")
    return env


def _ytdlp_candidates() -> list[list[str]]:
    """Every yt-dlp we can see: PATH, the usual install places, conda's base env, this Python."""
    names = ["yt-dlp.exe", "yt-dlp"] if native_helpers.IS_WINDOWS else ["yt-dlp"]
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    conda = os.environ.get("CONDA_EXE")             # …/miniforge/base/bin/conda
    if conda:
        dirs.append(str(Path(conda).parent))
    dirs += [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin",
             str(Path.home() / ".local" / "bin")]
    seen, out = set(), []
    for d in dirs:
        for n in names:
            p = Path(d) / n
            if p.is_file() and os.access(p, os.X_OK):
                real = p.resolve()
                if real not in seen:
                    seen.add(real)
                    out.append([str(p)])
    try:
        import yt_dlp  # noqa: F401
        out.append([sys.executable, "-m", "yt_dlp"])
    except ImportError:
        pass
    return out


def ytdlp_info(refresh: bool = False) -> dict | None:
    """The newest yt-dlp available: {"cmd", "version", "age_days", "others"}. VOICE_REC_YTDLP
    (a path) overrides the search. Several installs are common (brew, pip, conda) and the
    first on PATH is often an old one that YouTube no longer answers."""
    global _ytdlp_found
    if not refresh and time.time() - _ytdlp_found[0] < 300:
        return _ytdlp_found[1]
    import subprocess
    forced = os.environ.get("VOICE_REC_YTDLP")
    found = []
    for cmd in ([[forced]] if forced else _ytdlp_candidates()):
        try:
            r = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=30)
            version = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError):
            continue
        if re.match(r"^\d{4}\.\d{2}\.\d{2}", version):
            found.append((version, cmd))
    if not found:
        _ytdlp_found = (time.time(), None)
        return None
    found.sort(key=lambda x: [int(n) for n in re.findall(r"\d+", x[0])], reverse=True)
    version, cmd = found[0]
    try:
        from datetime import date
        y, m, d = (int(x) for x in version.split(".")[:3])
        age = (date.today() - date(y, m, d)).days
    except ValueError:
        age = None
    info = {"cmd": cmd, "version": version, "age_days": age,
            "deno": bool(shutil.which("deno", path=ytdlp_env()["PATH"])),
            "others": [f"{' '.join(c)} ({v})" for v, c in found[1:]]}
    _ytdlp_found = (time.time(), info)
    return info


def ytdlp_cmd() -> list[str] | None:
    info = ytdlp_info()
    return list(info["cmd"]) if info else None


# ── Queue: tasks + persistence ───────────────────────────────────────────────

class Queue:
    def __init__(self, path: Path, on_change=None):
        self.path = path
        self.on_change = on_change or (lambda task, removed=False: None)
        self.tasks: list[dict] = []
        if path.exists():
            try:
                self.tasks = json.loads(path.read_text(encoding="utf-8")).get("tasks", [])
            except Exception as e:
                print(f"[batch] could not read {path}: {e}", file=sys.stderr)
        # Whatever was running when the last process ended didn't finish.
        for t in self.tasks:
            for name, s in t["stages"].items():
                if s["status"] == "running":
                    s.update(status="hold", error="Interrupted: the batch runner stopped while this was running")
        self.save()

    def save(self):
        S._write_json(self.path, {"tasks": self.tasks})

    def get(self, task_id: str) -> dict | None:
        return next((t for t in self.tasks if t["id"] == task_id), None)

    def changed(self, task: dict, persist: bool = True):
        if persist:
            self.save()
        self.on_change(task)

    def add(self, specs: list[dict]) -> list[dict]:
        tasks = [new_task(s) for s in specs]       # all or nothing: a bad row raises first
        self.tasks.extend(tasks)
        self.save()
        for t in tasks:
            self.on_change(t)
        return tasks

    def remove(self, task_id: str):
        task = self.get(task_id)
        if task:
            self.tasks.remove(task)
            self.save()
            self.on_change(task, removed=True)

    def restart(self, task_id: str, stage: str | None = None) -> dict:
        """Take held stages (or the given one) off hold. A recognition continues where it stopped."""
        task = self.get(task_id)
        if not task:
            raise KeyError(task_id)
        names = [stage] if stage else [n for n in STAGES if task["stages"][n]["status"] == "hold"]
        for n in names:
            s = task["stages"][n]
            if s["status"] in ("hold", "done") and s["status"] != "running":
                s.update(status="pending", error="", ready=time.time(), note="")
                if n == "recognize" and stage and s.get("complete"):
                    s.pop("sid", None)          # explicit restart of a finished run: from scratch
                    s.pop("position", None)
        self.changed(task)
        return task


# ── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    """Starts stages as slots free up. Runners do the work:
         runner.recognize(task, stage, file, update) -> {"sid", "position", "complete", "error"}
         runner.beautify(task, stage, update) -> {"ok", "error", "v"}
    update(**fields) sets display fields on the stage (line, progress, note) and notifies."""

    def __init__(self, queue: Queue, runner, parallel: int = DEFAULT_PARALLEL, log=None, on_external=None):
        self.on_external = on_external or (lambda project: None)   # a project's external transcripts changed
        self.q = queue
        self.runner = runner
        self.parallel = parallel
        self.log = log or (lambda task, stage, line: None)
        self.running: dict[tuple[str, str], asyncio.Task] = {}
        self.wake = asyncio.Event()
        self._last_emit: dict[str, float] = {}

    def kick(self):
        self.wake.set()

    def busy(self, stage: str) -> int:
        return sum(1 for (_, s) in self.running if s == stage)

    def _eligible(self, stage: str) -> list[dict]:
        prev = {"recognize": "download", "beautify": "recognize"}.get(stage)
        out = [t for t in self.q.tasks if t["stages"][stage]["status"] == "pending"
               and (prev is None or t["stages"][prev]["status"] == "done")
               and (t["id"], stage) not in self.running]
        # first come, first served: when the task got to this stage
        return sorted(out, key=lambda t: t["stages"][stage].get("ready") or t["created"])

    def _schedule(self):
        for t in self._eligible("download")[:max(0, self.parallel - self.busy("download"))]:
            self._start(t, "download", self._download(t))
        for stage, fn in (("recognize", self._recognize), ("beautify", self._beautify)):
            if not self.busy(stage):
                ready = self._eligible(stage)
                if ready:
                    self._start(ready[0], stage, fn(ready[0]))

    def _start(self, task: dict, stage: str, coro):
        s = task["stages"][stage]
        s.update(status="running", started=time.time(), error="", line="", note="", progress=None)
        self.q.changed(task)
        key = (task["id"], stage)

        async def wrap():
            try:
                await coro
            except asyncio.CancelledError:
                if self.q.get(task["id"]):
                    pos = s.get("position") if stage == "recognize" else None
                    s.update(status="hold", error=f"Interrupted at {fmt_hms(pos)}: restart to continue from there"
                             if pos else "Interrupted")
            except Exception as e:
                s.update(status="hold", error=f"{type(e).__name__}: {e}")
            finally:
                if s["status"] == "running":       # a stage that forgot to say how it ended
                    s.update(status="hold", error=s.get("error") or "Stopped")
                s["finished"] = time.time()
                if s["status"] == "done":
                    nxt = STAGES.index(stage) + 1
                    if nxt < len(STAGES):
                        task["stages"][STAGES[nxt]]["ready"] = time.time()
                self.running.pop(key, None)
                if self.q.get(task["id"]):
                    self.q.changed(task)
                self.kick()

        self.running[key] = asyncio.get_running_loop().create_task(wrap())

    def cancel(self, task_id: str):
        for (tid, stage), at in list(self.running.items()):
            if tid == task_id:
                at.cancel()         # the runners' CancelledError handlers stop their processes

    def updater(self, task: dict, stage: str):
        s = task["stages"][stage]

        def update(**kw):
            s.update(kw)
            now = time.monotonic()
            if now - self._last_emit.get(task["id"], 0) > 0.4 or "error" in kw:
                self._last_emit[task["id"]] = now
                self.q.changed(task, persist=False)
        return update

    async def run(self, until_idle: bool = False):
        while True:
            self._schedule()
            if until_idle and not self.running:
                return
            await self.wake.wait()
            self.wake.clear()

    # ── stages ──

    async def _download(self, task: dict):
        s = task["stages"]["download"]
        update = self.updater(task, "download")
        info = await asyncio.to_thread(ytdlp_info)
        cmd = list(info["cmd"]) if info else None
        if not cmd:
            s.update(status="hold", error=f"yt-dlp is not installed: {HINTS['ytdlp']}")
            return
        pdir = S.project_dir(task["project"])
        for sub in ("transcripts", "screenshots", "docs", "audio"):
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        vid = task["video_id"]
        meta_dir = pdir / ".batch"
        opts = ["--newline", "--no-playlist", "--no-mtime", "--no-colors",
                # H.264 + AAC up to 1080p: plays in every browser; the frames come from it
                # only formats with real video and audio: when YouTube hands out nothing else,
                # yt-dlp would otherwise "download" the storyboard (preview images, .mhtml)
                "-f", "bv*[height<=1080]+ba/b[height<=1080][vcodec!=none][acodec!=none]/b[vcodec!=none][acodec!=none]"
                if task.get("kind", "youtube") == "youtube" else
                # a stream's variant playlist (480p/video.m3u8) says nothing about its codecs or size
                "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
                "-S", "vcodec:h264,acodec:aac",
                "--merge-output-format", "mp4",
                # named by our id: a stream's own id from yt-dlp is just "playlist" / "video"
                "-o", str(pdir / "audio" / f"{vid}.%(ext)s"),
                "--write-info-json", "-o", f"infojson:{meta_dir / vid}", *header_args(task)]
        ffmpeg = native_helpers.ffmpeg_exe()
        if ffmpeg:
            opts += ["--ffmpeg-location", ffmpeg]
        cmd += [*opts, task["url"]]
        self.log(task, "download", "$ " + masked(cmd))
        log: list[str] = s.setdefault("log", [])
        log.clear()
        log.append(f"yt-dlp {info['version']} ({' '.join(info['cmd'])})"
                   + (f" - {info['age_days']} days old, consider updating it" if (info["age_days"] or 0) > YTDLP_STALE_DAYS else ""))
        self.log(task, "download", log[-1])
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env=ytdlp_env())
        errors = []
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                log.append(line)
                del log[:-LOG_LINES]
                if line.startswith("ERROR"):
                    errors.append(line)
                m = re.search(r"\[download\]\s+([\d.]+)%", line)
                update(line=line, progress=float(m.group(1)) if m else s.get("progress"))
                self.log(task, "download", line)
            rc = await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            raise
        for junk in (pdir / "audio").glob(f"{vid}.mhtml"):      # a storyboard from an older run
            junk.unlink(missing_ok=True)
        if rc != 0:
            error = (errors[-1] if errors else "\n".join(log[-5:])) or f"yt-dlp exited with {rc}"
            if task.get("kind", "youtube") == "youtube" and (
                    any("Only images are available" in l or "Precondition check failed" in l for l in log)
                    or "Requested format is not available" in error):
                error = ("YouTube gave yt-dlp no video or audio for this video (its player requests were refused: "
                         "\"Precondition check failed\", HTTP 400). This is often temporary: restart the download. "
                         f"If it keeps failing, update yt-dlp ({HINTS['ytdlp_update']}) and install a "
                         f"JavaScript runtime it can use ({HINTS['deno']}).\n\n" + error)
            elif task.get("kind") == "stream" and re.search(r"HTTP Error (401|403)|Forbidden|Unauthorized", "\n".join(log)):
                error = ("The server refused the stream (401/403). Streams behind a login need the request's "
                         "headers: open the video in the browser, DevTools → Network → the .m3u8 request → "
                         "Copy as cURL, remove this task and add the cURL command instead. Copied headers expire "
                         "after a while (a new login, a signed link), so copy a fresh one.\n\n" + error)
            s.update(status="hold", error=error)
            return
        files = [p for p in (pdir / "audio").glob(f"{vid}.*")
                 if p.suffix.lower() in VIDEO_SUFFIXES and not p.name.endswith(".part")]
        if not files:
            s.update(status="hold", error="yt-dlp finished but the video file is not there")
            return
        file = max(files, key=lambda p: p.stat().st_mtime)
        meta = {}
        try:
            meta = json.loads((meta_dir / f"{vid}.info.json").read_text(encoding="utf-8"))
            title = meta.get("title") or ""
            # the generic extractor names a stream after its file: "playlist", "video"
            task["title"] = "" if task.get("kind") == "stream" and title.lower() in (
                "playlist", "video", "index", "master", vid) else title
            s["duration"] = meta.get("duration")
        except Exception:
            pass
        await self._subtitles(task, s, cmd[:len(cmd) - len(opts) - 1], meta, meta_dir, update)
        s.update(status="done", file=file.name, progress=100.0, line=f"Downloaded {file.name}")

    async def _subtitles(self, task, s, ytdlp, meta, meta_dir, update):
        """The video's default subtitles → the project's external transcripts (Notes), which
        Beautify and Ask use. Not fatal: a video without subtitles is transcribed all the same."""
        project, log = task["project"], s["log"]
        old = s.get("subtitles")
        if old and any(f["name"] == old["name"] for f in external.load_index(project)):
            return                                      # imported by an earlier run of this stage
        pick = pick_subtitles(meta)
        if not pick:
            log.append("No subtitles for this video")
            return
        lang, auto = pick
        update(line=f"Downloading subtitles ({lang}{', auto-generated' if auto else ''})")
        for old_vtt in meta_dir.glob(f"{task['video_id']}.*.vtt"):
            old_vtt.unlink(missing_ok=True)
        cmd = [*ytdlp, "--skip-download", "--no-playlist", "--no-colors",
               "--write-auto-subs" if auto else "--write-subs", "--sub-langs", lang, "--sub-format", "vtt",
               "-o", f"subtitle:{meta_dir / task['video_id']}", *header_args(task), task["url"]]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env=ytdlp_env())
        out = (await proc.communicate())[0].decode("utf-8", "replace")
        vtt = next(iter(meta_dir.glob(f"{task['video_id']}.*.vtt")), None)
        if proc.returncode != 0 or not vtt:
            err = next((l for l in out.splitlines() if l.startswith("ERROR")), out.strip()[-300:])
            log.append(f"Subtitles ({lang}) could not be downloaded: {err}")
            return
        data = vtt.read_bytes()
        try:
            text = external.from_youtube_vtt(data.decode("utf-8-sig", errors="replace"))
            rec = await asyncio.to_thread(
                external.add, project, f"{'YouTube s' if task.get('kind', 'youtube') == 'youtube' else 'S'}ubtitles "
                f"{task['video_id']} {lang}.vtt", data, None, text, "youtube-auto" if auto else "youtube")
        except ValueError as e:
            log.append(f"Subtitles ({lang}) skipped: {e}")
            return
        s["subtitles"] = {"name": rec["name"], "lang": lang, "auto": auto, "lines": rec["lines"]}
        log.append(f"Subtitles ({lang}{', auto-generated' if auto else ''}) added to the project's notes "
                   f"as an external transcript: {rec['name']}")
        self.log(task, "download", log[-1])
        self.on_external(project)

    async def _recognize(self, task: dict):
        s = task["stages"]["recognize"]
        file = S.project_dir(task["project"]) / "audio" / task["stages"]["download"]["file"]
        if not file.is_file():
            s.update(status="hold", error=f"The video is gone: {file} (restart the download)")
            task["stages"]["download"]["status"] = "hold"
            task["stages"]["download"]["error"] = "The downloaded file is missing"
            return
        if s.get("sid") and not s.get("position"):
            # The previous run died without saying where it got to: continue after its last line.
            s["position"] = resume_point(task["project"], s["sid"])
        res = await self.runner.recognize(task, s, file, self.updater(task, "recognize"))
        if res.get("sid"):
            s["sid"] = res["sid"]
            bind_subtitles(task, res["sid"], self.on_external)
        if res.get("position") is not None:
            s["position"] = res["position"]
        if res.get("complete"):
            s.update(status="done", complete=True, line="Transcribed")
        else:
            pos = s.get("position")
            where = f"Stopped at {fmt_hms(pos)}" if pos else "Stopped"
            s.update(status="hold", complete=False,
                     error=res.get("error") or f"{where} before the end: restart to continue from there")

    async def _beautify(self, task: dict):
        s = task["stages"]["beautify"]
        sid = task["stages"]["recognize"].get("sid")
        if not sid:
            s.update(status="hold", error="No transcription session to beautify")
            return
        res = await self.runner.beautify(task, s, sid, self.updater(task, "beautify"))
        if res.get("ok"):
            s.update(status="done", v=res.get("v"), line=f"Beautified (v{res.get('v')})" if res.get("v") else "Beautified")
        else:
            s.update(status="hold", error=res.get("error") or "Beautify failed")


def pick_subtitles(meta: dict) -> tuple[str, bool] | None:
    """The video's default subtitle track from yt-dlp's info: (language code, auto-generated).
    Uploaded subtitles first (the video's language, else the only / first track); otherwise
    YouTube's captions in the original language ('<lang>-orig'), never machine translations."""
    lang = (meta.get("language") or "").lower()
    manual = [k for k in (meta.get("subtitles") or {}) if k != "live_chat"]
    if manual:
        for k in manual:
            if lang and (k.lower() == lang or k.lower().startswith(lang + "-")):
                return k, False
        return manual[0], False
    autos = meta.get("automatic_captions") or {}
    orig = [k for k in autos if k.endswith("-orig")]
    if orig:
        return orig[0], True
    if lang and lang in autos:
        return lang, True
    return None


def bind_subtitles(task: dict, sid: str, on_external=None):
    """Imported subtitles belong to the task's session (not to every session of the project)."""
    sub = task["stages"]["download"].get("subtitles")
    if not sub:
        return
    try:
        rec = next((f for f in external.load_index(task["project"]) if f["name"] == sub["name"]), None)
        if rec and rec.get("sid") != sid:
            external.set_session(task["project"], sub["name"], sid)
            if on_external:
                on_external(task["project"])
    except Exception as e:
        print(f"[batch] could not bind subtitles: {e}", file=sys.stderr)


def fmt_hms(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def resume_point(project: str, sid: str) -> float:
    """Where a recognition that died (no clean stop) should continue: the start of its last
    speech line, which is removed (it may be cut off) and transcribed again."""
    path = S.transcript_path(project, sid)
    if not path.exists():
        return 0.0
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        m = S.LINE_RE.match(lines[i].rstrip("\n"))
        if m and not m.group(2).startswith("["):          # speech, not [screenshot: ...]
            h, mi, se = (int(x) for x in m.group(1).split(":"))
            path.write_text("".join(lines[:i]), encoding="utf-8")
            return float(h * 3600 + mi * 60 + se)
    return 0.0


def recognize_args(task: dict, stage: dict, settings: dict) -> dict:
    """The recognizer options shared by both runners: global settings + the task's."""
    return {
        "engine": settings.get("engine") or "whisper", "lang": settings.get("lang") or "auto",
        "speakers": str(settings.get("speakers") or "auto"), "pause": float(settings.get("pause") or 1.5),
        "auto_frames": bool(task.get("auto_frames")), "start": float(stage.get("position") or 0),
        "continue_sid": stage.get("sid") or "",
    }


# ── Standalone runner (CLI): main.py / beautify.py as subprocesses ───────────

class CliRunner:
    def __init__(self, python: str):
        self.python = python
        self.procs: dict[str, asyncio.subprocess.Process] = {}

    async def recognize(self, task, stage, file, update):
        o = recognize_args(task, stage, S.load_settings())
        pdir = S.project_dir(task["project"])
        events_path = pdir / ".batch" / f"recognize-{int(time.time())}.events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text("")
        cmd = [self.python, "-u", str(S.ROOT / "main.py"), task["project"], "600",
               "--source", f"file:{file}", "--events", str(events_path),
               "--lang", o["lang"], "--pause", str(o["pause"]), "--vocab-file", str(S.notes_path(task["project"]))]
        if o["engine"] != "whisper":
            cmd += ["--engine", o["engine"]]
        if o["speakers"] != "off":
            cmd += ["--speakers", o["speakers"]]
        if o["auto_frames"]:
            cmd += ["--auto-frames", "1"]
        if o["start"] > 0:
            cmd += ["--start", f"{o['start']:.2f}"]
        if o["continue_sid"] and S.transcript_path(task["project"], o["continue_sid"]).exists():
            cmd += ["--continue-session", o["continue_sid"]]
        print(f"[{task['project']}] recognize: {' '.join(cmd[2:])}")
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(S.DATA_ROOT), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env=native_helpers.child_env(),
                                                    start_new_session=True)
        self.procs["recognize"] = proc
        res = {"sid": o["continue_sid"] or None}
        tail: list[str] = []

        async def drain():
            async for raw in proc.stdout:
                for part in re.split(r"[\r\n]", raw.decode("utf-8", "replace")):
                    part = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", part).strip()
                    if part and not part.startswith(("●", "[transcribing")):
                        tail.append(part)
                        del tail[:-20]
                        if part.startswith("[") and part[1:3].isdigit():
                            print(f"[{task['project']}] {part[:160]}")

        async def follow():
            with open(events_path, encoding="utf-8") as f:
                while True:
                    line = f.readline()
                    if not line:
                        if proc.returncode is not None:
                            return
                        await asyncio.sleep(0.2)
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    t = ev.get("type")
                    if t == "session":
                        res["sid"] = ev["timestamp"]
                        stage["sid"] = ev["timestamp"]
                    elif t == "source_progress":
                        update(progress=round(100 * ev["done"] / ev["total"], 1) if ev.get("total") else None,
                               line=f"Transcribing {fmt_hms(ev.get('pos'))} of {fmt_hms((ev.get('offset') or 0) + ev.get('total', 0))}")
                    elif t == "source_done":
                        res.update(position=ev.get("position"), complete=ev.get("complete"))
                        stage["position"] = ev.get("position")
                    elif t == "status" and ev.get("detail"):
                        update(note=ev["detail"])

        try:
            await asyncio.gather(drain(), proc.wait(), follow())
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.send_signal(signal.SIGINT)     # finishes the phrase and reports where it got to
                await proc.wait()
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if '"source_done"' in line:
                    stage["position"] = json.loads(line).get("position")
            raise
        if proc.returncode not in (0, -signal.SIGINT) and not res.get("complete"):
            res["error"] = f"Recognizer exited with code {proc.returncode}:\n" + "\n".join(tail[-6:])
        return res

    async def beautify(self, task, stage, sid, update):
        cmd = [self.python, "-u", str(S.ROOT / "beautify.py"), task["project"], sid, "--mode", "final"]
        print(f"[{task['project']}] beautify: {' '.join(cmd[2:])}")
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(S.DATA_ROOT), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env=native_helpers.child_env())
        self.procs["beautify"] = proc
        tail, v = [], None
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-10]
                m = re.match(r"Beautify v(\d+)", line)
                if m:
                    v = int(m.group(1))
                update(line=line.strip()[:200])
                print(f"[{task['project']}] {line}")
            await proc.wait()
        except asyncio.CancelledError:
            proc.terminate()
            raise
        return {"ok": proc.returncode == 0, "v": v, "error": "\n".join(tail[-4:])}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _console_log(task, stage, line):
    if stage == "download":
        now = time.monotonic()
        last = _console_log.__dict__.setdefault("last", {})
        if line.startswith("[download]") and "%" in line and now - last.get(task["id"], 0) < 2:
            return              # progress lines: one every 2 s per task
        last[task["id"]] = now
    print(f"[{task['project']}] {line}")


def cmd_run(a):
    csv_path = Path(a.csv)
    state_path = Path(a.state) if a.state else csv_path.with_name(csv_path.name + ".state.json")
    last_state: dict[str, str] = {}

    def on_change(task, removed=False):
        state = task_state(task)
        if last_state.get(task["id"]) != state:
            last_state[task["id"]] = state
            held = next((f"{n}: {s.get('error')}" for n, s in task["stages"].items() if s["status"] == "hold"), "")
            print(f"[{task['project']}] ── {state}{' - ' + held if held else ''}")

    q = Queue(state_path, on_change)
    specs = parse_rows(csv_path.read_text(encoding="utf-8"))
    known = {(t["video_id"], t["project"]) for t in q.tasks}
    fresh = []
    for spec in specs:
        try:
            c = check_spec(spec)
        except ValueError as e:
            sys.exit(f"{csv_path}: {e}")
        if (c["video_id"], c["project"]) not in known:
            fresh.append(spec)
    q.add(fresh)
    retried = [t for t in q.tasks if task_state(t) == "hold"]
    for t in retried:
        q.restart(t["id"])       # a new run retries what was on hold
    print(f"{len(q.tasks)} task(s) ({len(fresh)} new, {len(retried)} retried) · state: {state_path}")
    python = os.environ.get("VOICE_REC_RECORDER_PYTHON") or S.load_settings().get("recorder_python") or sys.executable
    orch = Orchestrator(q, CliRunner(python), parallel=a.parallel, log=_console_log)

    async def main():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def on_sigint():
            if stop.is_set():
                return
            stop.set()
            print("\nStopping: running stages are interrupted and put on hold (run again to continue)...")
            for tid in {tid for tid, _ in orch.running}:
                orch.cancel(tid)
        try:
            loop.add_signal_handler(signal.SIGINT, on_sigint)
        except NotImplementedError:      # Windows
            pass
        await orch.run(until_idle=True)

    asyncio.run(main())
    print()
    for t in q.tasks:
        st = task_state(t)
        err = next((f" - {n}: {s.get('error')}" for n, s in t["stages"].items() if s["status"] == "hold"), "")
        print(f"  {st:<8} {t['project']:<24} {t['url']}{err}")
    sys.exit(0 if all(task_state(t) == "done" for t in q.tasks) else 1)


def _server_call(server: str, method: str, path: str, body=None):
    import urllib.request
    req = urllib.request.Request(server.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        sys.exit(f"{e.code}: {detail}")
    except OSError as e:
        sys.exit(f"Cannot reach the web UI at {server}: {e}")


def cmd_submit(a):
    specs = parse_rows(Path(a.csv).read_text(encoding="utf-8"))
    for s in specs:
        try:
            check_spec(s)
        except ValueError as e:
            sys.exit(f"{a.csv}: {e}")
    res = _server_call(a.server, "POST", "/api/batch/tasks", {"tasks": specs})
    print(f"Added {len(res.get('added', []))} task(s) to {a.server}/batch")


def cmd_list(a):
    res = _server_call(a.server, "GET", "/api/batch")
    for t in res.get("tasks", []):
        err = next((f" - {n}: {s.get('error')}" for n, s in t["stages"].items() if s["status"] == "hold"), "")
        print(f"{task_state(t):<20} {t['project']:<24} {t['url']}{err}")


def main():
    native_helpers.utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Run the tasks of a CSV here (no web UI needed)")
    r.add_argument("csv")
    r.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="Downloads at once (default 3)")
    r.add_argument("--state", default=None, help="State file (default: <csv>.state.json)")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("submit", help="Add the tasks of a CSV to the web UI's queue")
    s.add_argument("csv")
    s.add_argument("--server", default="http://127.0.0.1:8765")
    s.set_defaults(fn=cmd_submit)
    ls = sub.add_parser("list", help="Show the web UI's queue")
    ls.add_argument("--server", default="http://127.0.0.1:8765")
    ls.set_defaults(fn=cmd_list)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
