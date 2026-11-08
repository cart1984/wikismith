"""Images and large files: what's too big to commit, and shrinking images to what's actually needed.

Agents read images at about 1,600 px at most, and that also fills a laptop screen in Obsidian. So images are reduced
to `files.image_max_px` and `files.image_max_kb`, with their metadata (camera, GPS location) removed. BMP, TIFF and
HEIC are always converted: the first two are uncompressed, and HEIC doesn't display everywhere. Images with few
colors or transparency (screenshots, diagrams) become PNG, so text stays sharp; photos become JPEG.

HEIC needs pillow-heif, the `heic` extra (`uv tool install 'wikismith[heic]'`); everything else works out of the box.
"""

from __future__ import annotations

import datetime as dt
import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wikismith.config import Files

IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
CONVERT = {".bmp", ".tif", ".tiff", ".heic", ".heif"}  # always converted, whatever their size
KEEP = {".gif"}  # may be animated: left alone
KEEP_ORIGINALS_DAYS = 30
HEIC_INSTALL = (
    "HEIC images need pillow-heif: install wikismith with its heic extra (uv tool install 'wikismith[heic]')."
)


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Shrunk:
    old: Path
    new: Path
    old_bytes: int
    new_bytes: int

    def describe(self, root: Path) -> str:
        return f"{_rel(self.old, root)} {_size(self.old_bytes)} → {_rel(self.new, root)} {_size(self.new_bytes)}"


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGES


def too_large(path: Path, size: int, limits: Files) -> bool:
    """Whether a file needs attention before it's committed: an image to shrink, or another file over the limit."""
    suffix = path.suffix.lower()
    if not is_image(path):
        return size > limits.max_kb * 1024
    if suffix in KEEP:
        return False
    if suffix in CONVERT or size > limits.image_max_kb * 1024:
        return True
    try:
        return max(_open(path).size) > limits.image_max_px
    except MediaError:
        return False  # without Pillow, the size alone decides


def shrink(path: Path, limits: Files, originals: Path | None = None) -> Shrunk:
    """Shrink an image in place (its suffix may change). The original is kept in `originals`, if given."""
    old_bytes = path.stat().st_size
    image = _prepare(_open(path), limits.image_max_px)
    data, suffix = _encode(image, path.suffix.lower(), limits.image_max_kb * 1024)
    target = path.with_suffix(suffix)
    if target != path and target.exists():
        target = _free_name(target)
    if originals is not None:
        keep_original(path, originals)
    target.write_bytes(data)
    if target != path:
        path.unlink()
    return Shrunk(path, target, old_bytes, len(data))


def keep_original(path: Path, originals: Path) -> Path:
    """Copy a file into today's folder of originals, and forget originals older than 30 days."""
    today = dt.date.today()
    folder = originals / today.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    copy = _free_name(folder / path.name) if (folder / path.name).exists() else folder / path.name
    shutil.copy2(path, copy)
    for old in originals.iterdir():
        try:
            day = dt.date.fromisoformat(old.name)
        except ValueError:
            continue
        if (today - day).days > KEEP_ORIGINALS_DAYS:
            shutil.rmtree(old, ignore_errors=True)
    return copy


# -- Pillow ---------------------------------------------------------------------------


def _open(path: Path) -> Any:
    from PIL import Image

    if path.suffix.lower() in (".heic", ".heif"):
        try:
            from pillow_heif import register_heif_opener
        except ImportError as e:
            raise MediaError(HEIC_INSTALL) from e
        register_heif_opener()
    try:
        image = Image.open(path)
        image.load()
    except OSError as e:
        raise MediaError(f"{path.name}: not a readable image ({e})") from e
    return image


def _prepare(image: Any, max_px: int) -> Any:
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)  # keep the orientation the metadata gave it, before dropping the metadata
    image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    return image


def _encode(image: Any, suffix: str, max_bytes: int) -> tuple[bytes, str]:
    """Encode without metadata: PNG for screenshots and anything transparent, else JPEG (WebP stays WebP)."""
    transparent = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    few_colors = image.getcolors(maxcolors=4096) is not None
    if suffix == ".webp":
        return _save(image, "WEBP", quality=80), ".webp"
    if transparent or few_colors:
        png = _save(image if transparent else image.convert("RGB"), "PNG", optimize=True)
        if len(png) <= max_bytes or transparent:
            return png, ".png"
    rgb = image.convert("RGB")
    data = b""
    for quality in (82, 72, 62):
        data = _save(rgb, "JPEG", quality=quality, optimize=True, progressive=True)
        if len(data) <= max_bytes:
            break
    return data, ".jpg"


def _save(image: Any, fmt: str, **options: Any) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **options)  # no `exif=` or `pnginfo=`: metadata isn't carried over
    return out.getvalue()


# -- helpers ----------------------------------------------------------------------------


def _free_name(path: Path) -> Path:
    n = 1
    while (candidate := path.with_name(f"{path.stem}-{n}{path.suffix}")).exists():
        n += 1
    return candidate


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
