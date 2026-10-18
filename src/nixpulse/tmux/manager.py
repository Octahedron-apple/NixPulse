"""tmux orchestration through libtmux.

NixPulse intentionally does not shell out to ``tmux`` for pane/session
management.  libtmux owns the server interaction; subprocess is reserved for
the separate Nix syntax preflight command.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from libtmux import Server
from libtmux.exc import LibTmuxException, TmuxCommandNotFound
from libtmux.window import PaneDirection


class TmuxError(RuntimeError):
    """Raised when a tmux operation cannot be completed safely."""


@dataclass(frozen=True)
class PaneLayout:
    sandbox: Any
    telemetry: Any
    control: Any


class TmuxManager:
    """Create and operate the NixPulse three-pane flight deck."""

    def __init__(self, session_name: str = "nixpulse-war-room", *, server=None) -> None:
        # libtmux 0.61 uses unicode record separator '␞' which tmux replaces with '_'
        # unless a UTF-8 locale is explicitly configured.
        if "UTF-8" not in os.environ.get("LC_ALL", ""):
            os.environ["LC_ALL"] = "C.UTF-8"
        if "UTF-8" not in os.environ.get("LANG", ""):
            os.environ["LANG"] = "C.UTF-8"

        self.session_name = session_name
        self.server = server or Server()
        self.session = None
        self.window = None
        self.layout: PaneLayout | None = None

    def _server_error(self, exc: Exception) -> TmuxError:
        if isinstance(exc, TmuxCommandNotFound):
            return TmuxError("tmux is not installed or is not on PATH")
        return TmuxError(str(exc) or "tmux operation failed")

    def session_exists(self) -> bool:
        try:
            return bool(self.server.has_session(self.session_name, exact=True))
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    def _existing_session(self):
        try:
            return self.server.sessions.get(
                default=None, session_name=self.session_name
            )
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    @staticmethod
    def _pane(pane: Any) -> Any:
        if hasattr(pane, "send_keys"):
            return pane
        raise TmuxError(f"invalid libtmux pane: {pane!r}")

    def create_layout(
        self,
        workdir: str | os.PathLike[str],
        *,
        reuse_existing: bool = True,
    ) -> PaneLayout:
        """Create or reuse a session and ensure it has three named roles."""

        directory = Path(workdir).resolve()
        if not directory.is_dir():
            raise TmuxError(f"workspace directory does not exist: {directory}")
        try:
            if self.session_exists():
                if not reuse_existing:
                    raise TmuxError(
                        f"tmux session {self.session_name!r} already exists"
                    )
                session = self._existing_session()
                if session is None:
                    raise TmuxError("tmux reported a session but libtmux could not find it")
            else:
                session = self.server.new_session(
                    session_name=self.session_name,
                    attach=False,
                    start_directory=str(directory),
                    window_name="war-room",
                    x=160,
                    y=50,
                )
            window = session.windows[0]
            panes = list(window.panes)
            sandbox = panes[0]
            if len(panes) < 2:
                telemetry = window.split(
                    start_directory=str(directory), direction=PaneDirection.Right
                )
            else:
                telemetry = panes[1]
            panes = list(window.panes)
            if len(panes) < 3:
                control = window.split(
                    target=sandbox.id,
                    start_directory=str(directory),
                    direction=PaneDirection.Below,
                )
            else:
                control = panes[2]
            window.select_layout("main-horizontal")
        except (LibTmuxException, IndexError, AttributeError) as exc:
            if isinstance(exc, TmuxError):
                raise
            raise self._server_error(exc) from exc

        self.session = session
        self.window = window
        self.layout = PaneLayout(sandbox, telemetry, control)
        return self.layout

    def send_keys(self, pane: Any, command: str, *, enter: bool = True) -> None:
        if "\n" in command or "\r" in command:
            raise TmuxError("tmux commands cannot contain literal newlines")
        try:
            self._pane(pane).send_keys(command, enter=enter)
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    def current_command(self, pane: Any) -> str:
        try:
            p = self._pane(pane)
            if hasattr(p, "refresh"):
                try:
                    p.refresh()
                except Exception:
                    pass
            cmd = getattr(p, "pane_current_command", "")
            if not cmd and hasattr(p, "id"):
                # Direct tmux query fallback
                res = self.server.cmd("display-message", "-p", "-t", p.id, "#{pane_current_command}")
                cmd = res.stdout[0] if res.stdout else ""
            return str(cmd)
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    def get_pane_cwd(self, pane: Any) -> Path | None:
        """Query the real-time working directory of a tmux pane."""
        try:
            p = self._pane(pane)
            res = self.server.cmd("display-message", "-p", "-t", p.id, "#{pane_current_path}")
            if res.stdout and res.stdout[0].strip():
                return Path(res.stdout[0].strip()).resolve()
        except Exception:
            pass
        return None

    def is_idle(self, pane: Any) -> bool:
        return self.current_command(pane).strip().lower() in {
            "bash",
            "zsh",
            "fish",
            "sh",
        }

    def wait_for_idle(self, pane: Any, *, timeout: float = 30.0) -> bool:
        """Wait for a guarded reload to return to an interactive shell prompt."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_idle(pane):
                return True
            time.sleep(0.1)
        return self.is_idle(pane)

    def capture_pane(self, pane: Any, *, lines: int = 80) -> str:
        try:
            captured = self._pane(pane).capture_pane(start=-lines)
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc
        return "\n".join(captured or [])

    def preflight(self, shell_file: str | os.PathLike[str]) -> None:
        """Parse a generated shell file before dispatching any reload keys."""

        path = Path(shell_file)
        if not path.is_file():
            raise TmuxError(f"shell file does not exist: {path}")
        try:
            result = subprocess.run(
                ["nix-instantiate", "--parse", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TmuxError("nix-instantiate is not installed") from exc
        if result.returncode != 0:
            raise TmuxError((result.stderr or result.stdout).strip() or "Nix parse failed")

    def reload_shell(self, pane: Any, shell_file: str | os.PathLike[str]) -> None:
        """Safely reload an idle pane without replacing its parent shell."""

        self.preflight(shell_file)
        if not self.wait_for_idle(pane, timeout=3.0):
            raise TmuxError("target pane is not idle; refusing to inject a reload")
        # Do not use ``exec``: a bad Nix expression must not kill the tmux pane.
        self.send_keys(
            pane,
            "nix-shell --run 'bash -i' || "
            "echo '[NixPulse] reload failed; current shell preserved'",
        )

    def set_layout(self, layout_name: str) -> None:
        """Change tmux window layout (e.g. main-horizontal, even-horizontal, tiled)."""
        valid = {"main-horizontal", "main-vertical", "even-horizontal", "even-vertical", "tiled"}
        if layout_name not in valid:
            raise TmuxError(f"invalid layout name: {layout_name!r}; must be one of {valid}")
        try:
            if self.window is not None:
                self.window.select_layout(layout_name)
            else:
                session = self._existing_session()
                if session and session.windows:
                    session.windows[0].select_layout(layout_name)
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    def resize_pane(self, target: str = "telemetry", direction: str = "U", cells: int = 5) -> None:
        """Resize target pane (sandbox, telemetry, control) by direction (U, D, L, R) and cells."""
        pane_map = {
            "sandbox": getattr(self.layout, "sandbox", None) if self.layout else None,
            "telemetry": getattr(self.layout, "telemetry", None) if self.layout else None,
            "control": getattr(self.layout, "control", None) if self.layout else None,
        }
        pane = pane_map.get(target)
        try:
            flag_map = {"U": "-U", "D": "-D", "L": "-L", "R": "-R", "up": "-U", "down": "-D", "left": "-L", "right": "-R"}
            flag = flag_map.get(direction.upper(), "-U")
            if pane is not None:
                self.server.cmd("resize-pane", "-t", pane.id, flag, str(cells))
            else:
                self.server.cmd("resize-pane", flag, str(cells))
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    def display_message(self, message: str) -> None:
        """Flash a notification banner across the tmux status line."""
        try:
            self.server.cmd("display-message", message)
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    def attach(self) -> None:
        """Attach the current terminal using libtmux, not a tmux subprocess."""

        try:
            self.server.attach_session(self.session_name)
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

    def close(self) -> None:
        try:
            self.server.kill_session(self.session_name)
        except LibTmuxException as exc:
            raise self._server_error(exc) from exc

