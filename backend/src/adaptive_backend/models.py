from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from threading import Lock

import httpx

from .config import Settings


class ModelUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class ModelReply:
    text: str
    model: str
    provider: str
    raw: dict[str, Any] | None = None
    latency_ms: int | None = None


ModelRoute = Literal["teaching", "generation", "validation", "summarization", "planning", "rerank"]


class ModelGateway:
    """Central provider gateway for OVAEL.

    The learning/agent layers never speak directly to providers.  The hackathon
    default can be Xiaomi MiMo via an OpenAI-compatible endpoint, while the old
    tutor/local/supervisor configuration remains supported for compatibility.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.timeout = httpx.Timeout(settings.request_timeout_seconds)
        self._circuit_lock = Lock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _circuit_check(self) -> None:
        with self._circuit_lock:
            if self._circuit_open_until > time.monotonic():
                raise ModelUnavailable("Model provider circuit is temporarily open")
            if self._circuit_open_until:
                self._circuit_open_until = 0.0
                self._consecutive_failures = 0

    def _circuit_success(self) -> None:
        with self._circuit_lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

    def _circuit_failure(self) -> None:
        with self._circuit_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 5:
                self._circuit_open_until = time.monotonic() + 30.0

    @property
    def mimo_available(self) -> bool:
        return bool(self.settings.mimo_base_url and self.settings.mimo_model and self.settings.mimo_api_key)

    @property
    def tutor_available(self) -> bool:
        return self.mimo_available or bool(self.settings.tutor_url and self.settings.tutor_model)

    @property
    def local_available(self) -> bool:
        return bool(self.settings.local_model_url and self.settings.local_model_name)

    @property
    def supervisor_available(self) -> bool:
        return bool(self.settings.supervisor_url and self.settings.supervisor_model)

    def route_chat(
        self,
        route: ModelRoute,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 900,
    ) -> ModelReply:
        # MiMo is the release default when configured.  A distinct supervisor may
        # still be used for validation/planning if explicitly provided.
        if route in {"validation", "planning"} and self.supervisor_available:
            return self.supervisor_chat(
                system=system, user=user, temperature=0.0, max_tokens=max_tokens
            )
        if self.mimo_available:
            return self._chat(
                base_url=self.settings.mimo_base_url or "",
                api_key=self.settings.mimo_api_key,
                model=self.settings.mimo_model or "",
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=max_tokens,
                provider="xiaomi_mimo",
                api_key_header=True,
            )
        return self.tutor_chat(
            system=system, user=user, temperature=temperature, max_tokens=max_tokens
        )

    def route_json(
        self,
        route: ModelRoute,
        *,
        system: str,
        payload: dict[str, Any],
        max_tokens: int = 1200,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        reply = self.route_chat(
            route,
            system=system,
            user=json.dumps(payload, ensure_ascii=False),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        parsed = self._extract_json(reply.text)
        if not isinstance(parsed, dict):
            raise ModelUnavailable(f"{route} model did not return a JSON object")
        return parsed

    def tutor_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 700,
    ) -> ModelReply:
        if self.mimo_available:
            return self.route_chat(
                "teaching", system=system, user=user, temperature=temperature, max_tokens=max_tokens
            )
        if not (self.settings.tutor_url and self.settings.tutor_model):
            raise ModelUnavailable("Tutor API endpoint is not configured")
        return self._chat(
            base_url=self.settings.tutor_url,
            api_key=self.settings.tutor_api_key,
            model=self.settings.tutor_model,
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
            provider="tutor_api",
        )

    def local_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 700,
    ) -> ModelReply:
        if not self.local_available:
            raise ModelUnavailable("Local model endpoint is not configured")
        return self._chat(
            base_url=self.settings.local_model_url or "",
            api_key=self.settings.local_model_api_key,
            model=self.settings.local_model_name,
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
            provider="local",
        )

    def supervisor_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 900,
    ) -> ModelReply:
        if not self.supervisor_available:
            raise ModelUnavailable("Supervisor endpoint is not configured")
        return self._chat(
            base_url=self.settings.supervisor_url or "",
            api_key=self.settings.supervisor_api_key,
            model=self.settings.supervisor_model or "",
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
            provider="supervisor",
        )

    def local_json(
        self, *, system: str, payload: dict[str, Any], max_tokens: int = 900, temperature: float = 0.1
    ) -> dict[str, Any]:
        """Structured local-model call used only as a bounded fallback tier."""
        reply = self.local_chat(
            system=system,
            user=json.dumps(payload, ensure_ascii=False),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        parsed = self._extract_json(reply.text)
        if not isinstance(parsed, dict):
            raise ModelUnavailable("Local model did not return a JSON object")
        return parsed

    def supervisor_json(
        self, *, system: str, payload: dict[str, Any], max_tokens: int = 900
    ) -> dict[str, Any]:
        reply = self.supervisor_chat(
            system=system,
            user=json.dumps(payload, ensure_ascii=False),
            temperature=0.0,
            max_tokens=max_tokens,
        )
        parsed = self._extract_json(reply.text)
        if not isinstance(parsed, dict):
            raise ModelUnavailable("Supervisor did not return a JSON object")
        return parsed

    def _chat(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        provider: str,
        api_key_header: bool = False,
    ) -> ModelReply:
        self._circuit_check()
        endpoint = self._endpoint(base_url, "chat/completions")
        headers = {"Content-Type": "application/json"}
        if api_key:
            if api_key_header:
                headers["api-key"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": max(0.0, min(2.0, temperature)),
            "max_completion_tokens": max_tokens,
        }
        if provider != "xiaomi_mimo":
            body["max_tokens"] = body.pop("max_completion_tokens")

        started = time.perf_counter()
        retryable = {408, 429, 500, 502, 503, 504}
        last_exc: Exception | None = None
        try:
            with httpx.Client(timeout=self.timeout) as client:
                for attempt in range(3):
                    try:
                        response = client.post(endpoint, headers=headers, json=body)
                        if response.status_code in retryable:
                            if attempt < 2:
                                retry_after = response.headers.get("retry-after")
                                try:
                                    wait = min(2.0, max(0.0, float(retry_after))) if retry_after else 0.15 * (2**attempt) + random.random() * 0.05
                                except ValueError:
                                    wait = 0.15 * (2**attempt) + random.random() * 0.05
                                time.sleep(wait)
                                continue
                            response.raise_for_status()
                        # Fail fast on non-retryable client/configuration errors.
                        response.raise_for_status()
                        data = response.json()
                        break
                    except httpx.HTTPStatusError as exc:
                        last_exc = exc
                        status = exc.response.status_code
                        if status not in retryable or attempt >= 2:
                            raise ModelUnavailable(f"{provider} model request failed with HTTP {status}") from exc
                    except (httpx.TransportError, ValueError) as exc:
                        last_exc = exc
                        if attempt >= 2:
                            raise ModelUnavailable(f"{provider} model call failed") from exc
                        time.sleep(0.15 * (2**attempt) + random.random() * 0.05)
                else:  # pragma: no cover
                    raise ModelUnavailable(f"{provider} model call failed") from last_exc
        except ModelUnavailable:
            self._circuit_failure()
            raise

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            self._circuit_failure()
            raise ModelUnavailable(f"{provider} model returned an unexpected response shape") from exc
        if not isinstance(text, str) or not text.strip():
            self._circuit_failure()
            raise ModelUnavailable(f"{provider} model returned an empty response")
        self._circuit_success()
        latency = int((time.perf_counter() - started) * 1000)
        return ModelReply(text=text.strip(), model=model, provider=provider, raw=data, latency_ms=latency)

    @staticmethod
    def _endpoint(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/api/v1") or base.endswith("/v1"):
            return f"{base}/{path}"
        return f"{base}/v1/{path}"

    @staticmethod
    def _extract_json(text: str) -> Any:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
            stripped = re.sub(r"\s*```$", "", stripped)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start >= 0 and end > start:
                return json.loads(stripped[start : end + 1])
            raise
