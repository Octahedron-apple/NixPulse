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
        api_key: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or ""
        self.base_url = host.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"http://{self.base_url}"
        self.model = model
        self.timeout = timeout
        self.session = session or requests.Session()
        self.is_openrouter = "openrouter.ai" in self.base_url or bool(self.api_key and "127.0.0.1" not in self.base_url)

    def generate_json(self, prompt: str, *, system: str = "") -> Mapping[str, Any]:
        """Make one explicit structured-generation request to Ollama or OpenRouter."""
        if self.is_openrouter:
            headers = {"Authorization": f"Bearer {self.api_key}", "HTTP-Referer": "https://github.com/Octahedron-apple/NixPulse"}
            payload = {
                "model": self.model,
                "messages": [
                    *([{"role": "system", "content": system}] if system else []),
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            }
            endpoint = f"{self.base_url}/chat/completions" if "/v1" in self.base_url or "/api/v1" in self.base_url else f"{self.base_url}/api/v1/chat/completions"
            try:
                response = self.session.post(endpoint, json=payload, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
            except Exception as exc:
                raise OllamaError(f"OpenRouter request failed: {exc}") from exc

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
        num_ctx: int = 4096,
    ) -> Mapping[str, Any]:
        """Make a chat completion request to Ollama or OpenRouter with optional function tools."""

        if self.is_openrouter:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/Octahedron-apple/NixPulse",
                "X-Title": "NixPulse",
            }
            # Convert tool messages to OpenAI standard format if needed
            converted_messages = []
            for msg in messages:
                if msg.get("role") == "tool":
                    converted_messages.append({
                        "role": "tool",
                        "content": str(msg.get("content", "")),
                        "tool_call_id": msg.get("tool_call_id", "call_1"),
                    })
                else:
                    converted_messages.append(dict(msg))

            payload = {
                "model": self.model,
                "messages": converted_messages,
            }
            if tools:
                payload["tools"] = list(tools)

            endpoint = f"{self.base_url}/chat/completions" if "/v1" in self.base_url or "/api/v1" in self.base_url else f"{self.base_url}/api/v1/chat/completions"
            try:
                response = self.session.post(endpoint, json=payload, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                msg = data["choices"][0]["message"]
                return {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": msg.get("tool_calls"),
                }
            except Exception as exc:
                raise OllamaError(f"OpenRouter chat request failed: {exc}") from exc

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
