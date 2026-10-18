"""Offline, deterministic five-step NixPulse demo.

Run with ``python -m nixpulse.demo`` or ``nixpulse demo``.  It deliberately
does not construct an Ollama request and does not require Nix or tmux; this
makes it safe to rehearse before the live dependencies are installed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from nixpulse.daemon import NixPulseDaemon
from nixpulse.tmux.manager import TmuxManager
from nixpulse.tmux.watcher import strip_ansi


DEMO_INTENT = "Set up a Go web service, benchmark it against SQLite, and monitor it."
DEMO_FAILURE = """\x1b[31mserver.c:2:10: fatal error: zlib.h: No such file or directory\x1b[0m
compilation terminated.
"""


def run_demo(*, workspace: str | Path | None = None, pause: float = 0.0, console: Console | None = None) -> Path:
    console = console or Console()
    daemon = NixPulseDaemon()
    root = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="nixpulse-demo-"))

    console.print(Panel.fit(
        "[bold cyan]NixPulse Offline War Room[/bold cyan]\n"
        "[dim]Deterministic rehearsal — Ollama is not contacted[/dim]",
        border_style="cyan",
    ))
    plan = daemon.plan(DEMO_INTENT)
    console.print(f"[yellow]1/5[/yellow] Intent accepted: {DEMO_INTENT}")
    console.print(f"[yellow]2/5[/yellow] Safe package plan: [green]{', '.join(plan.packages)}[/green]")
    shell_file = daemon.provision(plan, root)
    console.print(f"[yellow]3/5[/yellow] Generated [bold]{shell_file}[/bold]")
    if pause:
        time.sleep(pause)

    remediation = daemon.diagnose(DEMO_FAILURE, exit_code=1)
    if remediation is None:  # pragma: no cover - protects the demo contract
        raise RuntimeError("demo failure signature was not detected")
    table = Table(title="[bold red]Crash detected — deterministic triage[/bold red]")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Sanitized evidence", strip_ansi(remediation.incident.evidence))
    table.add_row("Suggested package", f"[green]pkgs.{remediation.diagnosis.suggested_package}[/green]")
    table.add_row("Explanation", remediation.diagnosis.explanation)
    console.print(table)
    console.print("[yellow]4/5[/yellow] Operator approval: [green]yes (demo preset)[/green]")
    daemon.apply_remediation(remediation, approved=True)
    console.print("[yellow]5/5[/yellow] [bold green]Manifest patched; sandbox is ready to reload.[/bold green]")
    console.print(Panel(
        f"[green]Demo complete[/green]\n"
        f"Packages: {', '.join(daemon.manifest.all_packages)}\n"
        f"Artifact: {shell_file}\n"
        "Model calls: 0",
        border_style="green",
    ))
    return shell_file


def run_tmux_demo(
    *,
    workspace: str | Path | None = None,
    session_name: str = "nixpulse-demo",
    attach: bool = False,
    console: Console | None = None,
) -> Path:
    """Run the same deterministic flow inside a real libtmux war room."""

    console = console or Console()
    root = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="nixpulse-tmux-demo-"))
    daemon = NixPulseDaemon()
    plan = daemon.plan(DEMO_INTENT)
    shell_file = daemon.provision(plan, root)
    manager = TmuxManager(session_name)
    layout = manager.create_layout(root)
    manager.send_keys(layout.telemetry, "btop || top || while sleep 5; do date; done")
    manager.send_keys(
        layout.control,
        f"python3 -m nixpulse.control --workspace {root}",
    )
    manager.send_keys(
        layout.sandbox,
        "printf '\\033[31mfatal error: zlib.h: No such file or directory\\033[0m\\n' >&2; false",
    )

    remediation = daemon.diagnose(DEMO_FAILURE, exit_code=1)
    if remediation is None:  # pragma: no cover
        raise RuntimeError("demo failure signature was not detected")
    daemon.tmux = manager
    daemon.apply_remediation(remediation, approved=True)
    if not manager.wait_for_idle(layout.sandbox):
        raise RuntimeError("sandbox did not return to an interactive shell")
    console.print(Panel(
        f"[bold green]libtmux demo war room ready[/bold green]\n"
        f"Session: {session_name}\n"
        f"Panes: sandbox={layout.sandbox.id}, telemetry={layout.telemetry.id}, control={layout.control.id}\n"
        f"Patched artifact: {shell_file}\n"
        "Model calls: 0",
        border_style="green",
    ))
    if attach:
        manager.attach()
    return shell_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--pause", type=float, default=0.0)
    parser.add_argument("--tmux", action="store_true", help="run the flow in a real libtmux session")
    parser.add_argument("--attach", action="store_true", help="attach after creating the tmux demo")
    args = parser.parse_args(argv)
    if args.tmux:
        run_tmux_demo(workspace=args.workspace, attach=args.attach)
    else:
        run_demo(workspace=args.workspace, pause=args.pause)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
