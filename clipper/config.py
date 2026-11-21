from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Reaction words/phrases that raise a moment's score. English + Hinglish + Hindi. Edit freely.
DEFAULT_KEYWORDS: List[str] = [
    "no way", "oh my god", "omg", "let's go", "lets go", "what the", "bro",
    "crazy", "insane", "holy", "wtf", "are you kidding", "clutch", "dead",
    "gta", "wanted", "cops", "police", "heist", "crash",
    "arre", "kya baat", "pagal", "bhai", "oye", "wah", "kamaal",
    "अरे", "भाई", "पागल", "क्या बात", "वाह", "कमाल",
]


@dataclass
class Config:
    # Shorts constraints
    min_len: float = 30.0
    max_len: float = 45.0
    target_len: float = 38.0
    max_size_mb: float = 50.0
    width: int = 1080
    height: int = 1920
    fps: int = 30
    # detection
    max_clips: int = 10
    min_gap: float = 20.0          # min seconds between clips
    candidates_per_clip: float = 1.5   # loud stretches to transcribe per wanted clip
    full_transcribe: bool = False      # True = run Whisper on the whole video (slow)
    w_audio: float = 0.5
    w_keywords: float = 0.3
    w_speech: float = 0.2
    keywords: List[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    # whisper
    whisper_model: str = "small"
    language: Optional[str] = None  # None = auto-detect
    device: str = "cpu"             # "cpu" or "mps" (Apple GPU, experimental)
    compute_type: str = "int8"      # int8 for cpu; try float16 on mps
    # layout:
    #   auto    = facecam split if a webcam is found, else filler split, else blur
    #   facecam = webcam on top, gameplay below
    #   filler  = gameplay on top, looping filler video below (from filler_dir)
    #   blur    = full frame centered over blurred copy
    #   crop    = center crop
    layout: str = "auto"
    facecam_rect: Optional[Tuple[int, int, int, int]] = None  # manual x,y,w,h in source px
    facecam_frac: float = 0.35     # share of height for the webcam panel
    filler_dir: str = "assets/filler"
    captions: bool = False         # burned-in Whisper captions (off: transcripts are rough)
    # like & subscribe sticker (transparent PNG). None = no sticker.
    sticker: Optional[str] = "assets/stickers/like_subscribe.png"
    sticker_width: float = 0.8     # share of frame width
    # (start, seconds shown); negative start = seconds before the clip ends
    sticker_times: List[Tuple[float, float]] = field(default_factory=lambda: [(3.0, 4.0), (-7.0, 5.0)])
