"""LLM providers for the optional AI analyst.

Two backends behind one interface:

  * OllamaProvider -- a model running locally (llama3.1, mistral, whatever you
    have pulled). Nothing leaves your machine.
  * GeminiProvider -- Google's API. Your prompt, including the market data
    summary, is sent to Google.

Both are optional. With neither configured the analyst is simply unavailable
and the rest of the app behaves exactly as before.

IMPORTANT: nothing in this package may place, size, or approve an order. The
analyst reads a summary the app has already computed and writes prose about it.
Keeping the language model out of the execution path is deliberate -- a
hallucinated number that reaches a broker is not a recoverable error.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 120.0


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    text: str
    model: str
    provider: str


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    def complete(self, system: str, user: str) -> LLMReply: ...

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(usable, human-readable status) — shown on the Settings page."""


class OllamaProvider(LLMProvider):
    """Local model over Ollama's HTTP API. Nothing leaves the machine."""

    name = "ollama"

    def __init__(self, base_url: str = "", model: str = "") -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model

    def available(self) -> tuple[bool, str]:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            r.raise_for_status()
            tags = [m.get("name", "") for m in r.json().get("models", [])]
        except Exception as exc:
            return False, f"Ollama not reachable at {self.base_url} ({type(exc).__name__})"

        if not tags:
            return False, "Ollama is running but has no models pulled"
        # Ollama reports "llama3.1:8b"; accept a bare "llama3.1" as a match.
        if not any(t == self.model or t.split(":")[0] == self.model.split(":")[0]
                   for t in tags):
            return False, f"Model {self.model!r} not pulled. Available: {', '.join(tags[:6])}"
        return True, f"Ollama ready — {self.model}"

    def complete(self, system: str, user: str) -> LLMReply:
        try:
            r = httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "options": {"temperature": 0.3},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            text = r.json().get("message", {}).get("content", "").strip()
        except Exception as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc

        if not text:
            raise LLMError("Ollama returned an empty response")
        return LLMReply(text=text, model=self.model, provider=self.name)


class GeminiProvider(LLMProvider):
    """Google Generative Language API.

    Note that using this sends your market-data summary to Google. The local
    Ollama provider is the private option.
    """

    name = "gemini"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str = "", model: str = "") -> None:
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "No GEMINI_API_KEY set"
        return True, f"Gemini configured — {self.model}"

    def complete(self, system: str, user: str) -> LLMReply:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")

        url = f"{self.ENDPOINT}/{self.model}:generateContent"
        try:
            r = httpx.post(
                url,
                # The key goes in a header, never the URL, so it cannot leak
                # into logs or proxy access records.
                headers={"x-goog-api-key": self.api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1400},
                },
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 400:
                raise LLMError(f"Gemini rejected the request: {r.text[:300]}")
            if r.status_code in (401, 403):
                raise LLMError("Gemini rejected the API key (401/403)")
            if r.status_code == 404:
                raise LLMError(
                    f"Model {self.model!r} not found. Set GEMINI_MODEL to one your "
                    "key can access (e.g. gemini-2.0-flash)."
                )
            r.raise_for_status()
            data = r.json()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"Gemini request failed: {exc}") from exc

        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError):
            blocked = data.get("promptFeedback", {}).get("blockReason")
            raise LLMError(
                f"Gemini returned no usable text{f' (blocked: {blocked})' if blocked else ''}"
            )

        if not text:
            raise LLMError("Gemini returned an empty response")
        return LLMReply(text=text, model=self.model, provider=self.name)


def get_provider(name: str = "") -> LLMProvider | None:
    """Resolve the configured provider, or None when the analyst is disabled."""
    choice = (name or settings.ai_provider or "none").lower()
    if choice in ("none", "off", ""):
        return None
    if choice == "ollama":
        return OllamaProvider()
    if choice == "gemini":
        return GeminiProvider()
    log.warning("unknown AI provider %r; analyst disabled", choice)
    return None


def provider_status() -> dict:
    p = get_provider()
    if p is None:
        return {
            "enabled": False, "name": "none", "model": "",
            "status": "AI analyst is off (set AI_PROVIDER to ollama or gemini)",
            "ok": False, "is_local": False,
        }
    ok, status = p.available()
    return {
        "enabled": True, "name": p.name, "model": p.model,
        "status": status, "ok": ok, "is_local": p.name == "ollama",
    }
