"""Image post-processing, ported from DreamGen-Studio tools/image-gen/common.py.

Every provider's result goes through to_webp(), so every provider gives the same thing:
WebP bytes, optionally scaled to a width (no crop) or cropped and resized to an exact
size. With fit, the uncropped original is returned as well, so it can be re-cropped.
"""
from __future__ import annotations

import io

from PIL import Image

ANCHORS = ('auto', 'top', 'center', 'bottom')


class ImageError(ValueError):
    pass


def parse_size(text: str) -> tuple[int, int]:
    """'WxH' -> (W, H)."""
    try:
        w, h = (int(v) for v in text.lower().split('x'))
    except ValueError:
        raise ImageError(f'Size must look like WxH (for example 800x800), not {text!r}') from None
    if not (16 <= w <= 8192 and 16 <= h <= 8192):
        raise ImageError(f'Size {text!r} is out of range (16..8192 per side)')
    return w, h


def open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # Pillow raises many types
        raise ImageError(f'Not a readable image: {e}') from None
    if img.mode not in ('RGB', 'RGBA'):
        img = img.convert('RGBA' if 'A' in img.getbands() else 'RGB')
    return img


def fit(img: Image.Image, size: tuple[int, int], anchor: str = 'auto') -> Image.Image:
    """Crop img to the aspect ratio of size, then resize to exactly size.

    When the image is too tall, anchor decides what is trimmed: 'top' keeps the top,
    'bottom' the bottom, 'center' trims both evenly. 'auto' is 'top' for a target
    taller than wide and 'center' otherwise. Too-wide images are trimmed evenly.
    """
    if anchor not in ANCHORS:
        raise ImageError(f'Anchor must be one of {", ".join(ANCHORS)}, not {anchor!r}')
    w, h = size
    if anchor == 'auto':
        anchor = 'top' if h > w else 'center'
    target = w / h
    iw, ih = img.size
    if iw / ih > target:
        nw = round(ih * target)
        left = (iw - nw) // 2
        img = img.crop((left, 0, left + nw, ih))
    elif iw / ih < target:
        nh = round(iw / target)
        top = {'top': 0, 'center': (ih - nh) // 2, 'bottom': ih - nh}[anchor]
        img = img.crop((0, top, iw, top + nh))
    return img.resize((w, h), Image.LANCZOS)


def encode_webp(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, 'WEBP', quality=quality, method=6)
    return buf.getvalue()


def to_webp(data: bytes, *, fit_size: str | None = None, width: int | None = None,
            anchor: str = 'auto', quality: int = 85) -> tuple[bytes, bytes | None, tuple[int, int]]:
    """Return (webp, uncropped_original_or_None, (w, h)) for raw image bytes."""
    img = open_image(data)
    full = None
    if fit_size:
        full = encode_webp(img, quality)
        img = fit(img, parse_size(fit_size), anchor)
    elif width:
        if not 16 <= width <= 8192:
            raise ImageError('width must be 16..8192')
        if img.width != width:
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    return encode_webp(img, quality), full, img.size


def preview(webp: bytes, longest: int = 768) -> bytes:
    """A smaller WebP for inline display, so a result does not flood the client's context."""
    img = open_image(webp)
    if max(img.size) > longest:
        img.thumbnail((longest, longest), Image.LANCZOS)
    return encode_webp(img, 80)


def to_png_or_jpeg(data: bytes) -> tuple[bytes, str]:
    """Video models take JPEG or PNG only; anything else is converted to PNG."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return data, 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return data, 'image/jpeg'
    buf = io.BytesIO()
    open_image(data).save(buf, 'PNG')
    return buf.getvalue(), 'image/png'


def sniff_mime(data: bytes) -> str:
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    return 'image/png'
