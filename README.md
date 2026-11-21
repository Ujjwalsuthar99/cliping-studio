# Clipping Studio

Auto-clip highlights from gaming streams and VODs into 30–45 s vertical Shorts, and optionally
upload them to YouTube. Everything runs locally: no paid APIs.

- **Videos / VODs**: whole video or only chosen time sections.
- **Live streams**: records YouTube/Kick streams and clips while they run (several at once).
- **Highlight detection**: loudness spikes + reaction keywords (English/Hindi) + speech density.
- **Shorts layout**: streamer webcam on top, gameplay below (auto-detected and remembered per
  streamer), like & subscribe sticker, 1080×1920 H.264, under 50 MB.
- **YouTube upload**: official Data API, score filter, pacing, daily cap, no duplicates.
- **Web dashboard** for all of the above.

> **Content rights.** Clipping and re-uploading someone else's stream needs their permission or
> clip policy. Without it expect Content ID claims, strikes and no monetization. The scorer only
> listens to audio; it cannot tell if a scene is unsafe (e.g. in-game strip clubs), so review
> clips before they go public.

## Requirements

- macOS (tested on Apple Silicon) or Linux
- Python **3.10+** (yt-dlp dropped 3.9)
- [Deno](https://deno.com) on `PATH` (yt-dlp needs a JS runtime for YouTube)

ffmpeg is bundled via `imageio-ffmpeg`; no system ffmpeg needed.

## Install

```bash
brew install python@3.12 deno          # macOS
git clone <this repo> clipper && cd clipper
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The first run downloads the Whisper model (~150 MB for `base`, ~500 MB for `small`).

## Quick start: dashboard

```bash
.venv/bin/python server.py
```

Open http://127.0.0.1:8765. Tabs: **Video / VOD**, **Live streams**, **Jobs** (logs, stop),
**Clips** (preview, upload, skip), **Facecam**, **YouTube**. The server listens on localhost only
and needs a per-run token, so other sites/devices can't use it. Jobs keep running if you close
the server.

## Command line

### Finished video or VOD

```bash
# whole video
.venv/bin/python cli.py "<video url or file>" -o output --layout facecam --model base

# only some parts (downloaded and searched); --max-clips is per section
.venv/bin/python cli.py "<video url>" --section 1:00:00-1:20:00 --section 3:15:00-3:40:00 \
    --max-clips 5 --layout facecam --model base

# ...and upload the results
.venv/bin/python cli.py "<video url>" --max-clips 5 --model base --upload --upload-privacy public
```

Output: `clip_<n>_score<score>_<time>.mp4` plus `clips.json` (times, score, layout, title) in
the output folder. The downloaded video is cached in `<output>/_work/`.

### Live streams

```bash
.venv/bin/python live.py "<live url>" "<another live url>" --layout facecam --model base

# upload each clip as soon as it is cut
.venv/bin/python live.py "<live url>" --layout facecam --model base --upload --upload-privacy public
```

Clips go to `live_output/<stream>/`. Stop with Ctrl+C or `kill <pid>`.

- `--min-score 0.8` – moments at/above this are clipped immediately.
- `--per-hour 8` – target and cap. If 60/N minutes pass without a strong moment, the best
  moment of that stretch is clipped (if above `--floor 0.3`).
- `--warmup 180` – seconds recorded before clipping starts.

Live mode keeps only the last ~minute of the stream on disk plus the best moment so far
(~60–500 MB per stream).

### Common options (`cli.py` and `live.py`)

| Option | Default | Meaning |
|---|---|---|
| `--layout` | `auto` | `facecam`, `filler`, `blur`, `crop`, `auto` |
| `--model` | `small` | Whisper: `tiny`, `base` (fast), `small`, `medium` |
| `--lang` | auto | force `hi` / `en` |
| `--facecam x,y,w,h` | saved/auto | webcam rectangle in source pixels |
| `--min` / `--max` | 30 / 45 | clip length (s) |
| `--max-mb` | 50 | size cap per clip |
| `--sticker PNG` / `--no-sticker` | like & subscribe | overlay sticker |
| `--captions` | off | burn in Whisper captions |
| `--filler-dir` | `assets/filler` | background videos for the `filler` layout |
| `--device` | `cpu` | `mps` exists but was slower on M1 Pro |
| `--upload`, `--upload-privacy`, `--upload-min-score`, `--upload-every`, `--upload-max-per-day` | | auto upload |

### Score

Per second: 50% loudness vs. the stream's normal level, 30% reaction keywords, 20% speech
density, smoothed over ~8 s. Roughly: **< 0.4** normal gameplay, **0.6–0.8** noticeable
reaction, **0.8–1.3+** clear highlight. It is relative to each stream.

## Facecam

Every streamer places the webcam differently, but keeps it in the same spot, so the position is
stored per channel in `facecams.json`. Order of use: `--facecam` > confirmed per-channel rect >
auto-detection (remembered, with a `facecam_preview.jpg` next to the clips) > last saved rect.

```bash
.venv/bin/python check_facecam.py "<any video/live URL of the streamer>"           # detect + preview
.venv/bin/python check_facecam.py "<URL>" --save                                    # confirm it
.venv/bin/python check_facecam.py "<URL>" --set 0,458,388,244 --save                # set by hand
.venv/bin/python check_facecam.py "<URL>" --none --save                             # no webcam
```

Do this once for each new streamer.

## YouTube upload setup

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and enable
   **YouTube Data API v3**.
2. **OAuth consent screen / Audience**: External, Testing, and add your Google account under
   **Test users** (otherwise login fails with `Error 403: access_denied`).
3. **Credentials → OAuth client ID → Desktop app**, download it as `client_secret.json` in this
   folder. Never commit it.
4. Log in once (opens a browser; pick the channel if you have several):

   ```bash
   .venv/bin/python upload.py login
   ```

   Test-mode logins expire after 7 days; run `login` again when asked.

### Uploader

```bash
.venv/bin/python upload.py queue                       # what would upload next
.venv/bin/python upload.py run --dry-run               # preview title/description
.venv/bin/python upload.py run                         # upload the next due clip
.venv/bin/python upload.py run --watch                 # keep uploading new clips
.venv/bin/python upload.py run --file <clip.mp4> --privacy public
.venv/bin/python upload.py skip <clip or folder>       # never upload these
```

Settings live in `upload_config.json`: privacy, `min_score` (0.9), `max_per_day` (6),
`every_minutes` (90), title templates, description (credits the streamer and links the stream),
tags, and optional streamer name overrides. `--privacy scheduled` uploads as private and lets
YouTube publish at spaced times, which gives you a window to delete bad clips.

History is in `uploads.json` (one entry per clip with the video link), so nothing uploads twice.
YouTube allows 100 uploads/day per project by default.

## Project layout

```
cli.py              clip a finished video / sections
live.py             clip live streams
upload.py           YouTube uploader (login, queue, run, skip)
check_facecam.py    detect / save a streamer's webcam position
server.py, ui/      web dashboard
upload_config.json  upload settings and templates
clipper/
  analyze.py        loudness, Whisper, scoring, clip selection
  export.py         layouts, sticker, encoding, size cap
  facecam.py        webcam detection (YuNet + overlay border search)
  profiles.py       per-channel facecam memory
  recorder.py       live HLS segment recorder
  live.py           live clipping loop
  pipeline.py       video pipeline
  source.py         downloads / sections (yt-dlp)
  youtube.py        YouTube Data API
models/             YuNet face detector (OpenCV Zoo)
assets/             sticker, filler videos
```

## Troubleshooting

- **yt-dlp errors / "page needs to be reloaded"**: `.venv/bin/pip install -U "yt-dlp[default]"`
  and make sure `deno` is installed.
- **Slow**: use `--model base`; for VODs use `--section`.
- **Wrong facecam**: run `check_facecam.py` and save the right rect.
- **No live clips**: watch the status line "best moment since last update"; lower
  `--min-score` or raise `--per-hour`.
- **`objc ... implemented in both`, `setPreferableTarget`, "MPS backend" tips**: harmless
  warnings from OpenCV / PyAV / CTranslate2.
- **Upload: login expired**: `.venv/bin/python upload.py login`.

## Credits

[yt-dlp](https://github.com/yt-dlp/yt-dlp), [streamlink](https://streamlink.github.io),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), [FFmpeg](https://ffmpeg.org),
[OpenCV](https://opencv.org) and the YuNet model from
[OpenCV Zoo](https://github.com/opencv/opencv_zoo).
