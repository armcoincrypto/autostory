"""Local Story media compatibility + dry-run blocker tests (no live Telegram)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageOps

from src.stories.rotation_audit import media_precheck
from src.stories.story_media_compat import (
    evaluate_story_media_compat,
    normalize_story_image,
    synthesize_story_canary_jpeg,
)


def _save_jpeg(path: Path, size: tuple[int, int], *, mode: str = "RGB", progressive: bool = False, **save_kw) -> None:
    if mode == "CMYK":
        im = Image.new("CMYK", size, (0, 50, 50, 0))
    else:
        im = Image.new(mode if mode != "RGBA" else "RGBA", size, (40, 80, 120, 255) if mode == "RGBA" else (40, 80, 120))
        if mode == "RGB" and im.mode != "RGB":
            im = im.convert("RGB")
    im.save(path, format="JPEG", quality=85, progressive=progressive, **save_kw)


def test_missing_file(tmp_path: Path) -> None:
    r = evaluate_story_media_compat(tmp_path / "missing.jpg")
    assert r["ok"] is False
    assert r["blocker"] == "story_media_decode_failed"


def test_wrong_extension_non_image(tmp_path: Path) -> None:
    p = tmp_path / "not_image.jpg"
    p.write_bytes(b"this is not an image at all")
    r = evaluate_story_media_compat(p)
    assert r["ok"] is False
    assert r["blocker"] == "story_media_decode_failed"
    assert r["media_decodes"] is False


def test_landscape_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "landscape.jpg"
    _save_jpeg(p, (1920, 1080))
    r = evaluate_story_media_compat(p)
    assert r["media_decodes"] is True
    assert r["media_vertical"] is False
    assert r["ok"] is False
    assert r["blocker"] == "story_media_not_vertical"


def test_square_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "square.jpg"
    _save_jpeg(p, (1080, 1080))
    r = evaluate_story_media_compat(p)
    assert r["blocker"] == "story_media_not_vertical"


def test_portrait_jpeg_accepted(tmp_path: Path) -> None:
    p = tmp_path / "portrait.jpg"
    _save_jpeg(p, (720, 1280))
    r = evaluate_story_media_compat(p)
    assert r["ok"] is True
    assert r["media_vertical"] is True
    assert r["media_story_compatible"] is True


def test_exif_rotated_jpeg(tmp_path: Path) -> None:
    # Landscape pixels with EXIF orientation 6 (rotate 90 CW) — still landscape by pixel dims.
    p = tmp_path / "exif.jpg"
    im = Image.new("RGB", (1920, 1080), (10, 20, 30))
    exif = im.getexif()
    exif[274] = 6
    im.save(p, format="JPEG", quality=85, exif=exif)
    r = evaluate_story_media_compat(p)
    assert r["media_decodes"] is True
    assert r["media_exif_orientation"] == 6
    # Pixel dimensions are landscape → not vertical until normalized.
    assert r["blocker"] == "story_media_not_vertical"


def test_cmyk_jpeg_requires_normalization(tmp_path: Path) -> None:
    p = tmp_path / "cmyk.jpg"
    _save_jpeg(p, (720, 1280), mode="CMYK")
    r = evaluate_story_media_compat(p)
    assert r["ok"] is False
    assert r["blocker"] == "story_media_normalization_required"
    assert r["media_normalization_required"] is True


def test_progressive_vertical_still_accepted(tmp_path: Path) -> None:
    p = tmp_path / "progressive.jpg"
    _save_jpeg(p, (720, 1280), progressive=True)
    r = evaluate_story_media_compat(p)
    assert r["ok"] is True
    assert r["media_progressive"] is True


def test_corrupt_truncated_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "trunc.jpg"
    # SOI + junk, no valid SOF
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    r = evaluate_story_media_compat(p)
    assert r["ok"] is False
    assert r["blocker"] == "story_media_decode_failed"


def test_normalized_1080x1920(tmp_path: Path) -> None:
    src = tmp_path / "src_landscape.jpg"
    out = tmp_path / "out_story.jpg"
    _save_jpeg(src, (1600, 900))
    report = normalize_story_image(src, out)
    assert src.exists()
    assert out.exists()
    assert src.read_bytes() != out.read_bytes()
    assert report["output_width"] == 1080
    assert report["output_height"] == 1920
    v = evaluate_story_media_compat(out)
    assert v["ok"] is True
    assert v["media_format"] == "JPEG"
    assert v["media_color_mode"] == "RGB"
    assert v["media_vertical"] is True


def test_normalize_refuses_overwrite(tmp_path: Path) -> None:
    p = tmp_path / "same.jpg"
    _save_jpeg(p, (720, 1280))
    with pytest.raises(ValueError):
        normalize_story_image(p, p)


def test_media_precheck_blocks_undecodable(tmp_path: Path) -> None:
    p = tmp_path / "bad.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\xcc\xee" + b"\x00" * 200)
    r = media_precheck(str(p))
    assert r["ok"] is False
    assert r["compat_blocker"] == "story_media_decode_failed"
    assert r["media_decodes"] is False


def test_media_precheck_accepts_vertical(tmp_path: Path) -> None:
    p = tmp_path / "ok.jpg"
    _save_jpeg(p, (1080, 1920))
    r = media_precheck(str(p))
    assert r["ok"] is True
    assert r["media_story_compatible"] is True
    assert r["media_width"] == 1080
    assert r["media_height"] == 1920
    assert "Telegram may still reject" in (r["message"] or "")


def test_synthesize_canary_does_not_touch_source(tmp_path: Path) -> None:
    evil = tmp_path / "big15d.jpg"
    evil.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    before = evil.read_bytes()
    out = tmp_path / "canary_140_story_1080x1920.jpg"
    report = synthesize_story_canary_jpeg(out)
    assert evil.read_bytes() == before
    assert out.exists()
    assert report["decoder_verification"]["ok"] is True
