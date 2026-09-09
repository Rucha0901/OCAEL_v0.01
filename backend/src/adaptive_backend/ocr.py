from __future__ import annotations

import base64
import io
import re

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .schemas import OCRResult


class OCRService:
    """Printed-text OCR with local-first and configured MiMo vision adapters."""

    MAX_BYTES = 12 * 1024 * 1024
    MAX_PIXELS = 24_000_000
    LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_+-]{1,32}$")

    def __init__(self, settings: Settings | None = None):
        self.settings = settings

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
                normalized = ImageOps.exif_transpose(image).convert("RGB").copy()
        except Image.DecompressionBombError as exc:
            raise ValueError("Image dimensions exceed OCR safety limit") from exc
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("Unsupported or corrupted image") from exc

        try:
            import pytesseract

            prepared = ImageOps.autocontrast(ImageOps.grayscale(normalized))
            if prepared.width < 1600 and prepared.width * prepared.height * 4 <= self.MAX_PIXELS:
                prepared = prepared.resize((prepared.width * 2, prepared.height * 2), Image.Resampling.LANCZOS)
            text = pytesseract.image_to_string(prepared, lang=language, config="--psm 6").strip()
            details = pytesseract.image_to_data(
                prepared,
                lang=language,
                config="--psm 6",
                output_type=pytesseract.Output.DICT,
            )
            confidences = []
            for raw in details.get("conf", []):
                try:
                    confidence = float(raw)
                except (TypeError, ValueError):
                    continue
                if confidence >= 0:
                    confidences.append(confidence)
            mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
            # A non-empty local transcription can still be unusably noisy. When
            # the configured vision model is available, let it handle low-quality
            # pages rather than indexing corrupted text as learner material.
            mimo_ready = bool(self.settings and self.settings.mimo_base_url and self.settings.mimo_api_key)
            if text and (mean_confidence >= 55.0 or not mimo_ready):
                return OCRResult(text=text, engine="pytesseract", confidence=round(mean_confidence / 100.0, 4) if confidences else None)
        except (ImportError, RuntimeError, OSError):
            # A Python package without the Tesseract executable is not a working
            # local adapter. Fall through to the configured private model route.
            pass

        if self.settings and self.settings.mimo_base_url and self.settings.mimo_api_key:
            return self._mimo_extract(normalized, language)
        raise RuntimeError(
            "OCR is not configured. Install the local OCR extra with Tesseract or configure OVAEL_MIMO_API_KEY."
        )

    def _mimo_extract(self, image: Image.Image, language: str = "eng") -> OCRResult:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        base = (self.settings.mimo_base_url or "").rstrip("/")
        endpoint = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
        payload = {
            "model": self.settings.mimo_model or "mimo-v2.5",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise OCR engine. Transcribe only visible text from the image. "
                        "Correct for page rotation, preserve reading order and useful line breaks, and keep headings, lists, and table rows readable. "
                        "Do not summarize, explain, infer missing words, add Markdown fences, or add any text that is not visible."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"OCR language hint: {language}. Return the transcription only."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                    ],
                },
            ],
            "temperature": 0,
            "max_completion_tokens": 2400,
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(self.settings.request_timeout_seconds)) as client:
                response = client.post(
                    endpoint,
                    headers={"api-key": self.settings.mimo_api_key, "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("MiMo OCR failed or returned an unexpected response") from exc
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("MiMo OCR returned an empty transcription")
        return OCRResult(text=text.strip(), engine="mimo-v2.5-vision-ocr", confidence=None)
