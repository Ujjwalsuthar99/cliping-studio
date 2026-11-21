"""Generate the default "LIKE / SUBSCRIBE" sticker (transparent PNG) with Pillow."""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"


def _emoji(ch: str, size: int) -> Image.Image:
    f = ImageFont.truetype(EMOJI, 160)  # Apple emoji only renders at fixed sizes
    im = Image.new("RGBA", (180, 180), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((10, 10), ch, font=f, embedded_color=True)
    im = im.crop(im.getbbox())
    return im.resize((size, int(size * im.height / im.width)), Image.LANCZOS)


def _pill(text: str, icon: str, fill, fg, h: int = 150) -> Image.Image:
    font = ImageFont.truetype(FONT, int(h * 0.42))
    tw = int(ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength(text, font=font))
    ic = _emoji(icon, int(h * 0.62))
    pad = int(h * 0.32)
    w = pad + ic.width + int(h * 0.18) + tw + pad
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=fill)
    im.alpha_composite(ic, (pad, (h - ic.height) // 2))
    d.text((pad + ic.width + int(h * 0.18), h // 2), text, font=font, fill=fg, anchor="lm")
    return im


def make_default(path: str) -> str:
    like = _pill("LIKE", "👍", (255, 255, 255, 255), (20, 20, 20, 255))
    sub = _pill("SUBSCRIBE", "🔔", (230, 33, 23, 255), (255, 255, 255, 255))
    gap, m = 30, 30  # m = margin for the drop shadow
    w = like.width + gap + sub.width + 2 * m
    h = max(like.height, sub.height) + 2 * m
    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    body.alpha_composite(like, (m, m))
    body.alpha_composite(sub, (m + like.width + gap, m))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(body.getchannel("A").point(lambda a: int(a * 0.55)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    out = Image.new("RGBA", (w, h + 8), (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, 8))
    out.alpha_composite(body, (0, 0))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    out.save(path)
    return path


def ensure(path: str) -> str:
    return path if os.path.exists(path) else make_default(path)
