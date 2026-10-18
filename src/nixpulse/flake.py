"""First-class Nix flake generation for NixPulse workspaces.

The model is not involved in this module.  Package names are resolved through
the same allow-list used by :mod:`nixpulse.manifest`, and all generated files
are replaced atomically.  ``shell.nix`` is emitted alongside the flake so an
older ``nix-shell`` workflow can continue to use the workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Iterable

from nixpulse.manifest import (
    ManifestError,
    WorkspaceManifest,
    render_shell_nix,
    resolve_package,
)


class FlakeError(ValueError):
    """Raised when a flake cannot be safely generated."""


class FlakeValidationError(RuntimeError):
    """Raised when an explicitly requested ``nix flake check`` fails."""

    def __init__(self, result: "FlakeCheckResult") -> None:
        self.result = result
        detail = (result.stderr or result.stdout).strip()
        super().__init__(detail or f"nix flake check failed with exit code {result.returncode}")


@dataclass(frozen=True)
class FlakeCheckResult:
    """The observable result of an explicit flake check."""

    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


_SYSTEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _canonical_tokens(tokens: Iterable[str], *, field: str) -> tuple[str, ...]:
    result: list[str] = []
    for token in tokens:
        if not isinstance(token, str):
            raise FlakeError(f"{field} must contain only package names")
        try:
            package = resolve_package(token)
        except ManifestError as exc:
            raise FlakeError(str(exc)) from exc
        if package not in result:
            result.append(package)
    return tuple(result)


@dataclass(frozen=True)
class FlakeSpec:
    """Validated inputs used by the flake and legacy shell renderers.

    ``packages`` are exposed through the modern ``packages`` mkShell
    attribute.  ``build_inputs`` are the library/runtime inputs.  zlib is a
    default runtime input because NixPulse uses its library path as the
    deterministic dynamic-linker example; callers can disable that default
    for a deliberately minimal shell.
    """

    packages: tuple[str, ...] = ()
    build_inputs: tuple[str, ...] = ()
    system: str = "x86_64-linux"
    description: str = "NixPulse development shell"
    include_zlib: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.system, str) or not _SYSTEM_RE.fullmatch(self.system):
            raise FlakeError(f"invalid Nix system: {self.system!r}")
        if not isinstance(self.description, str) or "\n" in self.description or "\r" in self.description:
            raise FlakeError("flake description must be a single line")
        packages = _canonical_tokens(self.packages, field="packages")
        build_inputs = _canonical_tokens(self.build_inputs, field="build_inputs")
        if self.include_zlib and "zlib" not in build_inputs:
            build_inputs = (*build_inputs, "zlib")
        object.__setattr__(self, "packages", packages)
        object.__setattr__(self, "build_inputs", build_inputs)

    @classmethod
    def from_manifest(
        cls,
        manifest: WorkspaceManifest,
        *,
        system: str = "x86_64-linux",
        description: str = "NixPulse development shell",
    ) -> "FlakeSpec":
        return cls(
            packages=manifest.packages,
            build_inputs=manifest.libraries,
            system=system,
            description=description,
        )


def _coerce_spec(
    value: FlakeSpec | WorkspaceManifest | Iterable[str],
    *,
    system: str | None = None,
    build_inputs: Iterable[str] = (),
) -> FlakeSpec:
    if isinstance(value, FlakeSpec):
        if system is None and not tuple(build_inputs):
            return value
        return FlakeSpec(
            packages=value.packages,
            build_inputs=tuple(build_inputs) or value.build_inputs,
            system=system or value.system,
            description=value.description,
            include_zlib=value.include_zlib,
        )
    if isinstance(value, WorkspaceManifest):
        return FlakeSpec.from_manifest(value, system=system or "x86_64-linux")
    try:
        packages = tuple(value)
    except TypeError as exc:
        raise FlakeError("flake input must be a FlakeSpec, WorkspaceManifest, or package iterable") from exc
    return FlakeSpec(
        packages=packages,
        build_inputs=tuple(build_inputs),
        system=system or "x86_64-linux",
    )


def render_flake_nix(
    value: FlakeSpec | WorkspaceManifest | Iterable[str],
    *,
    system: str | None = None,
    build_inputs: Iterable[str] = (),
) -> str:
    """Render a flake with ``devShells.<system>.default``.

    Only canonical package attributes from ``manifest.py`` reach the Nix
    expression.  Both ``packages`` and ``buildInputs`` are explicit in the
    generated ``mkShell`` for compatibility with current and older Nix
    development-shell conventions.
    """

    spec = _coerce_spec(value, system=system, build_inputs=build_inputs)
    package_expr = " ".join(spec.packages)
    build_input_expr = " ".join(spec.build_inputs)
    description = json.dumps(spec.description)
    return f'''{{
  description = {description};

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {{ self, nixpkgs }}:
    {{
      devShells.{spec.system}.default =
        let
          pkgs = import nixpkgs {{ system = "{spec.system}"; }};
          libs = with pkgs; [ {build_input_expr} ];
        in
        pkgs.mkShell {{
          packages = with pkgs; [ {package_expr} ];
          buildInputs = with pkgs; [ {build_input_expr} ];
          shellHook = ''
            export LD_LIBRARY_PATH="${{pkgs.lib.makeLibraryPath libs}}:''${{LD_LIBRARY_PATH:-}}"
          '';
        }};
    }};
}}
'''


def render_legacy_shell_nix(
    value: FlakeSpec | WorkspaceManifest | Iterable[str],
    *,
    system: str | None = None,
    build_inputs: Iterable[str] = (),
) -> str:
    """Render the ``shell.nix`` fallback matching a generated flake."""

    spec = _coerce_spec(value, system=system, build_inputs=build_inputs)
    packages = spec.packages or ("zlib",)
    try:
        manifest = WorkspaceManifest(packages=packages, libraries=spec.build_inputs)
    except ManifestError as exc:
        raise FlakeError(str(exc)) from exc
    return render_shell_nix(manifest)


def _atomic_write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)
    return path


def validate_flake(
    path: str | os.PathLike[str],
    *,
    nix_binary: str = "nix",
    timeout: float | None = None,
) -> FlakeCheckResult:
    """Run ``nix flake check --no-build`` for an explicitly requested check.

    This function does not call the model or any other Nix operation.  A Nix
    evaluation failure is returned to the caller so it can decide how to
    present or handle the failure.
    """

    flake = Path(path)
    if flake.is_dir():
        flake = flake / "flake.nix"
    if not flake.is_file():
        raise FlakeError(f"flake file does not exist: {flake}")
    command = (nix_binary, "flake", "check", "--no-build")
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(flake.parent),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return FlakeCheckResult(command, 127, stderr=f"{nix_binary}: command not found")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return FlakeCheckResult(command, 124, stdout=stdout, stderr=stderr or "nix flake check timed out")
    return FlakeCheckResult(
        command,
        completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def write_flake_nix(
    path: str | os.PathLike[str],
    value: FlakeSpec | WorkspaceManifest | Iterable[str],
    *,
    system: str | None = None,
    build_inputs: Iterable[str] = (),
    write_legacy: bool = True,
    validate: bool = False,
    nix_binary: str = "nix",
    timeout: float | None = None,
) -> Path:
    """Atomically write ``flake.nix`` and, by default, ``shell.nix``.

    Validation is opt-in.  When ``validate=True`` the only command run is the
    explicit ``nix flake check --no-build`` check, and a failed check raises
    :class:`FlakeValidationError` after both files have been written.
    """

    destination = Path(path)
    if destination.name != "flake.nix":
        raise FlakeError(f"flake output must be named flake.nix: {destination}")
    spec = _coerce_spec(value, system=system, build_inputs=build_inputs)
    _atomic_write(destination, render_flake_nix(spec))
    if write_legacy:
        _atomic_write(destination.parent / "shell.nix", render_legacy_shell_nix(spec))
    if validate:
        result = validate_flake(destination, nix_binary=nix_binary, timeout=timeout)
        if not result.ok:
            raise FlakeValidationError(result)
    return destination


# Short aliases make the module convenient for callers that do not need the
# explicit ``_nix`` suffix while keeping the descriptive APIs above public.
render_flake = render_flake_nix
write_flake = write_flake_nix
