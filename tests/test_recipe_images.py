from __future__ import annotations

import io

import pytest
from PIL import Image

from cookbook.recipes.images import (
    InvalidRecipeImage,
    delete_stored_image,
    resolve_stored_image,
    store_image,
    validate_image,
)


def encoded_image(image_format: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color=(35, 90, 140)).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("image_format", "extension", "media_type"),
    [
        ("JPEG", ".jpg", "image/jpeg"),
        ("PNG", ".png", "image/png"),
        ("WEBP", ".webp", "image/webp"),
    ],
)
def test_supported_image_is_verified_and_stored_byte_for_byte(
    tmp_path, image_format: str, extension: str, media_type: str
) -> None:
    content = encoded_image(image_format)

    verified = validate_image(content, max_bytes=1024)
    stored = store_image(content, tmp_path, max_bytes=1024)
    relative_path = stored.relative_path
    resolved = resolve_stored_image(tmp_path, relative_path)

    assert verified.extension == extension
    assert verified.media_type == media_type
    assert relative_path.startswith("recipes/")
    assert relative_path.endswith(extension)
    assert resolved is not None
    assert resolved.media_type == media_type
    assert resolved.path.read_bytes() == content


def test_oversized_corrupt_and_unsupported_images_are_rejected(tmp_path) -> None:
    content = encoded_image("PNG")

    with pytest.raises(InvalidRecipeImage, match="exceeds"):
        validate_image(content, max_bytes=len(content) - 1)
    with pytest.raises(InvalidRecipeImage, match="valid JPEG"):
        validate_image(b"\x89PNG\r\n\x1a\nnot really a png", max_bytes=1024)
    with pytest.raises(InvalidRecipeImage, match="JPEG, PNG, or WebP"):
        validate_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", max_bytes=1024)
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "../secret.png",
        "recipes/../../secret.png",
        "recipes/not-a-uuid.png",
        "recipes/0123456789abcdef0123456789abcdef.svg",
    ],
)
def test_stored_path_resolution_rejects_unsafe_paths(tmp_path, relative_path: str) -> None:
    assert resolve_stored_image(tmp_path, relative_path) is None
    assert delete_stored_image(tmp_path, relative_path) is False


def test_delete_stored_image_only_removes_a_managed_image(tmp_path) -> None:
    content = encoded_image("PNG")
    relative_path = store_image(content, tmp_path, max_bytes=1024).relative_path
    resolved = resolve_stored_image(tmp_path, relative_path)
    assert resolved is not None and resolved.path.exists()

    assert delete_stored_image(tmp_path, relative_path) is True
    assert resolve_stored_image(tmp_path, relative_path) is None
