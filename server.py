#!/usr/bin/env python3
"""Local web dashboard for the clipper.

  .venv/bin/python server.py            then open http://127.0.0.1:8765

Starts cli.py / live.py / upload.py / check_facecam.py as background jobs (argument lists,
never a shell), shows their logs, the clips gallery and upload status. Listens on 127.0.0.1
only, and every API call needs a per-run token, so other websites can't drive it.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HOST, PORT = "127.0.0.1", int(os.environ.get("CLIPPER_PORT", "8765"))
TOKEN = secrets.token_urlsafe(24)
JOBS_FILE = os.path.join(ROOT, "jobs.json")
LOG_DIR = os.path.join(ROOT, "logs")
PREVIEW_DIR = os.path.join(ROOT, "previews")
SERVE_DIRS = ("output", "live_output", "previews")
NOISE = re.compile(r"HF_TOKEN|Tip: experimental MPS|objc\[|AVF(Frame|Audio)Receiver|"
                   r"setPreferableTarget|One of the duplicates|ctranslate2\] \[thread")
SECTION_RE = re.compile(r"^\d{1,2}(:\d{1,2}){0,2}(\.\d+)?-\d{1,2}(:\d{1,2}){0,2}(\.\d+)?$")
NAME_RE = re.compile(r"^[\w.-]{1,60}$")
LAYOUTS = {"auto", "facecam", "filler", "blur", "crop"}
MODELS = {"tiny", "base", "small", "medium"}
PRIVACY = {"private", "unlisted", "public", "scheduled"}

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(PREVIEW_DIR, exist_ok=True)
lock = threading.Lock()


# ---------------------------------------------------------------- jobs ----
def load_jobs():
    if os.path.exists(JOBS_FILE):
        with open(JOBS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_jobs(jobs):
    tmp = JOBS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)
    os.replace(tmp, JOBS_FILE)


JOBS = load_jobs()
PROCS = {}  # job id -> Popen (only for jobs started by this server process)


def alive(job_id):
    p = PROCS.get(job_id)
    if p is not None:
        return p.poll() is None
    pid = JOBS.get(job_id, {}).get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)  # job survived a server restart (own session)
        return True
    except OSError:
        return False


def refresh_status():
    with lock:
        for jid, j in JOBS.items():
            if j["status"] == "running" and not alive(jid):
                p = PROCS.get(jid)
                code = p.returncode if p is not None else None
                j["status"] = "finished" if code in (0, None) else f"failed ({code})"
                j["ended"] = time.time()
        save_jobs(JOBS)


def start_job(kind, title, args, out=None, extra=None):
    jid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    log_path = os.path.join(LOG_DIR, f"{jid}.log")
    log = open(log_path, "w", encoding="utf-8")
    cmd = [PY, "-u"] + args
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)  # own process group: stop kills children too
    log.close()
    with lock:
        PROCS[jid] = p
        JOBS[jid] = {"id": jid, "kind": kind, "title": title, "cmd": cmd[2:], "out": out,
                     "pid": p.pid, "status": "running", "started": time.time(),
                     "log": os.path.relpath(log_path, ROOT), **(extra or {})}
        save_jobs(JOBS)
    return JOBS[jid]


def stop_job(jid):
    j = JOBS.get(jid)
    if not j or not alive(jid):
        return False
    try:
        os.killpg(j["pid"], signal.SIGTERM)  # live.py stops cleanly on SIGTERM
    except ProcessLookupError:
        return False
    with lock:
        j["status"] = "stopping"
        save_jobs(JOBS)

    def reap():
        for _ in range(60):
            if not alive(jid):
                break
            time.sleep(1)
        else:
            try:
                os.killpg(j["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        with lock:
            j["status"] = "stopped"
            j["ended"] = time.time()
            save_jobs(JOBS)
    threading.Thread(target=reap, daemon=True).start()
    return True


def read_log(jid, offset=0, limit=200_000):
    j = JOBS.get(jid)
    if not j:
        return "", 0
    path = os.path.join(ROOT, j["log"])
    if not os.path.exists(path):
        return "", 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(limit)
    text = data.decode("utf-8", "replace").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if not NOISE.search(ln)]
    return "\n".join(lines), offset + len(data)


# ------------------------------------------------------------ validation --
class Bad(ValueError):
    pass


def v_source(s, allow_file=True):
    s = (s or "").strip()
    if re.match(r"^https?://[^\s]+$", s):
        return s
    if allow_file and s and os.path.isfile(os.path.expanduser(s)):
        return os.path.abspath(os.path.expanduser(s))
    raise Bad(f"not a URL or existing file: {s!r}")


def v_choice(v, options, name):
    if v not in options:
        raise Bad(f"{name} must be one of {sorted(options)}")
    return v


def v_num(v, lo, hi, name, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise Bad(f"{name} must be a number")
    if not lo <= x <= hi:
        raise Bad(f"{name} must be between {lo} and {hi}")
    return x


def v_name(v, default):
    v = (v or "").strip() or default
    if not NAME_RE.match(v):
        raise Bad("output name: letters, digits, . _ - only")
    return v


def common_args(d):
    a = ["--layout", v_choice(d.get("layout", "facecam"), LAYOUTS, "layout"),
         "--model", v_choice(d.get("model", "base"), MODELS, "model")]
    if d.get("facecam"):
        if not re.match(r"^\d+,\d+,\d+,\d+$", d["facecam"].strip()):
            raise Bad("facecam must be x,y,w,h")
        a += ["--facecam", d["facecam"].strip()]
    if d.get("lang"):
        a += ["--lang", v_choice(d["lang"], {"hi", "en"}, "language")]
    if d.get("no_sticker"):
        a += ["--no-sticker"]
    if d.get("captions"):
        a += ["--captions"]
    return a


def upload_args(d):
    if not d.get("upload"):
        return []
    a = ["--upload", "--upload-privacy", v_choice(d.get("privacy", "private"), PRIVACY, "privacy"),
         "--upload-every", str(v_num(d.get("every", 0), 0, 1440, "upload every"))]
    if d.get("upload_min_score") not in (None, ""):
        a += ["--upload-min-score", str(v_num(d["upload_min_score"], 0, 10, "upload min score"))]
    if d.get("max_per_day") not in (None, ""):
        a += ["--upload-max-per-day", str(v_num(d["max_per_day"], 1, 100, "max per day", int))]
    return a


def job_video(d):
    src = v_source(d.get("source"))
    out = os.path.join("output", v_name(d.get("name"), time.strftime("video_%Y%m%d_%H%M%S")))
    args = ["cli.py", src, "-o", out,
            "--max-clips", str(v_num(d.get("max_clips", 5), 1, 50, "max clips", int))]
    for s in d.get("sections") or []:
        s = s.strip().replace(" ", "")
        if s:
            if not SECTION_RE.match(s):
                raise Bad(f"bad section {s!r} (use 1:00:00-1:20:00)")
            args += ["--section", s]
    args += common_args(d) + upload_args(d)
    secs = [s for s in (d.get("sections") or []) if s.strip()]
    title = f"Video: {src}" + (f"  [{', '.join(secs)}]" if secs else "")
    return start_job("video", title, args, out)


def job_live(d):
    urls = [v_source(u, allow_file=False) for u in (d.get("urls") or []) if u.strip()]
    if not urls:
        raise Bad("add at least one live URL")
    out = "live_output"
    args = ["live.py", *urls, "-o", out,
            "--min-score", str(v_num(d.get("min_score", 0.8), 0, 10, "min score")),
            "--per-hour", str(v_num(d.get("per_hour", 8), 1, 60, "clips per hour", int)),
            "--floor", str(v_num(d.get("floor", 0.3), 0, 10, "floor"))]
    args += common_args(d) + upload_args(d)
    return start_job("live", "Live: " + ", ".join(urls), args, out)


# ---------------------------------------------------------------- data ----
def jload(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def list_clips():
    ups = jload(os.path.join(ROOT, "uploads.json"), {}).get("uploads", {})
    out = []
    for d in ("output", "live_output"):
        base = os.path.join(ROOT, d)
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith("_")]
            if "clips.json" not in files:
                continue
            meta = jload(os.path.join(dirpath, "clips.json"), {})
            for c in meta.get("clips", []):
                p = os.path.join(dirpath, c["file"])
                if not os.path.exists(p):
                    continue
                key = os.path.relpath(p, ROOT)
                u = ups.get(key, {})
                status = ("skipped" if u.get("skipped") else
                          (u.get("privacy") or "uploaded") if u.get("video_id") else "not uploaded")
                out.append({"key": key, "file": c["file"], "folder": os.path.relpath(dirpath, ROOT),
                            "score": c.get("score", 0), "duration": c.get("duration"),
                            "size_mb": c.get("size_mb"), "layout": c.get("layout"),
                            "source": meta.get("source", ""), "mtime": os.path.getmtime(p),
                            "upload": status, "url": u.get("url")})
    return sorted(out, key=lambda c: -c["mtime"])


_yt_cache = {"t": 0, "v": None}


def youtube_status():
    if time.time() - _yt_cache["t"] < 300 and _yt_cache["v"]:
        return _yt_cache["v"]
    if not os.path.exists(os.path.join(ROOT, "client_secret.json")):
        v = {"ok": False, "msg": "client_secret.json missing"}
    else:
        try:
            sys.path.insert(0, ROOT)
            from clipper import youtube
            yt = youtube.service(os.path.join(ROOT, "client_secret.json"),
                                 os.path.join(ROOT, "token.json"))
            v = {"ok": True, "channel": youtube.channel_title(yt)}
        except Exception as e:  # not logged in / expired / offline
            v = {"ok": False, "msg": str(e)[:300]}
    _yt_cache.update(t=time.time(), v=v)
    return v


def safe_file(rel):
    """Map a /files/ path to a real file inside SERVE_DIRS, or None (no path traversal)."""
    p = os.path.realpath(os.path.join(ROOT, rel))
    for d in SERVE_DIRS:
        base = os.path.realpath(os.path.join(ROOT, d)) + os.sep
        if p.startswith(base) and os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------- http ----
class H(BaseHTTPRequestHandler):
    server_version = "clipper"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")  # blocks DNS-rebinding

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        if not self._host_ok() or not secrets.compare_digest(
                self.headers.get("X-Clipper-Token", ""), TOKEN):
            self._send(403, {"error": "forbidden"})
            return False
        return True

    # ---- GET
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path == "/":
            with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
                return self._send(200, f.read().replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if u.path.startswith("/files/"):
            q = parse_qs(u.query)
            if not secrets.compare_digest(q.get("t", [""])[0], TOKEN):
                return self._send(403, {"error": "forbidden"})
            p = safe_file(unquote(u.path[len("/files/"):]))
            return self._file(p) if p else self._send(404, {"error": "not found"})
        if not self._auth():
            return
        if u.path == "/api/state":
            refresh_status()
            jobs = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:50]
            return self._send(200, {"jobs": jobs})
        if u.path == "/api/clips":
            return self._send(200, {"clips": list_clips()})
        if u.path == "/api/youtube":
            if parse_qs(u.query).get("refresh"):
                _yt_cache["t"] = 0
            return self._send(200, youtube_status())
        if u.path == "/api/facecams":
            return self._send(200, jload(os.path.join(ROOT, "facecams.json"), {}))
        m = re.match(r"^/api/jobs/([\w-]+)/log$", u.path)
        if m:
            off = int(parse_qs(u.query).get("offset", ["0"])[0] or 0)
            text, new = read_log(m.group(1), off)
            return self._send(200, {"text": text, "offset": new})
        self._send(404, {"error": "not found"})

    def _file(self, path):
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):  # needed for video seeking
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---- POST
    def do_POST(self):
        if not self._auth():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad JSON"})
        path = urlparse(self.path).path
        try:
            if path == "/api/jobs/video":
                return self._send(200, job_video(d))
            if path == "/api/jobs/live":
                return self._send(200, job_live(d))
            m = re.match(r"^/api/jobs/([\w-]+)/stop$", path)
            if m:
                return self._send(200, {"ok": stop_job(m.group(1))})
            m = re.match(r"^/api/jobs/([\w-]+)/delete$", path)
            if m:
                with lock:
                    j = JOBS.get(m.group(1))
                    if j and j["status"] not in ("running", "stopping"):
                        JOBS.pop(m.group(1))
                        save_jobs(JOBS)
                return self._send(200, {"ok": True})
            if path == "/api/clips/upload":
                key = d.get("key", "")
                if not safe_file(key):
                    raise Bad("unknown clip")
                privacy = v_choice(d.get("privacy", "private"), PRIVACY - {"scheduled"}, "privacy")
                return self._send(200, start_job("upload", f"Upload: {key}",
                                                 ["upload.py", "run", "--file", key, "--privacy", privacy]))
            if path == "/api/clips/skip":
                key = d.get("key", "")
                if not safe_file(key):
                    raise Bad("unknown clip")
                r = subprocess.run([PY, "upload.py", "skip", key], cwd=ROOT,
                                   capture_output=True, text=True, timeout=60)
                return self._send(200, {"ok": r.returncode == 0, "out": r.stdout[-500:]})
            if path == "/api/uploader":
                args = ["upload.py", "run", "--watch",
                        "--privacy", v_choice(d.get("privacy", "private"), PRIVACY, "privacy"),
                        "--every", str(v_num(d.get("every", 90), 0, 1440, "every")),
                        "--min-score", str(v_num(d.get("min_score", 0.9), 0, 10, "min score"))]
                return self._send(200, start_job("uploader", "Upload queue (all folders)", args))
            if path == "/api/youtube/login":
                _yt_cache["t"] = 0
                return self._send(200, start_job("login", "YouTube login (browser opens)",
                                                 ["upload.py", "login"]))
            if path == "/api/facecam/check":
                src = v_source(d.get("source"))
                prev = os.path.join("previews", f"facecam_{int(time.time())}.jpg")
                return self._send(200, start_job("facecam", f"Facecam check: {src}",
                                                 ["check_facecam.py", src, "--preview", prev],
                                                 extra={"preview": prev, "source": src}))
            if path == "/api/facecam/save":
                src = v_source(d.get("source"), allow_file=False)
                args = ["check_facecam.py", src, "--save"]
                if d.get("none"):
                    args += ["--none"]
                elif d.get("rect"):
                    if not re.match(r"^\d+,\d+,\d+,\d+$", d["rect"].strip()):
                        raise Bad("rect must be x,y,w,h")
                    args += ["--set", d["rect"].strip()]
                prev = os.path.join("previews", f"facecam_{int(time.time())}.jpg")
                return self._send(200, start_job("facecam", f"Facecam save: {src}",
                                                 args + ["--preview", prev],
                                                 extra={"preview": prev, "source": src}))
        except Bad as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})


def main():
    refresh_status()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f"Clipper dashboard: http://{HOST}:{PORT}   (Ctrl+C to quit; running jobs keep going)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
