#!/usr/bin/env python3
"""Upload clips to YouTube Shorts.

  .venv/bin/python upload.py login              one-time browser login to your channel
  .venv/bin/python upload.py queue              show which clips would be uploaded next
  .venv/bin/python upload.py run                upload what is due now, then exit
  .venv/bin/python upload.py run --watch        keep running, upload new clips as they appear
  .venv/bin/python upload.py run --file CLIP    upload one specific clip now
  .venv/bin/python upload.py skip CLIP|FOLDER   never upload these clips
"""
import argparse
import functools
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from clipper import youtube

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "uploads.json")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)  # atomic: a crash can't corrupt the upload history


def channel_of(source, cache):
    """Streamer/channel name for a clip's source URL (looked up once, then cached)."""
    if not source or not source.startswith("http"):
        return None, None
    if source not in cache:
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--skip-download",
                            "--no-playlist", "--print", "%(channel)s\t%(channel_url)s", source],
                           capture_output=True, text=True)
        line = (p.stdout.strip().splitlines() or [""])[0]
        name, _, url = line.partition("\t")
        cache[source] = [name if name and name != "NA" else None, url if url != "NA" else None]
    return tuple(cache[source])


def find_clips(cfg, state):
    """All clips listed in clips.json files under the configured folders."""
    out = []
    for d in cfg["clip_dirs"]:
        base = os.path.join(ROOT, d)
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith("_")]
            if "clips.json" not in files:
                continue
            meta = load_json(os.path.join(dirpath, "clips.json"), {})
            for c in meta.get("clips", []):
                path = os.path.join(dirpath, c["file"])
                if not os.path.exists(path):
                    continue
                out.append({"path": path, "key": os.path.relpath(path, ROOT),
                            "score": c.get("score", 0), "source": meta.get("source", ""),
                            "folder": os.path.basename(dirpath),
                            "mtime": os.path.getmtime(path)})
    return out


def pending(cfg, state, min_score, since=0.0):
    done = state["uploads"]
    clips = [c for c in find_clips(cfg, state)
             if c["key"] not in done and c["score"] >= min_score and c["mtime"] >= since]
    return sorted(clips, key=lambda c: -c["score"])  # best first


def metadata(c, cfg, state):
    over = cfg.get("streamers", {}).get(c["folder"]) or {}
    name, url = channel_of(c["source"], state.setdefault("channels", {}))
    streamer = over.get("name") or name or "Streamer"
    vals = {"streamer": streamer, "source": c["source"] or "",
            "channel_url": over.get("channel_url") or url or "", "score": f"{c['score']:.2f}"}
    recent = [u.get("title", "") for u in sorted(state["uploads"].values(),
                                                  key=lambda u: -u.get("time", 0))[:len(cfg["titles"]) - 1]]
    options = [t for t in cfg["titles"] if t.format(**vals) + " #shorts" not in recent
               and t.format(**vals) not in recent] or cfg["titles"]
    title = random.choice(options).format(**vals)  # avoid repeating a recent title
    if "#shorts" not in title.lower() and len(title) <= 92:
        title += " #shorts"
    desc = cfg["description"].format(**vals)
    tags = cfg["tags"] + [streamer]
    return title, desc, tags


def uploads_today(state):
    cut = time.time() - 86400
    return sum(1 for u in state["uploads"].values() if u.get("time", 0) > cut)


def last_upload_time(state):
    return max((u.get("time", 0) for u in state["uploads"].values()), default=0)


def cmd_login(a, cfg):
    if not os.path.exists(a.client_secret):
        sys.exit(f"missing {a.client_secret} (download the Desktop OAuth client JSON)")
    print("opening browser for Google login…")
    title = youtube.login(a.client_secret, a.token)
    print(f"logged in. uploads will go to channel: {title}")


def cmd_queue(a, cfg):
    state = load_json(STATE, {"uploads": {}, "channels": {}})
    p = pending(cfg, state, a.min_score)
    print(f"{len(p)} clips waiting (score >= {a.min_score}), "
          f"{uploads_today(state)}/{a.max_per_day} uploaded in the last 24h")
    for c in p[:20]:
        title, _, _ = metadata(c, cfg, state)
        print(f"  {c['score']:.2f}  {c['key']}\n        -> {title}")
    save_state(state)


def upload_one(yt, c, cfg, state, a):
    title, desc, tags = metadata(c, cfg, state)
    publish_at = None
    if a.privacy == "scheduled":
        start = max(time.time(), last_upload_time(state)) + a.every * 60
        publish_at = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"uploading {c['key']}  (score {c['score']:.2f})\n  title: {title}")
    if a.dry_run:
        print("  [dry run] not uploaded\n  description:\n    " + desc.replace("\n", "\n    "))
        return
    vid = youtube.upload(yt, c["path"], title, desc, tags,
                         "private" if a.privacy == "scheduled" else a.privacy, publish_at)
    state["uploads"][c["key"]] = {"video_id": vid, "title": title, "time": time.time(),
                                  "privacy": a.privacy, "publish_at": publish_at,
                                  "url": f"https://youtube.com/shorts/{vid}"}
    save_state(state)
    print(f"  done: https://youtube.com/shorts/{vid}" + (f"  (goes public {publish_at})" if publish_at else ""))


