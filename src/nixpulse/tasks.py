"""Deterministic, workspace-confined task and shell-script execution.

Task definitions may come from a small Makefile, a small justfile, or a
JSON/YAML-like mapping.  The runner never delegates task discovery to ``make``
or ``just``: it parses the declaration, renders one bash script per task,
validates the shebang and ``bash -n`` result, then executes it with bounded
stdout/stderr logs.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping, Sequence


class TaskError(ValueError):
    """Raised for invalid task declarations or task selection."""


class UnsafePathError(TaskError):
    """Raised when a path escapes the configured workspace."""


class ScriptValidationError(TaskError):
    """Raised before a generated or supplied script can be executed."""


_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BASH_SHEBANGS = {
    "#!/bin/bash",
    "#!/usr/bin/bash",
    "#!/usr/bin/env bash",
}


def _validate_task_name(name: str) -> str:
    if not isinstance(name, str) or not _TASK_NAME_RE.fullmatch(name):
        raise TaskError(f"unsafe task name: {name!r}")
    return name


def _validate_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise TaskError("task commands must be non-empty strings")
    if "\x00" in command:
        raise TaskError("task commands cannot contain NUL bytes")
    return command.rstrip()


@dataclass(frozen=True)
class TaskStep:
    """One command and its workspace-relative execution context."""

    command: str
    cwd: str | os.PathLike[str] = "."
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", _validate_command(self.command))
        if not isinstance(self.cwd, (str, os.PathLike)):
            raise TaskError("step cwd must be a path")
        cwd = os.fspath(self.cwd)
        if "\x00" in cwd:
            raise TaskError("step cwd cannot contain NUL bytes")
        if not isinstance(self.env, Mapping):
            raise TaskError("step env must be a mapping")
        normalized: dict[str, str] = {}
        for key, value in self.env.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                raise TaskError(f"invalid environment variable name: {key!r}")
            if not isinstance(value, str):
                raise TaskError(f"environment value for {key!r} must be a string")
            normalized[key] = value
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "env", normalized)


@dataclass(frozen=True)
class TaskDefinition:
    """A named task with ordered steps and optional task dependencies."""

    name: str
    steps: tuple[TaskStep, ...] = ()
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_task_name(self.name)
        steps = tuple(_coerce_step(step) for step in self.steps)
        dependencies: list[str] = []
        for dependency in self.dependencies:
            _validate_task_name(dependency)
            if dependency not in dependencies:
                dependencies.append(dependency)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "dependencies", tuple(dependencies))


# A short name is useful when defining tasks in application code.
Task = TaskDefinition


@dataclass(frozen=True)
class TaskResult:
    """Result and bounded log locations for one task execution."""

    task: str
    returncode: int
    stdout_log: Path
    stderr_log: Path
    script: Path
    duration: float = 0.0
    skipped: bool = False
    failed_dependency: str | None = None

    @property
    def exit_code(self) -> int:
        return self.returncode

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.skipped


def _coerce_step(value: TaskStep | str | Mapping[str, Any]) -> TaskStep:
    if isinstance(value, TaskStep):
        return value
    if isinstance(value, str):
        return TaskStep(value)
    if not isinstance(value, Mapping):
        raise TaskError("each task step must be a command string or mapping")
    command = value.get("run", value.get("command", value.get("script")))
    if not isinstance(command, str):
        raise TaskError("task step mapping requires a string run/command field")
    return TaskStep(command, value.get("cwd", "."), value.get("env", {}))


def _steps_from_value(value: Any) -> tuple[TaskStep, ...]:
    if isinstance(value, Mapping):
        if "steps" in value:
            value = value["steps"]
        elif any(key in value for key in ("run", "command", "script")):
            value = [value]
        else:
            raise TaskError("task mapping requires steps, run, command, or script")
    if isinstance(value, str):
        return (TaskStep(value),)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise TaskError("task steps must be a string or sequence")
    return tuple(_coerce_step(step) for step in value)


def _dependencies_from_value(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise TaskError("task dependencies must be a string or sequence")
    return tuple(value)


def parse_task_mapping(value: Mapping[str, Any]) -> dict[str, TaskDefinition]:
    """Normalize a custom mapping into validated task definitions.

    Accepted examples are ``{"test": ["..."]}`` and
    ``{"tasks": {"test": {"steps": ["..."], "depends_on": ["lint"]}}}``.
    """

    if not isinstance(value, Mapping):
        raise TaskError("task definition must be a mapping")
    root: Mapping[str, Any] = value
    if "tasks" in value:
        nested = value["tasks"]
        if not isinstance(nested, Mapping):
            raise TaskError("tasks must contain a mapping")
        root = nested
    result: dict[str, TaskDefinition] = {}
    for name, raw in root.items():
        if name in {"version", "name", "description"} and name not in value.get("tasks", {}):
            continue
        _validate_task_name(name)
        if isinstance(raw, TaskDefinition):
            definition = raw
            if definition.name != name:
                definition = TaskDefinition(name, definition.steps, definition.dependencies)
        elif isinstance(raw, Mapping):
            steps = _steps_from_value(raw)
            dependencies = _dependencies_from_value(
                raw.get("depends_on", raw.get("dependencies", raw.get("deps")))
            )
            definition = TaskDefinition(name, steps, dependencies)
        else:
            definition = TaskDefinition(name, _steps_from_value(raw))
        result[name] = definition
    if not result:
        raise TaskError("at least one task is required")
    return result


def _recipe_command(line: str) -> str:
    command = line.strip()
    while command.startswith(("@", "-")):
        command = command[1:].lstrip()
    return command


def _parse_rule_file(text: str, *, justfile: bool) -> dict[str, TaskDefinition]:
    definitions: dict[str, dict[str, Any]] = {}
    current: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0].isspace():
            if not current:
                continue
            command = _recipe_command(raw_line)
            if command:
                for target in current:
                    definitions[target]["steps"].append(command)
            continue
        line = raw_line.strip()
        if line.startswith(".PHONY:") or line.startswith("set ") or line.startswith("export "):
            current = []
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+(?:\s+[A-Za-z0-9_.-]+)*)\s*:\s*(.*)$", line)
        if not match:
            current = []
            continue
        targets = match.group(1).split()
        dependencies = tuple(item for item in match.group(2).split() if item and not item.startswith("#"))
        current = []
        for target in targets:
            _validate_task_name(target)
            definitions.setdefault(target, {"dependencies": [], "steps": []})
            definitions[target]["dependencies"].extend(dependencies)
            current.append(target)
        # Make supports ``target:; command``.  Just recipes are always on the
        # following indented lines, so this is intentionally Make-only.
        if not justfile and ";" in match.group(2):
            dependency_text, command = match.group(2).split(";", 1)
            for target in targets:
                definitions[target]["dependencies"] = dependency_text.split()
                if _recipe_command(command):
                    definitions[target]["steps"].append(_recipe_command(command))
    return {
        name: TaskDefinition(name, tuple(data["steps"]), tuple(data["dependencies"]))
        for name, data in definitions.items()
    }


def parse_makefile(text: str) -> dict[str, TaskDefinition]:
    """Parse the recipe/dependency subset needed by NixPulse tasks."""

    if not isinstance(text, str):
        raise TaskError("Makefile content must be text")
    result = _parse_rule_file(text, justfile=False)
    if not result:
        raise TaskError("Makefile contains no task rules")
    return result


def parse_justfile(text: str) -> dict[str, TaskDefinition]:
    """Parse simple justfile recipes without invoking the just executable."""

    if not isinstance(text, str):
        raise TaskError("justfile content must be text")
    result = _parse_rule_file(text, justfile=True)
    if not result:
        raise TaskError("justfile contains no task recipes")
    return result


def _yaml_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith(("'", '"')) and value.endswith(value[0]):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value[1:-1]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    if value.startswith("[") and value.endswith("]"):
        return [_yaml_scalar(item) for item in value[1:-1].split(",") if item.strip()]
    return value


def _parse_simple_yaml(text: str) -> Mapping[str, Any]:
    """Parse the small indentation-based mapping subset used by task files."""

    tokens: list[tuple[int, str]] = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise TaskError(f"YAML-like task files cannot use tabs (line {number})")
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        tokens.append((indent, stripped))
    if not tokens:
        raise TaskError("task mapping is empty")

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(tokens) or tokens[index][0] < indent:
            return {}, index
        is_list = tokens[index][0] == indent and tokens[index][1].startswith("- ")
        result: Any = [] if is_list else {}
        while index < len(tokens) and tokens[index][0] == indent:
            content = tokens[index][1]
            if is_list:
                if not content.startswith("- "):
                    break
                remainder = content[2:].strip()
                if ":" in remainder and not remainder.startswith(("\"", "'")):
                    key, raw_value = remainder.split(":", 1)
                    item: dict[str, Any] = {key.strip(): _yaml_scalar(raw_value)} if raw_value.strip() else {key.strip(): None}
                    index += 1
                    if index < len(tokens) and tokens[index][0] > indent:
                        nested, index = parse_block(index, tokens[index][0])
                        if raw_value.strip():
                            if isinstance(nested, Mapping):
                                item.update(nested)
                        else:
                            item[key.strip()] = nested
                    result.append(item)
                else:
                    result.append(_yaml_scalar(remainder))
                    index += 1
                continue
            if content.startswith("- "):
                break
            if ":" not in content:
                raise TaskError(f"invalid YAML-like task line: {content!r}")
            key, raw_value = content.split(":", 1)
            key = key.strip()
            if not key:
                raise TaskError("YAML-like task keys cannot be empty")
            index += 1
            if raw_value.strip():
                result[key] = _yaml_scalar(raw_value)
            elif index < len(tokens) and tokens[index][0] > indent:
                result[key], index = parse_block(index, tokens[index][0])
            else:
                result[key] = None
        return result, index

    parsed, position = parse_block(0, tokens[0][0])
    if position != len(tokens) or not isinstance(parsed, Mapping):
        raise TaskError("YAML-like task definition must be a mapping")
    return parsed


def load_tasks(source: Mapping[str, Any] | str | os.PathLike[str], *, format: str | None = None) -> dict[str, TaskDefinition]:
    """Load task definitions from a mapping, file, JSON, or small YAML text."""

    if isinstance(source, Mapping):
        return parse_task_mapping(source)
    if isinstance(source, os.PathLike):
        path = Path(source)
        if not path.is_file():
            raise TaskError(f"task definition file does not exist: {path}")
        text = path.read_text(encoding="utf-8")
        format = format or ("make" if path.name == "Makefile" else "just" if path.name == "justfile" else path.suffix.lstrip("."))
    elif isinstance(source, str):
        possible_path = Path(source)
        if "\n" not in source and possible_path.is_file():
            text = possible_path.read_text(encoding="utf-8")
            format = format or ("make" if possible_path.name == "Makefile" else "just" if possible_path.name == "justfile" else possible_path.suffix.lstrip("."))
        else:
            text = source
    else:
        raise TaskError("task source must be a mapping, path, or text")
    selected = (format or "json").lower()
    if selected in {"make", "makefile"}:
        return parse_makefile(text)
    if selected in {"just", "justfile"}:
        return parse_justfile(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _parse_simple_yaml(text)
    if not isinstance(parsed, Mapping):
        raise TaskError("JSON/YAML task definition must be a mapping")
    return parse_task_mapping(parsed)


def validate_shebang(script: str) -> None:
    """Require a bash shebang before a script is considered executable."""

    if not isinstance(script, str) or "\x00" in script:
        raise ScriptValidationError("script must be text without NUL bytes")
    first_line = script.splitlines()[0] if script.splitlines() else ""
    if first_line not in _BASH_SHEBANGS:
        raise ScriptValidationError(
            "script must start with a bash shebang: #!/usr/bin/env bash, #!/bin/bash, or #!/usr/bin/bash"
        )


def validate_shell_script(script: str, *, bash_binary: str = "bash") -> None:
    """Validate the shebang and syntax without executing the script body."""

    validate_shebang(script)
    try:
        checked = subprocess.run(
            [bash_binary, "-n"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ScriptValidationError(f"bash executable not found: {bash_binary}") from exc
    if checked.returncode != 0:
        detail = (checked.stderr or checked.stdout).strip()
        raise ScriptValidationError(detail or "bash -n rejected the script")


def generate_shell_script(steps: Iterable[TaskStep | str | Mapping[str, Any]], *, shebang: str = "#!/usr/bin/env bash") -> str:
    """Render ordered task steps as a strict bash script.

    The resulting script is syntax-checked by :class:`TaskRunner` before it is
    executed.  Paths are checked by the runner because only it knows the
    workspace root.
    """

    if shebang not in _BASH_SHEBANGS:
        raise ScriptValidationError(f"unsupported bash shebang: {shebang!r}")
    normalized = tuple(_coerce_step(step) for step in steps)
    lines = [shebang, "set -euo pipefail", ""]
    for step in normalized:
        lines.append(f"cd -- {shlex.quote(os.fspath(step.cwd))}")
        for key, value in step.env.items():
            lines.append(f"export {key}={shlex.quote(value)}")
        lines.append(step.command)
        lines.append("")
    return "\n".join(lines)


def _workspace_path(root: Path, value: str | os.PathLike[str], *, field: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"{field} escapes workspace {root}: {value!r}") from exc
    return resolved


def _atomic_text(path: Path, content: str, *, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    if mode is not None:
        temporary.chmod(mode)
    temporary.replace(path)
    return path


def _write_bounded(stream: Any, path: Path, max_bytes: int) -> None:
    written = 0
    with path.open("wb") as output:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            if written < max_bytes:
                piece = chunk[: max_bytes - written]
                output.write(piece)
                written += len(piece)


class TaskRunner:
    """Execute parsed tasks in a workspace with deterministic boundaries."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        tasks: Mapping[str, Any] | str | os.PathLike[str] | None = None,
        *,
        max_log_bytes: int = 256 * 1024,
        log_dir: str | os.PathLike[str] = ".nixpulse-logs",
        script_dir: str | os.PathLike[str] = ".nixpulse-scripts",
        bash_binary: str = "bash",
    ) -> None:
        if max_log_bytes <= 0:
            raise TaskError("max_log_bytes must be positive")
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.log_dir = _workspace_path(self.workspace, log_dir, field="log directory")
        self.script_dir = _workspace_path(self.workspace, script_dir, field="script directory")
        self.max_log_bytes = max_log_bytes
        self.bash_binary = bash_binary
        if tasks is None:
            self.tasks: dict[str, TaskDefinition] = {}
        elif isinstance(tasks, os.PathLike):
            source = _workspace_path(self.workspace, tasks, field="task definition")
            self.tasks = load_tasks(source)
        elif isinstance(tasks, str) and "\n" not in tasks and Path(tasks).is_file():
            source = _workspace_path(self.workspace, tasks, field="task definition")
            self.tasks = load_tasks(source)
        else:
            self.tasks = load_tasks(tasks)

    def _definition(self, name: str) -> TaskDefinition:
        _validate_task_name(name)
        try:
            return self.tasks[name]
        except KeyError as exc:
            raise TaskError(f"unknown task: {name!r}") from exc

    def _script_for(self, definition: TaskDefinition) -> tuple[Path, str]:
        for step in definition.steps:
            _workspace_path(self.workspace, step.cwd, field=f"task {definition.name!r} cwd")
        script = generate_shell_script(definition.steps)
        validate_shell_script(script, bash_binary=self.bash_binary)
        path = _workspace_path(self.workspace, self.script_dir / f"{definition.name}.sh", field="script path")
        return _atomic_text(path, script, mode=0o700), script

    def _execute(
        self,
        name: str,
        script_path: Path,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> TaskResult:
        stdout_log = _workspace_path(self.workspace, self.log_dir / f"{name}.stdout.log", field="stdout log")
        stderr_log = _workspace_path(self.workspace, self.log_dir / f"{name}.stderr.log", field="stderr log")
        stdout_log.parent.mkdir(parents=True, exist_ok=True)
        stderr_log.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        process_env = os.environ.copy()
        if env is not None:
            for key, value in env.items():
                if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key) or not isinstance(value, str):
                    raise TaskError("env overrides require valid string variable names and values")
            process_env.update(env)
        try:
            process = subprocess.Popen(
                [self.bash_binary, str(script_path)],
                cwd=str(cwd or self.workspace),
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise TaskError(f"could not execute bash script: {exc}") from exc
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=_write_bounded, args=(process.stdout, stdout_log, self.max_log_bytes), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_write_bounded, args=(process.stderr, stderr_log, self.max_log_bytes), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            returncode = 124
        stdout_thread.join()
        stderr_thread.join()
        return TaskResult(
            task=name,
            returncode=returncode,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            script=script_path,
            duration=time.monotonic() - started,
        )

    def run(
        self,
        name: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> TaskResult:
        """Run dependencies in declaration order and return the exit result."""

        self._definition(name)
        completed: dict[str, TaskResult] = {}
        visiting: set[str] = set()

        def visit(task_name: str) -> TaskResult:
            if task_name in completed:
                return completed[task_name]
            if task_name in visiting:
                raise TaskError(f"cyclic task dependency involving {task_name!r}")
            definition = self._definition(task_name)
            visiting.add(task_name)
            try:
                for dependency in definition.dependencies:
                    dependency_result = visit(dependency)
                    if not dependency_result.ok:
                        result = TaskResult(
                            task=task_name,
                            returncode=dependency_result.returncode,
                            stdout_log=dependency_result.stdout_log,
                            stderr_log=dependency_result.stderr_log,
                            script=dependency_result.script,
                            skipped=True,
                            failed_dependency=dependency_result.task,
                        )
                        completed[task_name] = result
                        return result
                script_path, _ = self._script_for(definition)
                result = self._execute(task_name, script_path, env=env, timeout=timeout)
                completed[task_name] = result
                return result
            finally:
                visiting.remove(task_name)

        return visit(name)

    def run_task(self, name: str, **kwargs: Any) -> TaskResult:
        """Explicit alias for :meth:`run`."""

        return self.run(name, **kwargs)

    def run_script(
        self,
        script: str | os.PathLike[str],
        *,
        name: str = "script",
        cwd: str | os.PathLike[str] = ".",
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> TaskResult:
        """Validate and execute script text or a workspace-local script file."""

        _validate_task_name(name)
        execution_cwd = _workspace_path(self.workspace, cwd, field="script cwd")
        if isinstance(script, os.PathLike):
            source = _workspace_path(self.workspace, script, field="script source")
            try:
                content = source.read_text(encoding="utf-8")
            except OSError as exc:
                raise TaskError(f"could not read script source: {source}") from exc
        elif isinstance(script, str):
            content = script
        else:
            raise TaskError("script must be text or a workspace-local path")
        validate_shell_script(content, bash_binary=self.bash_binary)
        path = _workspace_path(self.workspace, self.script_dir / f"{name}.sh", field="script path")
        script_path = _atomic_text(path, content, mode=0o700)
        return self._execute(name, script_path, cwd=execution_cwd, env=env, timeout=timeout)

    execute_script = run_script
