from __future__ import annotations

import base64
import json
import mimetypes
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

    A cloud provider is used only when its credential is explicitly configured;
    otherwise speech remains unavailable or uses the configured local adapter.
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
        if self.settings.mimo_base_url and self.settings.mimo_api_key:
            return self._mimo_stt(data, filename, language)
        raise RuntimeError(
            "No STT backend configured. Configure whisper.cpp, ADAPTIVE_STT_URL, or OVAEL_MIMO_API_KEY."
        )

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0) -> bytes:
        if not text.strip():
            raise ValueError("TTS text is empty")
        speed = max(0.75, min(3.0, float(speed)))
        if self.settings.tts_url:
            payload = {
                "model": self.settings.tts_model or "tts",
                "input": text,
                "voice": voice or "default",
                "speed": speed,
            }
            try:
                with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                    response = client.post(self.settings.tts_url, json=payload)
                    response.raise_for_status()
                    return response.content
            except httpx.HTTPError as exc:
                raise RuntimeError(f"TTS request failed: {exc}") from exc
        if self.settings.mimo_base_url and self.settings.mimo_api_key:
            return self._mimo_tts(text, voice=voice, speed=speed)
        raise RuntimeError("No TTS endpoint configured. Configure ADAPTIVE_TTS_URL or OVAEL_MIMO_API_KEY.")

    def _mimo_endpoint(self) -> str:
        base = (self.settings.mimo_base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _mimo_headers(self) -> dict[str, str]:
        return {
            "api-key": self.settings.mimo_api_key or "",
            "Content-Type": "application/json",
        }

    def _mimo_stt(self, data: bytes, filename: str, language: str | None) -> SpeechTranscript:
        # MiMo-V2.5-ASR currently documents WAV and MP3 inputs. Python's
        # mimetypes commonly returns audio/x-wav, which the API does not list,
        # so normalize to the documented media types before constructing the
        # data URL.
        suffix = Path(filename).suffix.lower()
        if suffix in {".wav", ".wave"}:
            mime = "audio/wav"
        elif suffix == ".mp3":
            mime = "audio/mpeg"
        else:
            guessed = mimetypes.guess_type(filename)[0]
            if guessed in {"audio/wav", "audio/x-wav"}:
                mime = "audio/wav"
            elif guessed in {"audio/mpeg", "audio/mp3"}:
                mime = "audio/mpeg"
            else:
                raise ValueError("MiMo ASR supports WAV or MP3 audio in this backend")
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "model": self.settings.stt_model or "mimo-v2.5-asr",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:{mime};base64,{encoded}"},
                        }
                    ],
                }
            ],
            "asr_options": {"language": language or "auto"},
        }
        try:
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.post(self._mimo_endpoint(), headers=self._mimo_headers(), json=payload)
                response.raise_for_status()
                result = response.json()
            text = result["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("MiMo ASR request failed or returned an unexpected response") from exc
        if not isinstance(text, str):
            raise RuntimeError("MiMo ASR returned an unexpected transcript")
        return SpeechTranscript(text=text.strip(), engine="mimo-v2.5-asr", language=language or "auto")

    def _mimo_tts(self, text: str, *, voice: str | None, speed: float) -> bytes:
        # Xiaomi's TTS API supports pace/tone control through natural-language
        # instruction plus an audio output descriptor. Playback-rate is also
        # retained in the call state so the client can apply exact local speed.
        payload = {
            "model": self.settings.tts_model or "mimo-v2.5-tts",
            "messages": [
                {
                    "role": "user",
                    "content": f"Speak clearly for a learner at approximately {speed:.2f}x normal pace. Keep the wording unchanged.",
                },
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": "wav", "voice": voice or "Mia"},
            "max_completion_tokens": 8192,
        }
        try:
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.post(self._mimo_endpoint(), headers=self._mimo_headers(), json=payload)
                response.raise_for_status()
                result = response.json()
            audio_data = result["choices"][0]["message"]["audio"]["data"]
            if not isinstance(audio_data, str) or not audio_data:
                raise ValueError("missing audio data")
            return base64.b64decode(audio_data, validate=True)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("MiMo TTS request failed or returned an unexpected response") from exc

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


class VoiceCallService:
    """Turn-based voice teaching session layered on the same TeachingService.

    This is intentionally not a second tutor. Audio is transcribed, sent through
    the same adaptive teaching loop, and the returned TeachingMove is synthesized.
    The transport is utterance/chunk based in this transport; a WebRTC transport can later
    wrap this service without changing learner-state semantics.
    """

    def __init__(self, db, speech: SpeechService, teaching, *, max_call_minutes: int = 90):
        self.db = db
        self.speech = speech
        self.teaching = teaching
        self.max_call_minutes = max_call_minutes

    def start(
        self,
        *,
        user_id: str,
        teaching_session_id: str,
        playback_rate: float = 1.0,
        language: str | None = None,
        voice: str | None = None,
    ) -> dict:
        from datetime import datetime, timezone
        from uuid import uuid4

        row = self.db.fetchone(
            "SELECT status FROM teaching_sessions WHERE teaching_session_id=? AND owner_user_id=?",
            (teaching_session_id, user_id),
        )
        if not row or row["status"] != "active":
            raise ValueError("Voice call requires an active teaching session")
        rate = max(0.75, min(3.0, float(playback_rate)))
        call_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO voice_calls(call_id,owner_user_id,teaching_session_id,status,playback_rate,language,voice,created_at,updated_at)
               VALUES(?,?,?,'active',?,?,?,?,?)""",
            (call_id, user_id, teaching_session_id, rate, language, voice, now, now),
        )
        return {
            "call_id": call_id,
            "teaching_session_id": teaching_session_id,
            "status": "active",
            "playback_rate": rate,
            "language": language,
            "voice": voice,
        }

    def set_playback_rate(self, *, user_id: str, call_id: str, playback_rate: float) -> dict:
        from datetime import datetime, timezone
        rate = max(0.75, min(3.0, float(playback_rate)))
        updated = self.db.execute(
            "UPDATE voice_calls SET playback_rate=?,updated_at=? WHERE call_id=? AND owner_user_id=? AND status='active'",
            (rate, datetime.now(timezone.utc).isoformat(), call_id, user_id),
        )
        if not updated:
            raise ValueError("Unknown or inactive voice call")
        return {"call_id": call_id, "playback_rate": rate}

    def turn(
        self,
        *,
        user_id: str,
        call_id: str,
        audio: bytes,
        filename: str,
        idempotency_key: str,
    ) -> dict:
        import base64
        row = self.db.fetchone(
            "SELECT * FROM voice_calls WHERE call_id=? AND owner_user_id=?",
            (call_id, user_id),
        )
        if not row or row["status"] != "active":
            raise ValueError("Unknown or inactive voice call")
        from datetime import datetime, timezone, timedelta
        created = datetime.fromisoformat(row["created_at"])
        if created + timedelta(minutes=self.max_call_minutes) <= datetime.now(timezone.utc):
            self.end(user_id=user_id, call_id=call_id)
            raise ValueError("Voice call reached the configured maximum duration")
        transcript = self.speech.transcribe(audio, filename=filename, language=row["language"])
        from .teaching import LearningTurnInput
        result = self.teaching.turn(
            user_id=user_id,
            session_id=row["teaching_session_id"],
            turn=LearningTurnInput(
                response=transcript.text, learner_intent=self._infer_intent(transcript.text), input_mode="voice"
            ),
            idempotency_key=idempotency_key,
        )
        text = str((result.get("move") or {}).get("content") or "")
        audio_b64 = None
        audio_error = None
        tts_configured = bool(
            self.speech.settings.tts_url
            or (self.speech.settings.mimo_base_url and self.speech.settings.mimo_api_key)
        )
        if text and tts_configured:
            try:
                rendered = self.speech.synthesize(text, voice=row["voice"], speed=float(row["playback_rate"]))
                audio_b64 = base64.b64encode(rendered).decode("ascii")
            except Exception as exc:
                # Return a bounded operational error; never include credentials or
                # request bodies in the learner-visible response.
                audio_error = type(exc).__name__
        return {
            "call_id": call_id,
            "transcript": transcript.model_dump(mode="json"),
            "teaching": result,
            "audio_base64": audio_b64,
            "audio_error": audio_error,
            "playback_rate": float(row["playback_rate"]),
        }

    @staticmethod
    def _infer_intent(text: str) -> str:
        value = " ".join(text.casefold().split())
        if any(x in value for x in ("i don't understand", "i do not understand", "explain again", "explain this", "not clear")):
            return "need_explanation"
        if any(x in value for x in ("give me an example", "show me an example", "example please")):
            return "example"
        if any(x in value for x in ("repeat that", "say that again", "repeat please")):
            return "repeat"
        if any(x in value for x in ("slow down", "too fast", "slower")):
            return "slow_down"
        if any(x in value for x in ("quiz me", "test me", "give me a question", "let me practice")):
            return "practice"
        if any(x in value for x in ("skip this", "move on", "next topic")):
            return "skip"
        if value.endswith("?") or value.startswith(("why ", "how ", "what ", "can you ", "could you ")):
            return "ask_question"
        return "answer"

    def end(self, *, user_id: str, call_id: str) -> dict:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        updated = self.db.execute(
            "UPDATE voice_calls SET status='ended',ended_at=?,updated_at=? WHERE call_id=? AND owner_user_id=? AND status='active'",
            (now, now, call_id, user_id),
        )
        if not updated:
            raise ValueError("Unknown or inactive voice call")
        return {"call_id": call_id, "status": "ended", "ended_at": now}
