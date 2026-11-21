"""Record a live HLS stream as small numbered segments on disk.

YouTube live serves separate video and audio playlists with matching sequence numbers, and
ffmpeg's HLS reader stalls on them, so we poll the playlists ourselves and download each
segment. Kick (via streamlink) serves one muxed playlist; the same code handles both.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _get(url: str, timeout: float = 20) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def resolve(url: str, max_height: int = 1080):
    """Return HLS media playlist URLs: [video, audio] or [muxed]. None if not live."""
    if "youtube.com" in url or "youtu.be" in url:
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-g",
                            "--match-filter", "is_live", "--no-playlist", "-f",
                            f"bv*[height<={max_height}][protocol*=m3u8]+ba[protocol*=m3u8]/"
                            f"b[height<={max_height}][protocol*=m3u8]", url],
                           capture_output=True, text=True)
        urls = [u for u in p.stdout.split() if u.startswith("http")]
        return urls or None
    q = f"{max_height}p60,{max_height}p,720p60,720p,best"
    p = subprocess.run([sys.executable, "-m", "streamlink", "--stream-url", url, q],
                       capture_output=True, text=True)
    u = p.stdout.strip()
    return [u] if u.startswith("http") else None


def _parse(playlist: str, base: str):
    """-> (media_sequence, [(seq, duration, url)], ended)"""
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", playlist)
    seq = int(m.group(1)) if m else 0
    out = []
    for dm in re.finditer(r"#EXTINF:([\d.]+)[^\n]*\n(?:#[^\n]*\n)*([^#\s][^\n]*)", playlist):
        u = dm.group(2).strip()
        if not u.startswith("http"):
            u = urllib.parse.urljoin(base, u)
        out.append((seq, float(dm.group(1)), u))
        seq += 1
    return out, "#EXT-X-ENDLIST" in playlist


class Recorder(threading.Thread):
    """Downloads segments into segdir. self.timeline = [(t_start, dur, seq)] of complete segments."""

    def __init__(self, url: str, segdir: str, log, poll_offline: float = 60.0):
        super().__init__(daemon=True)
        self.url, self.segdir, self.log = url, segdir, log
        self.poll_offline = poll_offline
        self.timeline = []          # complete segments, in order: (t_start, dur, key)
        self.last_seq = None        # last playlist sequence added to the timeline
        self.session = 0            # bumps when the stream restarts (sequence numbers reset)
        self._deleted = set()       # segment keys already removed from disk
        self._dropped_upto = 0      # timeline index before which everything is deleted
        self.live = False
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        os.makedirs(segdir, exist_ok=True)

    def duration(self) -> float:
        with self.lock:
            return self.timeline[-1][0] + self.timeline[-1][1] if self.timeline else 0.0

    def segments(self, t0: float, t1: float):
        """Complete segments overlapping [t0, t1]: [(t_start, dur, seq)]."""
        with self.lock:
            return [s for s in self.timeline if s[0] + s[1] > t0 and s[0] < t1]

    def _path(self, kind: str, seq: int, session=None) -> str:
        return os.path.join(self.segdir, f"{kind}_{self.session if session is None else session:03d}_{seq:09d}.ts")

    def paths(self, key):
        session, seq = key
        v = self._path("v", seq, session)
        a = self._path("a", seq, session)
        return v, (a if os.path.exists(a) else None)

    def drop_before(self, t: float, keep=()):
        """Delete segment files that end before stream time t, except inside keep ranges.

        keep: [(t0, t1)] stream-time ranges to hold on to (e.g. the best moment so far).
        """
        with self.lock:
            old = [s for s in self.timeline[self._dropped_upto:] if s[0] + s[1] < t]
        for st, d, key in old:
            if key in self._deleted or any(st < b and st + d > a for a, b in keep):
                continue
            for p in self.paths(key):
                if p and os.path.exists(p):
                    os.remove(p)
            self._deleted.add(key)
        # advance past the contiguous run of deleted segments so later calls stay cheap
        with self.lock:
            while (self._dropped_upto < len(self.timeline)
                   and self.timeline[self._dropped_upto][2] in self._deleted):
                self._deleted.discard(self.timeline[self._dropped_upto][2])
                self._dropped_upto += 1

    def stop(self):
        self.stop_flag.set()

    def run(self):
        while not self.stop_flag.is_set():
            urls = resolve(self.url)
            if not urls:
                if self.live:
                    self.log("stream ended / offline, waiting")
                self.live = False
                self.stop_flag.wait(self.poll_offline)
                continue
            self.live = True
            self.log(f"recording ({'video+audio' if len(urls) == 2 else 'muxed'} playlist)")
            try:
                self._record(urls)
            except Exception as e:  # network hiccup, expired URL, etc. -> re-resolve
                self.log(f"recorder error: {type(e).__name__}: {e}; re-resolving")
                self.stop_flag.wait(5)

    def _record(self, urls):
        kinds = ["v", "a"][: len(urls)]
        done = {k: set() for k in kinds}
        first = True
        fails = 0
        resolved_at = time.time()
        last_new = time.time()
        while not self.stop_flag.is_set():
            if time.time() - resolved_at > 3 * 3600:
                return  # signed URLs expire after a few hours; refresh proactively
            lists, ended = {}, False
            try:
                for k, u in zip(kinds, urls):
                    lists[k], e = _parse(_get(u).decode("utf-8", "replace"), u)
                    ended = ended or e
                fails = 0
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                fails += 1
                if fails >= 5:
                    raise RuntimeError(f"playlist fetch failing: {e}")
                self.stop_flag.wait(2)
                continue
            vlist = lists["v"]
            if first and vlist:
                newest = vlist[-1][0]
                if self.last_seq is not None and newest >= self.last_seq - 5 \
                        and vlist[0][0] <= self.last_seq + 1:
                    start = self.last_seq + 1        # same broadcast: resume without a gap
                else:
                    if self.last_seq is not None:
                        self.session += 1            # new broadcast: sequence restarted
                    start = newest - 2               # start near the live edge, not the DVR
                    self.last_seq = start - 1
                for k in kinds:
                    done[k] = {s for s, _, _ in lists[k] if s < start}
                first = False
            for k in kinds:
                for seq, _, u in lists[k]:
                    if seq in done[k]:
                        continue
                    path = self._path(k, seq)
                    with open(path, "wb") as f:
                        f.write(_get(u, timeout=30))
                    done[k].add(seq)
            if vlist and vlist[-1][0] > (self.last_seq or -1):
                last_new = time.time()
            elif time.time() - last_new > 90:
                self.log("no new video for 90s (stream stalled or ended), reconnecting")
                self.live = False
                return
            # extend timeline with segments that now have all parts
            durs = {s: d for s, d, _ in vlist}
            ready = sorted(s for s in done["v"] if all(s in done[k] for k in kinds) and s in durs)
            with self.lock:
                for s in ready:
                    if s <= self.last_seq:
                        continue
                    t = self.timeline[-1][0] + self.timeline[-1][1] if self.timeline else 0.0
                    self.timeline.append((t, durs[s], (self.session, s)))
                    self.last_seq = s
            if ended:
                return
            target = max(1.0, min(d for _, d, _ in vlist) if vlist else 2.0)
            self.stop_flag.wait(target)
