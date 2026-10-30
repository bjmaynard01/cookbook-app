"""Validated local recipe image storage."""
from __future__ import annotations

import io
import os
import re
import tempfile
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError


_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}
_MANAGED_PATH = re.compile(r"^recipes/[0-9a-f]{32}\.(?:jpg|png|webp)$")
_MAX_PIXELS = 40_000_000


class InvalidRecipeImage(ValueError):
    """The upload is too large or is not a supported, decodable image."""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    extension: str
    media_type: str


@dataclass(frozen=True, slots=True)
class StoredImage:
    relative_path: str
    path: Path
    media_type: str


def validate_image(content: bytes, *, max_bytes: int) -> ImageInfo:
    """Validate actual image bytes and return a type derived from the decoder."""
    if not content:
        raise InvalidRecipeImage("Choose an image to upload.")
    if len(content) > max_bytes:
        raise InvalidRecipeImage(f"Image exceeds the {max_bytes}-byte upload limit.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=tuple(_FORMATS)) as image:
                image_format = image.format
                if image_format not in _FORMATS:
                    raise InvalidRecipeImage("Use a JPEG, PNG, or WebP image.")
                if image.width <= 0 or image.height <= 0 or image.width * image.height > _MAX_PIXELS:
                    raise InvalidRecipeImage("Image dimensions exceed the supported limit.")
                if getattr(image, "n_frames", 1) != 1:
                    raise InvalidRecipeImage("Animated images are not supported.")
                image.load()
    except InvalidRecipeImage:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidRecipeImage("Image dimensions exceed the supported limit.") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidRecipeImage("Upload a valid JPEG, PNG, or WebP image.") from exc

    extension, media_type = _FORMATS[image_format]
    return ImageInfo(extension=extension, media_type=media_type)


def store_image(content: bytes, media_dir: Path, *, max_bytes: int) -> StoredImage:
    """Validate and atomically store original bytes under a generated name."""
    info = validate_image(content, max_bytes=max_bytes)
    root = media_dir.resolve()
    image_dir = root / "recipes"
    image_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = image_dir.resolve()
    if not resolved_dir.is_relative_to(root):
        raise OSError("Recipe image directory escapes the configured media root")

    filename = f"{uuid.uuid4().hex}{info.extension}"
    relative_path = f"recipes/{filename}"
    final_path = resolved_dir / filename
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".upload-", suffix=".tmp", dir=resolved_dir, delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, final_path)
    except OSError:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return StoredImage(relative_path=relative_path, path=final_path, media_type=info.media_type)


def resolve_stored_image(media_dir: Path, relative_path: str) -> StoredImage | None:
    """Resolve only generated recipe image paths contained under media_dir."""
    if not _MANAGED_PATH.fullmatch(relative_path):
        return None
    root = media_dir.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    extension = path.suffix.lower()
    media_type = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[extension]
    return StoredImage(relative_path=relative_path, path=path, media_type=media_type)


def delete_stored_image(media_dir: Path, relative_path: str | None) -> bool:
    """Delete a managed stored image; refuse unsafe or missing paths."""
    if not relative_path:
        return False
    image = resolve_stored_image(media_dir, relative_path)
    if image is None:
        return False
    image.path.unlink()
    return True
