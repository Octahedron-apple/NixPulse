"""NixPulse orchestration core.

This module contains the glue between validated manifest state, incident
signatures, and the optional adapters.  It is intentionally usable offline so
the demo can exercise the complete control flow without Ollama, Nix, or tmux.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from nixpulse.config import Settings, settings as default_settings
from nixpulse.llm.client import OllamaClient
from nixpulse.llm.prompts import (
    IncidentDiagnosis,
    IntentPlan,
    INCIDENT_SYSTEM_PROMPT,
    INTENT_SYSTEM_PROMPT,
    parse_incident,
    parse_intent,
)
from nixpulse.manifest import ManifestError, WorkspaceManifest, write_shell_nix
from nixpulse.tmux.watcher import Incident, detect_incident, truncate_output


@dataclass(frozen=True)
class Remediation:
    incident: Incident
    diagnosis: IncidentDiagnosis


def offline_intent_plan(intent: str) -> IntentPlan:
    """Produce a predictable plan for demos and disconnected development."""

    text = intent.lower()
    packages = ["gcc"] if any(word in text for word in ("c ", "compile", "header")) else []
    for phrase, package in (
        (("go", "golang", "web service"), "go"),
        (("sqlite", "database"), "sqlite"),
        (("load", "benchmark", "traffic"), "hey"),
        (("htop", "monitor", "telemetry"), "htop"),
        (("zlib", "compression"), "zlib"),
    ):
        if any(word in text for word in phrase):
            packages.append(package)
    if not packages:
        packages = ["go", "sqlite", "hey", "htop"]
    return IntentPlan(tuple(dict.fromkeys(packages)))


def offline_diagnosis(incident: Incident) -> IncidentDiagnosis:
    """Resolve common signatures without asking a model."""

    package = incident.suggested_package
    if incident.kind == "missing-header":
        header = re.search(r"([\w.+-]+)\.h", incident.evidence)
        package = package or {"zlib": "zlib"}.get(header.group(1).removesuffix(".h") if header else "")
    elif incident.kind == "linker-error":
        package = package or "zlib" if "-lz" in incident.evidence else package
    if incident.kind == "port-collision":
        package = ""
    else:
        package = package or "gcc"
    header_name = re.search(r"([\w.+-]+\.h)", incident.evidence)
    explanation = {
        "missing-header": (
            f"The compiler cannot find the required header {header_name.group(1)}."
            if header_name
            else "The compiler reported a missing C/C++ header."
        ),
        "missing-command": "The command is absent from the current sandbox PATH.",
        "linker-error": "The linker cannot resolve a library supplied by the shell.",
        "port-collision": "Another process already owns the requested port.",
    }.get(incident.kind, "NixPulse detected a failed command.")
    return IncidentDiagnosis(package, package, explanation)


class NixPulseDaemon:
    """State manager for provisioning and operator-approved remediation."""

    def __init__(
        self,
        *,
        config: Settings = default_settings,
        ollama: OllamaClient | None = None,
        tmux_manager: Any | None = None,
    ) -> None:
        self.config = config
        self.ollama = ollama or OllamaClient(
            host=self.config.ollama_host, model=self.config.model_name
        )
        self.tmux = tmux_manager
        self.manifest: WorkspaceManifest | None = None
        self.shell_file: Path | None = None

    @property
    def model_enabled(self) -> bool:
        return self.ollama is not None

    def plan(self, intent: str, *, use_model: bool = True) -> IntentPlan:
        if use_model and self.ollama is not None:
            try:
                payload = self.ollama.generate_json(
                    intent.strip(), system=INTENT_SYSTEM_PROMPT
                )
                return parse_intent(payload)
            except Exception:
                pass
        return offline_intent_plan(intent)

    def provision(self, plan: IntentPlan, workspace: str | Path) -> Path:
        destination = Path(workspace)
        destination.mkdir(parents=True, exist_ok=True)
        self.manifest = WorkspaceManifest(
            packages=plan.packages,
            layout=plan.layout,
            start_command=plan.start_command,
            state_dir=self.config.state_dir,
        )
        self.shell_file = write_shell_nix(destination / "shell.nix", self.manifest)
        return self.shell_file

    def diagnose(self, output: str, *, exit_code: int | None = None, use_model: bool = True) -> Remediation | None:
        incident = detect_incident(output, exit_code=exit_code)
        if incident is None:
            return None
        diagnosis = None
        if use_model and self.ollama is not None:
            try:
                diagnosis = parse_incident(
                    self.ollama.generate_json(
                        truncate_output(output), system=INCIDENT_SYSTEM_PROMPT
                    )
                )
            except Exception:
                pass
        if diagnosis is None:
            diagnosis = offline_diagnosis(incident)
        return Remediation(incident, diagnosis)

    def apply_remediation(self, remediation: Remediation, *, approved: bool) -> Path:
        if not approved:
            raise PermissionError("operator approval is required before changing the manifest")
        if self.manifest is None or self.shell_file is None:
            raise RuntimeError("provision a workspace before applying remediation")
        if not remediation.diagnosis.suggested_package:
            raise RuntimeError("this incident has no package remediation")
        try:
            self.manifest = self.manifest.with_package(remediation.diagnosis.suggested_package)
        except ManifestError:
            raise
        write_shell_nix(self.shell_file, self.manifest)
        if self.tmux is not None:
            if self.tmux.layout is None:
                raise RuntimeError("tmux layout has not been created")
            self.tmux.reload_shell(self.tmux.layout.sandbox, self.shell_file)
        return self.shell_file
