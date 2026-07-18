"""Local Story image compatibility checks and safe normalization (no Telegram calls)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

# Telegram Stories soft local target (not a guarantee of server acceptance).
STORY_TARGET_WIDTH = 1080
STORY_TARGET_HEIGHT = 1920
STORY_MAX_BYTES = 30 * 1024 * 1024
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm"}


def _ratio(w: int, h: int) -> float | None:
    if w <= 0 or h <= 0:
        return None
    return round(w / h, 4)


def inspect_story_image(path: str | Path) -> dict[str, Any]:
    """Decode and describe an image for Story compatibility (read-only)."""
    p = Path(path)
    out: dict[str, Any] = {
        "media_exists": p.exists(),
        "media_decodes": False,
        "media_format": None,
        "media_width": None,
        "media_height": None,
        "media_aspect_ratio": None,
        "media_vertical": False,
        "media_color_mode": None,
        "media_progressive": None,
        "media_exif_orientation": None,
        "media_byte_size": None,
        "media_sha256": None,
        "media_story_compatible": False,
        "media_normalization_required": False,
        "blocker": None,
        "operator_message": None,
        "technical_note": None,
    }
    if not p.exists():
        out["blocker"] = "story_media_decode_failed"
        out["operator_message"] = "Media file not found."
        return out
    out["media_byte_size"] = int(p.stat().st_size)
    try:
        out["media_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        out["blocker"] = "story_media_decode_failed"
        out["operator_message"] = "Media file is not readable."
        return out

    ext = p.suffix.lower()
    if ext in _VIDEO_EXTS:
        # Image Story checks do not apply; leave video to existing validate_media.
        out["media_decodes"] = True
        out["media_format"] = "video"
        out["media_story_compatible"] = True
        out["operator_message"] = (
            "Media passed local Story compatibility checks. "
            "Telegram may still reject media for server-side reasons."
        )
        return out

    if ext not in _IMAGE_EXTS:
        out["blocker"] = "story_media_format_unsupported"
        out["operator_message"] = "This image is not prepared for Telegram Stories. Normalize or replace it before publishing."
        return out

    if out["media_byte_size"] == 0:
        out["blocker"] = "story_media_decode_failed"
        out["operator_message"] = "This image is not prepared for Telegram Stories. Normalize or replace it before publishing."
        return out
    if out["media_byte_size"] > STORY_MAX_BYTES:
        out["blocker"] = "story_media_format_unsupported"
        out["operator_message"] = "This image is not prepared for Telegram Stories. Normalize or replace it before publishing."
        out["technical_note"] = "file_exceeds_30mb_story_limit"
        return out

    try:
        with Image.open(p) as im:
            im.load()
            fmt = (im.format or ext.lstrip(".")).upper()
            w, h = im.size
            mode = im.mode
            progressive = bool(im.info.get("progression") or im.info.get("progressive"))
            orient = None
            try:
                orient = im.getexif().get(274)
            except Exception:
                orient = None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        out["blocker"] = "story_media_decode_failed"
        out["operator_message"] = (
            "This image is not prepared for Telegram Stories. Normalize or replace it before publishing."
        )
        out["technical_note"] = f"{type(exc).__name__}: {exc}"
        return out

    out["media_decodes"] = True
    out["media_format"] = fmt
    out["media_width"] = int(w)
    out["media_height"] = int(h)
    out["media_aspect_ratio"] = _ratio(w, h)
    out["media_vertical"] = bool(h > w)
    out["media_color_mode"] = mode
    out["media_progressive"] = progressive
    out["media_exif_orientation"] = orient

    if w <= 0 or h <= 0:
        out["blocker"] = "story_media_decode_failed"
        out["operator_message"] = (
            "This image is not prepared for Telegram Stories. Normalize or replace it before publishing."
        )
        return out

    # CMYK / exotic modes need re-encode before Telegram photo upload.
    if mode not in {"RGB", "L"}:
        out["media_normalization_required"] = True
        out["blocker"] = "story_media_normalization_required"
        out["operator_message"] = (
            "This image is not prepared for Telegram Stories. Normalize or replace it before publishing."
        )
        out["technical_note"] = f"color_mode={mode}"
        return out

    if not out["media_vertical"]:
        out["media_normalization_required"] = True
        out["blocker"] = "story_media_not_vertical"
        out["operator_message"] = (
            "This image is not prepared for Telegram Stories. Normalize or replace it before publishing."
        )
        return out

    # Clearly vertical RGB/L JPEG/PNG/WebP: accept unchanged (policy A).
    out["media_story_compatible"] = True
    out["operator_message"] = (
        "Media passed local Story compatibility checks. "
        "Telegram may still reject media for server-side reasons."
    )
    if progressive or (orient not in (None, 1)):
        out["technical_note"] = "accepted_vertical; progressive_or_exif_present_but_not_blocking"
    return out


def evaluate_story_media_compat(path: str | Path) -> dict[str, Any]:
    """Return inspection plus ok flag used by dry-run / precheck."""
    info = inspect_story_image(path)
    info["ok"] = bool(info.get("media_story_compatible")) and not info.get("blocker")
    return info


def normalize_story_image(
    source_path: str | Path,
    output_path: str | Path,
    *,
    width: int = STORY_TARGET_WIDTH,
    height: int = STORY_TARGET_HEIGHT,
    quality: int = 90,
) -> dict[str, Any]:
    """
    Create a derived 1080×1920 baseline RGB JPEG.

    Never overwrites the source. Uses contain + letterbox (black bars) so the
    full source image is preserved.
    """
    src = Path(source_path)
    dst = Path(output_path)
    if not src.exists():
        raise FileNotFoundError(f"Source media not found: {src}")
    if src.resolve() == dst.resolve():
        raise ValueError("Refusing to overwrite source media path")

    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in {"RGB", "L"}:
            im = im.convert("RGB")
        elif im.mode == "L":
            im = im.convert("RGB")
        src_w, src_h = im.size
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        scale = min(width / src_w, height / src_h)
        new_w = max(1, int(round(src_w * scale)))
        new_h = max(1, int(round(src_h * scale)))
        resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
        offset = ((width - new_w) // 2, (height - new_h) // 2)
        canvas.paste(resized, offset)
        canvas.save(
            dst,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=False,
            subsampling=2,
        )

    verify = evaluate_story_media_compat(dst)
    return {
        "source_path": str(src),
        "output_path": str(dst),
        "source_width": src_w,
        "source_height": src_h,
        "source_aspect_ratio": _ratio(src_w, src_h),
        "output_width": verify.get("media_width"),
        "output_height": verify.get("media_height"),
        "output_aspect_ratio": verify.get("media_aspect_ratio"),
        "output_format": verify.get("media_format"),
        "output_mode": verify.get("media_color_mode"),
        "output_byte_size": verify.get("media_byte_size"),
        "output_sha256": verify.get("media_sha256"),
        "decoder_verification": verify,
        "composition": "contain_letterbox",
    }


def synthesize_story_canary_jpeg(output_path: str | Path) -> dict[str, Any]:
    """
    Build a fresh 1080×1920 RGB baseline JPEG when the rejected source cannot
    be decoded. Does not touch the corrupt original.
    """
    from PIL import ImageDraw, ImageFont

    dst = Path(output_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (STORY_TARGET_WIDTH, STORY_TARGET_HEIGHT), (18, 28, 48))
    draw = ImageDraw.Draw(img)
    # Soft vertical gradient bands (no external assets).
    for y in range(STORY_TARGET_HEIGHT):
        t = y / max(1, STORY_TARGET_HEIGHT - 1)
        r = int(18 + (42 - 18) * t)
        g = int(28 + (96 - 28) * t)
        b = int(48 + (140 - 48) * t)
        draw.line([(0, y), (STORY_TARGET_WIDTH, y)], fill=(r, g, b))
    draw.rectangle([80, 360, STORY_TARGET_WIDTH - 80, 1560], outline=(230, 236, 245), width=4)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
        font_sm = font
    draw.text((140, 700), "StoryFleet", fill=(245, 248, 252), font=font)
    draw.text((140, 800), "Account 140 canary", fill=(200, 210, 225), font=font_sm)
    draw.text((140, 880), "1080 × 1920 · JPEG · RGB", fill=(160, 175, 195), font=font_sm)
    img.save(dst, format="JPEG", quality=90, optimize=True, progressive=False, subsampling=2)
    return {
        "source_path": None,
        "source_note": "Rejected source not decodable; synthesized Story-compatible JPEG",
        "output_path": str(dst),
        "decoder_verification": evaluate_story_media_compat(dst),
    }
