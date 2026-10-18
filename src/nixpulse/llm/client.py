"""Explicit Ollama HTTP client with JSON generation and native tool calling.

Supports both single-shot JSON prompt generation and multi-turn chat loops with
native OpenAI/Ollama function calling.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import requests


class OllamaError(RuntimeError):
    """Raised when Ollama cannot provide a valid response."""


class OllamaClient:
    def __init__(
        self,
        host: str = "127.0.0.1:11434",
        model: str = "hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M",
        timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = host.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"http://{self.base_url}"
        self.model = model
        self.timeout = timeout
        self.session = session or requests.Session()

    def generate_json(self, prompt: str, *, system: str = "") -> Mapping[str, Any]:
        """Make one explicit structured-generation request to Ollama."""

        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "format": "json",
            "options": {"num_ctx": 2048},
        }
        try:
            response = self.session.post(
                f"{self.base_url}/api/generate", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        if not isinstance(data, Mapping):
            raise OllamaError("Ollama response must be a JSON object")
        result = data.get("response")
        if not isinstance(result, str):
            raise OllamaError("Ollama response did not contain a string response")
        try:
            parsed = json.loads(result)
        except ValueError as exc:
            raise OllamaError("Ollama response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise OllamaError("Ollama JSON response must be an object")
        return parsed

    def chat_with_tools(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        num_ctx: int = 2048,
    ) -> Mapping[str, Any]:
        """Make a chat completion request to Ollama with optional function tools."""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
            "options": {"num_ctx": num_ctx},
        }
        if tools:
            payload["tools"] = list(tools)

        try:
            response = self.session.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OllamaError(f"Ollama chat request failed: {exc}") from exc

        if not isinstance(data, Mapping):
            raise OllamaError("Ollama chat response must be a JSON object")
        message = data.get("message")
        if not isinstance(message, Mapping):
            raise OllamaError("Ollama chat response missing 'message' object")
        return message
