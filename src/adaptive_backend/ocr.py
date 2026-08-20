from __future__ import annotations

import io
import re

from PIL import Image, UnidentifiedImageError

from .schemas import OCRResult


class OCRService:
    """Local printed-text OCR adapter with pre-decode safety checks."""

    MAX_BYTES = 12 * 1024 * 1024
    MAX_PIXELS = 24_000_000
    LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_+-]{1,32}$")

    def extract(self, data: bytes, language: str = "eng") -> OCRResult:
        if not data:
            raise ValueError("Empty image")
        if len(data) > self.MAX_BYTES:
            raise ValueError("Image exceeds OCR upload limit")
        if not self.LANGUAGE_RE.fullmatch(language):
            raise ValueError("Invalid OCR language identifier")
        try:
            with Image.open(io.BytesIO(data)) as image:
                # Check header dimensions before decompression. Pillow's own bomb
                # protection remains enabled as a second line of defense.
                if image.width <= 0 or image.height <= 0 or image.width * image.height > self.MAX_PIXELS:
                    raise ValueError("Image dimensions exceed OCR safety limit")
                image.load()
                normalized = image.convert("RGB").copy()
        except Image.DecompressionBombError as exc:
            raise ValueError("Image dimensions exceed OCR safety limit") from exc
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("Unsupported or corrupted image") from exc

        try:
            import pytesseract
        except ImportError as exc:
            raise RuntimeError(
                "Local OCR backend is not installed. Install the project with the 'ocr' extra."
            ) from exc

        try:
            text = pytesseract.image_to_string(normalized, lang=language).strip()
        except Exception as exc:
            raise RuntimeError(f"Local OCR failed: {exc}") from exc
        return OCRResult(text=text, engine="pytesseract", confidence=None)
