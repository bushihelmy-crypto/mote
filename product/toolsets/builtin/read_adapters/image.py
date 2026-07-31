"""Image normalization for Read media results."""

from __future__ import annotations

import io

from PIL import Image


class ImageProcessingError(Exception):
    """The source image could not be decoded, resized, or encoded."""


def prepare_image(
    raw: bytes,
    detail: str,
    *,
    max_dimension: int,
) -> tuple[bytes, str]:
    """Return bytes to publish and a human-readable transformation note."""
    if detail == "original":
        return raw, "original"
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
            if max(width, height) <= max_dimension:
                return raw, "unchanged"

            image_format = image.format or "PNG"
            save_kwargs = {}
            exif = image.info.get("exif")
            if exif:
                save_kwargs["exif"] = exif
            icc = image.info.get("icc_profile")
            if icc:
                save_kwargs["icc_profile"] = icc

            image.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.BILINEAR,
            )
            output = io.BytesIO()
            image.save(output, format=image_format, **save_kwargs)
            new_width, new_height = image.size
            return (
                output.getvalue(),
                f"resized {width}x{height} -> {new_width}x{new_height}",
            )
    except Exception as exc:  # noqa: BLE001 - normalized at the Tool boundary
        raise ImageProcessingError(str(exc)) from exc


__all__ = ["ImageProcessingError", "prepare_image"]
