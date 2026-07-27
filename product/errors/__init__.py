"""Product-specific failures."""

from mote.product.errors.media import MediaGenerationError, PermanentMediaGenerationError, classify_media_failure

__all__ = ["MediaGenerationError", "PermanentMediaGenerationError", "classify_media_failure"]
