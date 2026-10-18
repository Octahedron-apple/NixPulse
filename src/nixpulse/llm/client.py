"""Explicit, minimal Ollama HTTP client.

The client is inert until ``generate_json`` is called.  NixPulse's normal
offline demo uses a deterministic adapter and never constructs a request.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import requests


class OllamaError(RuntimeError):
    """Raised when Ollama cannot provide a valid response."""


class OllamaClient:
    def __init__(
        self,
        host: str = "127.0.0.1:11434",
        model: str = "hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M",
        timeout: float = 30.0,
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
