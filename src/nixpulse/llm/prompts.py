"""Small JSON schemas and prompt builders for the optional Gemma adapter."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from nixpulse.manifest import ManifestError, resolve_package, resolve_packages


INTENT_SYSTEM_PROMPT = """You are NixPulse's planning assistant. Return JSON only.
Suggest generic package tokens, never Nix expressions or arbitrary attributes.
Allowed package tokens: btop, gcc, go, gnumake, hey, htop, ollama, pkg-config,
sqlite, tmux, zlib. Use an empty start_command unless the user explicitly asks
for one. Schema: {"packages":["go"],"layout":"main-horizontal",
"start_command":"clear"}."""

INCIDENT_SYSTEM_PROMPT = """You are NixPulse's local incident triage assistant. Return
JSON only. Suggest one generic package token from this allow-list: btop, gcc, go,
gnumake, hey, htop, ollama, pkg-config, sqlite, tmux, zlib. Never return a raw
Nix expression. Schema: {"missing_library":"zlib","suggested_package":"zlib",
"explanation":"..."}."""


@dataclass(frozen=True)
class IntentPlan:
    packages: tuple[str, ...]
    layout: str = "main-horizontal"
    start_command: str = "clear"

    def as_dict(self) -> dict[str, Any]:
        return {
            "packages": list(self.packages),
            "layout": self.layout,
            "start_command": self.start_command,
        }


@dataclass(frozen=True)
class IncidentDiagnosis:
    missing_library: str
    suggested_package: str
    explanation: str

    def as_dict(self) -> dict[str, str]:
        return {
            "missing_library": self.missing_library,
            "suggested_package": self.suggested_package,
            "explanation": self.explanation,
        }


def _object(value: str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Ollama returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Ollama JSON response must be an object")
    return parsed


def parse_intent(value: str | Mapping[str, Any]) -> IntentPlan:
    """Validate model output and resolve all package names deterministically."""

    payload = _object(value)
    packages = payload.get("packages")
    if not isinstance(packages, list) or not all(isinstance(item, str) for item in packages):
        raise ValueError("intent JSON must contain a string array named packages")
    try:
        resolved = resolve_packages(packages)
    except ManifestError as exc:
        raise ValueError(str(exc)) from exc
    layout = payload.get("layout", "main-horizontal")
    start_command = payload.get("start_command", "clear")
    if not isinstance(layout, str) or not isinstance(start_command, str):
        raise ValueError("layout and start_command must be strings")
    if layout not in {"main-horizontal", "even-horizontal", "tiled"}:
        raise ValueError(f"unsupported layout: {layout}")
    if "\n" in start_command or "\r" in start_command:
        raise ValueError("start_command cannot contain a newline")
    return IntentPlan(resolved, layout, start_command)


def parse_incident(value: str | Mapping[str, Any]) -> IncidentDiagnosis:
    payload = _object(value)
    suggested = payload.get("suggested_package")
    missing = payload.get("missing_library", suggested)
    explanation = payload.get("explanation", "")
    if not all(isinstance(item, str) for item in (suggested, missing, explanation)):
        raise ValueError("incident JSON contains invalid field types")
    try:
        canonical = resolve_package(suggested)
    except ManifestError as exc:
        raise ValueError(str(exc)) from exc
    return IncidentDiagnosis(missing, canonical, explanation)


def build_intent_prompt(intent: str) -> str:
    return f"{INTENT_SYSTEM_PROMPT}\n\nDeveloper intent:\n{intent.strip()}"


def build_incident_prompt(stderr: str) -> str:
    return f"{INCIDENT_SYSTEM_PROMPT}\n\nSanitized failure output:\n{stderr.strip()}"