def cmd_skip(a, cfg):
    state = load_json(STATE, {"uploads": {}, "channels": {}})
    targets = [os.path.abspath(t) for t in a.paths]
    n = 0
    for c in find_clips(cfg, state):
        if c["key"] in state["uploads"]:
            continue
        if any(c["path"] == t or c["path"].startswith(t.rstrip("/") + "/") for t in targets):
            state["uploads"][c["key"]] = {"skipped": True, "time": 0}
            n += 1
            print(f"skip {c['key']}")
    save_state(state)
    print(f"{n} clips marked as skipped")


def cmd_run(a, cfg):
    state = load_json(STATE, {"uploads": {}, "channels": {}})
    if a.file:
        path = os.path.abspath(a.file)
        c = next((c for c in find_clips(cfg, state) if c["path"] == path), None)
        if c is None:
            sys.exit(f"{a.file} is not listed in any clips.json under {cfg['clip_dirs']}")
        if c["key"] in state["uploads"] and not state["uploads"][c["key"]].get("dry_run"):
            sys.exit(f"already handled: {state['uploads'][c['key']]}")
        try:
            yt = None if a.dry_run else youtube.service(a.client_secret, a.token)
        except youtube.AuthError as e:
            sys.exit(str(e))
        upload_one(yt, c, cfg, state, a)
        save_state(state)
        return
    try:
        yt = None if a.dry_run else youtube.service(a.client_secret, a.token)
    except youtube.AuthError as e:
        sys.exit(str(e))
    if yt:
        print(f"channel: {youtube.channel_title(yt)}")
    while True:
        due = a.privacy == "scheduled" or time.time() - last_upload_time(state) >= a.every * 60
        room = a.max_per_day - uploads_today(state)
        p = pending(cfg, state, a.min_score, a.since)
        if p and due and room > 0:
            n = 1 if a.watch or a.privacy != "scheduled" else min(room, len(p))
            for c in p[:n]:
                try:
                    upload_one(yt, c, cfg, state, a)
                except youtube.AuthError as e:
                    sys.exit(str(e))
                except Exception as e:
                    msg = str(e)
                    print(f"  upload failed: {type(e).__name__}: {msg[:300]}")
                    if "quotaExceeded" in msg or "uploadLimitExceeded" in msg:
                        print("  YouTube daily limit reached; pausing 1 hour")
                        if not a.watch:
                            return
                        time.sleep(3600)
                if a.dry_run:
                    state["uploads"][c["key"]] = {"time": time.time(), "dry_run": True}
            if a.dry_run:
                return
        elif a.until_empty and (not p or room <= 0):
            print("all new clips handled" if not p else
                  f"daily limit {a.max_per_day} reached; {len(p)} clips left for later "
                  f"(run: .venv/bin/python upload.py run --watch)")
            return
        elif not a.watch:
            why = ("no clips waiting" if not p else
                   f"daily limit {a.max_per_day} reached" if room <= 0 else
                   f"next upload in {int((last_upload_time(state) + a.every * 60 - time.time()) / 60)} min")
            print(why)
            return
        save_state(state)
        time.sleep(20)


def main():
    cfg = load_json(os.path.join(ROOT, "upload_config.json"), None)
    if cfg is None:
        sys.exit("missing upload_config.json")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["login", "queue", "run", "skip"])
    p.add_argument("paths", nargs="*", help="for skip: clip files or folders")
    p.add_argument("--file", help="for run: upload this one clip now (ignores score/spacing)")
    p.add_argument("--watch", action="store_true", help="keep running and upload new clips as they appear")
    p.add_argument("--dry-run", action="store_true", help="show what would be uploaded, upload nothing")
    p.add_argument("--min-score", type=float, default=cfg["min_score"])
    p.add_argument("--max-per-day", type=int, default=cfg["max_per_day"])
    p.add_argument("--every", type=float, default=cfg["every_minutes"], help="minutes between uploads")
    p.add_argument("--privacy", choices=["private", "unlisted", "public", "scheduled"],
                   default=cfg["privacy"],
                   help="scheduled = upload private now, YouTube publishes every --every minutes")
    p.add_argument("--new-only", action="store_true",
                   help="ignore clips that existed before this command started (no backlog)")
    p.add_argument("--since", type=float, default=None,
                   help="only clips created after this unix time")
    p.add_argument("--until-empty", action="store_true",
                   help="with --watch: exit once no matching clips are left")
    p.add_argument("--clip-dirs", nargs="+", help="override clip_dirs from upload_config.json")
    p.add_argument("--client-secret", default=os.path.join(ROOT, "client_secret.json"))
    p.add_argument("--token", default=os.path.join(ROOT, "token.json"))
    a = p.parse_args()
    a.since = a.since if a.since is not None else (time.time() - 5 if a.new_only else 0.0)
    if a.clip_dirs:
        cfg["clip_dirs"] = a.clip_dirs
    {"login": cmd_login, "queue": cmd_queue, "run": cmd_run, "skip": cmd_skip}[a.command](a, cfg)


if __name__ == "__main__":
    main()
