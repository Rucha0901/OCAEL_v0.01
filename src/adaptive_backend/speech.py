from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import httpx

from .config import Settings
from .schemas import SpeechTranscript


class SpeechService:
    """Local/private speech adapters.

    STT supports either:
      1) a configured local whisper.cpp binary + model, or
      2) an explicitly configured OpenAI-compatible transcription endpoint.

    No cloud service is selected implicitly.
    """

    MAX_AUDIO_BYTES = 25 * 1024 * 1024

    def __init__(self, settings: Settings):
        self.settings = settings

    def transcribe(self, data: bytes, filename: str = "audio.wav", language: str | None = None) -> SpeechTranscript:
        if not data:
            raise ValueError("Empty audio")
        if len(data) > self.MAX_AUDIO_BYTES:
            raise ValueError("Audio exceeds transcription upload limit")
        if self.settings.whisper_cpp_bin and self.settings.whisper_cpp_model:
            return self._whisper_cpp(data, filename, language)
        if self.settings.stt_url:
            return self._http_stt(data, filename, language)
        raise RuntimeError(
            "No STT backend configured. Configure whisper.cpp locally or ADAPTIVE_STT_URL."
        )

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        if not self.settings.tts_url:
            raise RuntimeError("No TTS endpoint configured")
        if not text.strip():
            raise ValueError("TTS text is empty")
        payload = {
            "model": self.settings.tts_model or "tts",
            "input": text,
            "voice": voice or "default",
        }
        try:
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.post(self.settings.tts_url, json=payload)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            raise RuntimeError(f"TTS request failed: {exc}") from exc

    def _http_stt(self, data: bytes, filename: str, language: str | None) -> SpeechTranscript:
        form = {"model": self.settings.stt_model or "whisper"}
        if language:
            form["language"] = language
        try:
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.post(
                    self.settings.stt_url,
                    data=form,
                    files={"file": (filename, data, "application/octet-stream")},
                )
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"STT request failed: {exc}") from exc
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str):
            raise RuntimeError("STT endpoint returned an unexpected response")
        return SpeechTranscript(
            text=text.strip(), engine="http-stt", language=result.get("language") or language
        )

    def _whisper_cpp(self, data: bytes, filename: str, language: str | None) -> SpeechTranscript:
        binary = Path(self.settings.whisper_cpp_bin or "")
        model = Path(self.settings.whisper_cpp_model or "")
        if not binary.exists() or not model.exists():
            raise RuntimeError("Configured whisper.cpp binary/model path does not exist")
        suffix = Path(filename).suffix or ".wav"
        with tempfile.TemporaryDirectory(prefix="adaptive_stt_") as tmp:
            audio = Path(tmp) / f"input{suffix}"
            output_base = Path(tmp) / "transcript"
            audio.write_bytes(data)
            cmd = [
                str(binary),
                "-m",
                str(model),
                "-f",
                str(audio),
                "-oj",
                "-of",
                str(output_base),
            ]
            if language:
                cmd += ["-l", language]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=max(30, self.settings.request_timeout_seconds),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"whisper.cpp failed: {exc}") from exc
            if proc.returncode != 0:
                raise RuntimeError(f"whisper.cpp failed: {proc.stderr[-1500:]}")
            json_path = output_base.with_suffix(".json")
            if not json_path.exists():
                raise RuntimeError("whisper.cpp did not produce JSON output")
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            transcription = payload.get("transcription", [])
            if isinstance(transcription, list):
                text = "".join(str(item.get("text", "")) for item in transcription if isinstance(item, dict))
            else:
                text = str(payload.get("text", ""))
            return SpeechTranscript(text=text.strip(), engine="whisper.cpp", language=language)
