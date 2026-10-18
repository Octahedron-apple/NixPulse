"""Extensible Skills Engine for NixPulse.

Skills represent isolated units of capability with well-defined contracts:
1. Declarative metadata & JSON trigger schema.
2. Two-phase operation: Intent resolution -> Deterministic host execution.
3. Zero direct state mutation: validation guards on paths, flakes, shell scripts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from nixpulse.context import WorkspaceContext
from nixpulse.flake import FlakeSpec, render_flake_nix, validate_flake
from nixpulse.tasks import TaskRunner, generate_shell_script, validate_shell_script


class SkillError(ValueError):
    """Raised when skill execution fails or parameters are invalid."""


@dataclass(frozen=True)
class SkillResult:
    """The structured result of a skill execution."""

    skill: str
    ok: bool
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "ok": self.ok,
            "output": self.output,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SkillDefinition:
    """A registered skill with metadata, JSON schema, and runner."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[Any, Mapping[str, Any]], SkillResult]


class SkillRegistry:
    """Registry and dispatcher for NixPulse skills."""

    def __init__(self, workspace: str | os.PathLike[str], *, tmux_manager: Any | None = None) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.context = WorkspaceContext(self.workspace)
        self.task_runner = TaskRunner(self.workspace)
        self.tmux_manager = tmux_manager
        self._skills: dict[str, SkillDefinition] = {}
        self._register_default_skills()

    def register(self, skill: SkillDefinition) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> SkillDefinition:
        if name not in self._skills:
            raise SkillError(f"Unknown skill: {name!r}")
        return self._skills[name]

    def list_skills(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            }
            for s in self._skills.values()
        ]

    def as_ollama_tools(self) -> list[dict[str, Any]]:
        """Export registered skills formatted for Ollama function calling."""
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters,
                },
            }
            for s in self._skills.values()
        ]

    def execute(self, name: str, params: Mapping[str, Any]) -> SkillResult:
        skill = self.get(name)
        try:
            return skill.handler(self, params)
        except Exception as exc:
            return SkillResult(
                skill=name,
                ok=False,
                output=f"Error executing skill {name}: {exc}",
                metadata={"error": str(exc)},
            )

    # -------------------------------------------------------------------------
    # Default Skills Handlers
    # -------------------------------------------------------------------------

    def _skill_read_file(self, params: Mapping[str, Any]) -> SkillResult:
        path = params.get("path")
        if not path:
            return SkillResult("read_file", False, "Missing 'path' parameter")
        start_line = int(params.get("start_line", 1))
        end_line = int(params["end_line"]) if "end_line" in params and params["end_line"] is not None else None
        max_lines = int(params["max_lines"]) if "max_lines" in params and params["max_lines"] is not None else None

        # Check if caller wants a directory tree
        if params.get("tree", False) or (self.workspace / path).is_dir():
            tree = self.context.directory_tree(path, max_depth=int(params.get("max_depth", 2)))
            return SkillResult("read_file", True, tree, {"mode": "directory_tree", "path": str(path)})

        content = self.context.read_file(path, start_line=start_line, end_line=end_line, max_lines=max_lines)
        return SkillResult(
            "read_file",
            True,
            content,
            {"path": str(path), "start_line": start_line, "end_line": end_line},
        )

    def _skill_write_script(self, params: Mapping[str, Any]) -> SkillResult:
        name = str(params.get("name", "script"))
        content = params.get("content")
        steps = params.get("steps")
        if not content and not steps:
            return SkillResult("write_script", False, "Requires either 'content' or 'steps'")

        if steps:
            script_text = generate_shell_script(steps)
        else:
            script_text = str(content)

        validate_shell_script(script_text)
        script_dir = self.workspace / ".nixpulse-scripts"
        script_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / f"{name}.sh"
        script_path.write_text(script_text, encoding="utf-8")
        script_path.chmod(0o755)

        return SkillResult(
            "write_script",
            True,
            f"Wrote executable script to {script_path.relative_to(self.workspace)}",
            {"path": str(script_path), "name": name},
        )

    def _skill_manage_task(self, params: Mapping[str, Any]) -> SkillResult:
        task_name = params.get("task")
        action = params.get("action", "run")
        if action == "list":
            tasks = list(self.task_runner.tasks.keys())
            return SkillResult("manage_task", True, f"Configured tasks: {', '.join(tasks)}", {"tasks": tasks})

        if not task_name:
            return SkillResult("manage_task", False, "Missing 'task' parameter")

        res = self.task_runner.run(str(task_name))
        out = (
            f"Task {task_name} finished with exit code {res.returncode}\n"
            f"Duration: {res.duration:.2f}s\n"
            f"Logs: {res.stdout_log} | {res.stderr_log}"
        )
        return SkillResult(
            "manage_task",
            res.ok,
            out,
            {"task": task_name, "exit_code": res.returncode, "stdout_log": str(res.stdout_log), "stderr_log": str(res.stderr_log)},
        )

    def _skill_flake_manage(self, params: Mapping[str, Any]) -> SkillResult:
        packages = params.get("packages", [])
        build_inputs = params.get("build_inputs", [])
        system = params.get("system", "x86_64-linux")
        description = params.get("description", "NixPulse development shell")

        spec = FlakeSpec(
            packages=tuple(packages),
            build_inputs=tuple(build_inputs),
            system=system,
            description=description,
        )
        flake_content = render_flake_nix(spec)
        flake_file = self.workspace / "flake.nix"
        flake_file.write_text(flake_content, encoding="utf-8")

        # Optional check if nix is present
        check_msg = "Flake written."
        try:
            check_res = validate_flake(flake_file)
            if not check_res.ok:
                check_msg += f" (Check warning: {check_res.stderr.strip()})"
            else:
                check_msg += " Pre-flight check passed."
        except Exception:
            check_msg += " (Pre-flight syntax check bypassed)."

        return SkillResult(
            "flake_manage",
            True,
            f"{check_msg}\nPackages: {', '.join(spec.packages)}\nLibraries: {', '.join(spec.build_inputs)}",
            {"path": str(flake_file), "packages": list(spec.packages), "build_inputs": list(spec.build_inputs)},
        )

    def _skill_run_command(self, params: Mapping[str, Any]) -> SkillResult:
        command = params.get("command")
        if not command:
            return SkillResult("run_command", False, "Missing 'command' parameter")

        if self.tmux_manager and self.tmux_manager.layout:
            sandbox = self.tmux_manager.layout.sandbox
            if not self.tmux_manager.is_idle(sandbox):
                return SkillResult("run_command", False, "Dev pane is currently busy. Command not sent.")
            self.tmux_manager.send_keys(sandbox, str(command))
            return SkillResult("run_command", True, f"Dispatched command to Pane 0: {command}", {"pane": "sandbox"})

        return SkillResult(
            "run_command",
            True,
            f"[Dry-run / Headless] Dispatched to dev pane: {command}",
            {"command": command},
        )

    def _skill_tmux_control(self, params: Mapping[str, Any]) -> SkillResult:
        action = params.get("action", "layout")
        import subprocess

        # If manager exists, use it; otherwise use direct tmux commands
        try:
            if action == "layout":
                layout_name = params.get("layout", "main-horizontal")
                # Map loose layout requests
                layout_map = {
                    "split": "even-horizontal",
                    "even": "even-horizontal",
                    "horizontal": "even-horizontal",
                    "vertical": "even-vertical",
                    "tile": "tiled",
                    "tiled": "tiled",
                }
                for k, v in layout_map.items():
                    if k in layout_name.lower():
                        layout_name = v
                        break
                if self.tmux_manager:
                    self.tmux_manager.set_layout(layout_name)
                else:
                    subprocess.run(["tmux", "select-layout", layout_name], check=True)
                return SkillResult("tmux_control", True, f"Tmux window layout set to {layout_name!r}", {"layout": layout_name})

            elif action == "split":
                direction_arg = params.get("direction", "horizontal")
                flag = "-v" if "vert" in direction_arg.lower() or direction_arg == "V" else "-h"
                if self.tmux_manager and self.tmux_manager.window:
                    from libtmux.window import PaneDirection
                    p_dir = PaneDirection.Below if flag == "-v" else PaneDirection.Right
                    new_p = self.tmux_manager.window.split(direction=p_dir)
                    return SkillResult("tmux_control", True, f"Spawned new tmux pane: {new_p.id}", {"pane_id": new_p.id})
                else:
                    subprocess.run(["tmux", "split-window", flag], check=True)
                    return SkillResult("tmux_control", True, f"Spawned new tmux pane with split {flag}")

            elif action == "resize":
                direction = params.get("direction", "U").upper()
                flag = {"U": "-U", "D": "-D", "L": "-L", "R": "-R"}.get(direction, "-U")
                cells = str(int(params.get("cells", 5)))
                if self.tmux_manager:
                    self.tmux_manager.resize_pane(direction=direction, cells=int(cells))
                else:
                    subprocess.run(["tmux", "resize-pane", flag, cells], check=True)
                return SkillResult("tmux_control", True, f"Resized pane direction {direction} by {cells} cells")

            elif action == "notify":
                message = params.get("message", "NixPulse Alert")
                if self.tmux_manager:
                    self.tmux_manager.display_message(message)
                else:
                    subprocess.run(["tmux", "display-message", message], check=True)
                return SkillResult("tmux_control", True, f"Flashed status message: {message}", {"message": message})

            else:
                return SkillResult("tmux_control", False, f"Unknown tmux action: {action}")
        except Exception as exc:
            return SkillResult("tmux_control", False, f"tmux command failed: {exc}", {"error": str(exc)})

    def _register_default_skills(self) -> None:
        self.register(
            SkillDefinition(
                name="read_file",
                description="Targeted file inspection, line-range slicing, or directory tree bounds.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative workspace file path"},
                        "start_line": {"type": "integer", "description": "Start line (1-indexed)"},
                        "end_line": {"type": "integer", "description": "End line (inclusive)"},
                        "max_lines": {"type": "integer", "description": "Max lines to return"},
                        "tree": {"type": "boolean", "description": "Set true to return directory tree"},
                    },
                    "required": ["path"],
                },
                handler=lambda reg, params: reg._skill_read_file(params),
            )
        )
        self.register(
            SkillDefinition(
                name="write_script",
                description="Generate validated bash scripts in .nixpulse-scripts/ with chmod +x.",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Script base name"},
                        "content": {"type": "string", "description": "Full bash script text"},
                        "steps": {"type": "array", "items": {"type": "string"}, "description": "List of shell steps"},
                    },
                },
                handler=lambda reg, params: reg._skill_write_script(params),
            )
        )
        self.register(
            SkillDefinition(
                name="manage_task",
                description="Orchestrate deterministic tasks (build/test/bench) with bounded logs.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Task name"},
                        "action": {"type": "string", "enum": ["run", "list"], "default": "run"},
                    },
                },
                handler=lambda reg, params: reg._skill_manage_task(params),
            )
        )
        self.register(
            SkillDefinition(
                name="flake_manage",
                description="Nix flake and devShell management with syntax pre-flight checks.",
                parameters={
                    "type": "object",
                    "properties": {
                        "packages": {"type": "array", "items": {"type": "string"}},
                        "build_inputs": {"type": "array", "items": {"type": "string"}},
                        "system": {"type": "string", "default": "x86_64-linux"},
                    },
                },
                handler=lambda reg, params: reg._skill_flake_manage(params),
            )
        )
        self.register(
            SkillDefinition(
                name="run_command",
                description="Dispatch guarded command to Pane 0 after checking idle prompt state.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run in dev pane"},
                    },
                    "required": ["command"],
                },
                handler=lambda reg, params: reg._skill_run_command(params),
            )
        )
        self.register(
            SkillDefinition(
                name="tmux_control",
                description="Modify the tmux war room session: change window layout, resize panes, or flash banner messages.",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["layout", "split", "resize", "notify"],
                            "description": "Tmux action to perform: 'layout' (change layout), 'split' (add a new pane), 'resize' (adjust pane dimensions), 'notify' (flash message)",
                        },
                        "layout": {
                            "type": "string",
                            "enum": ["main-horizontal", "even-horizontal", "tiled", "main-vertical", "even-vertical"],
                            "description": "Layout name when action is 'layout'",
                        },
                        "target": {
                            "type": "string",
                            "enum": ["sandbox", "telemetry", "control"],
                            "description": "Target pane for resizing",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["U", "D", "L", "R"],
                            "description": "Resize direction: U (Up), D (Down), L (Left), R (Right)",
                        },
                        "cells": {
                            "type": "integer",
                            "description": "Number of terminal cells to adjust",
                        },
                        "message": {
                            "type": "string",
                            "description": "Status message to flash across tmux status bar",
                        },
                    },
                    "required": ["action"],
                },
                handler=lambda reg, params: reg._skill_tmux_control(params),
            )
        )
