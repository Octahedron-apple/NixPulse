"""Safe, deterministic generation of NixPulse ``shell.nix`` files.

The language model is deliberately not trusted with Nix expressions.  It may
suggest generic package tokens, but this module is the only place where those
tokens become ``pkgs.<attribute>`` expressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import tempfile
from typing import Iterable, Mapping


# Keep this list intentionally small and boring.  Adding an entry is a reviewable
# change and is safer than allowing arbitrary model output into Nix evaluation.
NIXPKGS_ATTR_MAP: Mapping[str, str] = {
    "btop": "btop",
    "gcc": "gcc",
    "go": "go",
    "gnumake": "gnumake",
    "hey": "hey",
    "htop": "htop",
    "ollama": "ollama",
    "pkg-config": "pkg-config",
    "pkgconfig": "pkg-config",
    "sqlite": "sqlite",
    "tmux": "tmux",
    "zlib": "zlib",
}

TOKEN_ALIASES: Mapping[str, str] = {
    "c-compiler": "gcc",
    "compiler": "gcc",
    "gnu-make": "gnumake",
    "load-generator": "hey",
    "load-test": "hey",
    "sqlite3": "sqlite",
    "zlib-devel": "zlib",
    "libz": "zlib",
}

LIBRARY_ATTRIBUTES = frozenset({"zlib"})

_TOKEN_RE = re.compile(r"[^a-z0-9+.-]+")


class ManifestError(ValueError):
    """Raised when an intent contains a package outside the safe allow-list."""


def normalize_token(token: str) -> str:
    """Normalize a model/user package token without changing its meaning."""

    normalized = _TOKEN_RE.sub("-", token.strip().lower()).strip("-")
    return TOKEN_ALIASES.get(normalized, normalized)


def resolve_package(token: str) -> str:
    """Return the canonical nixpkgs attribute for a generic package token."""

    normalized = normalize_token(token)
    try:
        return NIXPKGS_ATTR_MAP[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted(set(NIXPKGS_ATTR_MAP.values())))
        raise ManifestError(
            f"Unsupported package token {token!r}; allowed packages: {allowed}"
        ) from exc


def resolve_packages(tokens: Iterable[str]) -> tuple[str, ...]:
    """Resolve, de-duplicate, and preserve the order of package tokens."""

    resolved: list[str] = []
    for token in tokens:
        attribute = resolve_package(token)
        if attribute not in resolved:
            resolved.append(attribute)
    return tuple(resolved)


@dataclass(frozen=True)
class WorkspaceManifest:
    """Validated state used to render a disposable Nix development shell."""

    packages: tuple[str, ...]
    libraries: tuple[str, ...] = ()
    layout: str = "main-horizontal"
    start_command: str = "clear"
    state_dir: str = "/tmp/nixpulse"
    metadata: dict[str, str] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "packages", resolve_packages(self.packages))
        object.__setattr__(self, "libraries", resolve_packages(self.libraries) if self.libraries else ())
        if self.layout not in {"main-horizontal", "even-horizontal", "tiled"}:
            raise ManifestError(f"Unsupported tmux layout: {self.layout!r}")
        if "\n" in self.start_command or "\r" in self.start_command:
            raise ManifestError("start_command cannot contain a newline")

    @property
    def all_packages(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.packages, *self.libraries)))

    def with_package(self, token: str) -> "WorkspaceManifest":
        """Return a new manifest with a validated package added."""

        package = resolve_package(token)
        if package in self.packages or package in self.libraries:
            return self
        packages = self.packages
        libraries = self.libraries
        if package in LIBRARY_ATTRIBUTES:
            libraries = (*libraries, package)
        else:
            packages = (*packages, package)
        return WorkspaceManifest(
            packages=packages,
            libraries=libraries,
            layout=self.layout,
            start_command=self.start_command,
            state_dir=self.state_dir,
            metadata=dict(self.metadata),
        )


def _nix_list(attributes: Iterable[str]) -> str:
    return " ".join(attributes)


def render_shell_nix(manifest: WorkspaceManifest) -> str:
    """Render a stable, human-readable legacy ``shell.nix`` expression."""

    package_expr = _nix_list(manifest.packages)
    library_expr = _nix_list(manifest.libraries)
    state_dir = manifest.state_dir.replace("'", "")
    stderr_log = str(Path(state_dir) / "stderr.log").replace("'", "")
    library_block = ""
    if manifest.libraries:
        library_block = (
            "\n    export LD_LIBRARY_PATH=\"${pkgs.lib.makeLibraryPath libs}:"
            "$LD_LIBRARY_PATH\""
        )

    return f"""{{ pkgs ? import <nixpkgs> {{}} }}:
let
  libs = with pkgs; [ {library_expr} ];
in
pkgs.mkShell {{
  buildInputs = with pkgs; [ {package_expr} ] ++ libs;
  shellHook = ''
    export NIXPULSE_STATE_DIR='{state_dir}'
    export NIXPULSE_STDERR_LOG='{stderr_log}'
    mkdir -p \"$NIXPULSE_STATE_DIR\"
    # Commands can opt into deterministic stderr capture with: 2>\"$NIXPULSE_STDERR_LOG\"
    nixpulse_prompt_status() {{
      local status=$?
      printf '{{"event":"prompt","status":%s}}\\n' \"$status\" >> \"$NIXPULSE_STATE_DIR/status.log\"
      return \"$status\"
    }}
    case \";''${{PROMPT_COMMAND:-}};\" in
      *';nixpulse_prompt_status;'*) ;;
      *) PROMPT_COMMAND=\"nixpulse_prompt_status''${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}\" ;;
    esac{library_block}
  '';
}}
"""


def write_shell_nix(path: str | os.PathLike[str], manifest: WorkspaceManifest) -> Path:
    """Atomically write a generated shell file and return its path."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = render_shell_nix(manifest)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(destination)
    return destination
