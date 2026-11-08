"""Shrinking images: size, format, metadata, originals."""

import datetime as dt
from pathlib import Path

import pytest
from PIL import Image

from wikismith.config import Files
from wikismith.media import keep_original, shrink, too_large

LIMITS = Files()


def photo(path: Path, size: tuple[int, int] = (4000, 3000), gps: bool = True) -> Path:
    """A noisy image, so it compresses like a photo, with EXIF metadata (camera model and a GPS tag)."""
    image = Image.effect_noise(size, 60).convert("RGB")
    exif = Image.Exif()
    exif[0x0110] = "Test Camera"  # Model
    if gps:
        exif[0x8825] = {2: (37.0, 46.0, 30.0)}  # GPSInfo: latitude
    image.save(path, "JPEG", quality=95, exif=exif)
    return path


def screenshot(path: Path, fmt: str = "PNG") -> Path:
    image = Image.new("RGB", (2400, 1500), "white")
    for x in range(0, 2400, 40):
        image.paste((30, 30, 30), (x, 100, x + 20, 1400))
    image.save(path, fmt)
    return path


def test_photo_is_reduced_and_loses_its_metadata(tmp_path: Path) -> None:
    path = photo(tmp_path / "shed.jpg")
    assert too_large(path, path.stat().st_size, LIMITS)
    result = shrink(path, LIMITS)
    assert result.new == path and result.new_bytes <= 500 * 1024 < result.old_bytes
    with Image.open(path) as image:
        assert max(image.size) == 1600 and image.format == "JPEG"
        assert not image.getexif()  # no camera, no GPS


def test_screenshots_stay_png(tmp_path: Path) -> None:
    result = shrink(screenshot(tmp_path / "settings.png"), LIMITS)
    assert result.new.suffix == ".png"
    with Image.open(result.new) as image:
        assert max(image.size) == 1600


@pytest.mark.parametrize(("name", "fmt", "expected"), [("scan.bmp", "BMP", ".png"), ("scan.tiff", "TIFF", ".png")])
def test_uncompressed_formats_are_always_converted(tmp_path: Path, name: str, fmt: str, expected: str) -> None:
    path = tmp_path / name
    Image.new("RGB", (200, 100), "white").save(path, fmt)  # small, but BMP and TIFF are converted anyway
    assert too_large(path, path.stat().st_size, LIMITS)
    result = shrink(path, LIMITS)
    assert result.new == path.with_suffix(expected) and not path.exists()


def test_heic_becomes_jpeg(tmp_path: Path) -> None:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    path = tmp_path / "phone.heic"
    Image.effect_noise((3000, 2000), 60).convert("RGB").save(path, "HEIF")
    result = shrink(path, LIMITS)
    assert result.new == tmp_path / "phone.jpg" and not path.exists()


def test_small_images_and_gifs_pass(tmp_path: Path) -> None:
    small = tmp_path / "icon.png"
    Image.new("RGB", (64, 64), "red").save(small)
    assert not too_large(small, small.stat().st_size, LIMITS)
    gif = tmp_path / "anim.gif"
    Image.new("P", (3000, 3000)).save(gif)
    assert not too_large(gif, 10 * 1024 * 1024, LIMITS)


def test_other_files_have_their_own_limit(tmp_path: Path) -> None:
    pdf = tmp_path / "manual.pdf"
    assert not too_large(pdf, 900 * 1024, LIMITS)
    assert too_large(pdf, 2 * 1024 * 1024, LIMITS)


def test_originals_are_kept_and_expire(tmp_path: Path) -> None:
    originals = tmp_path / "originals"
    old = originals / (dt.date.today() - dt.timedelta(days=31)).isoformat()
    old.mkdir(parents=True)
    path = photo(tmp_path / "shed.jpg")
    before = path.read_bytes()
    shrink(path, LIMITS, originals)
    [copy] = (originals / dt.date.today().isoformat()).iterdir()
    assert copy.read_bytes() == before
    assert not old.exists()  # older than 30 days: gone
    assert keep_original(path, originals).name == "shed-1.jpg"  # never overwrites a kept original
