"""Interactive command-and-control deck used by the live tmux pane."""

from __future__ import annotations

import argparse
from pathlib import Path

from prompt_toolkit import prompt
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from nixpulse.daemon import NixPulseDaemon


def run_control_deck(workspace: str | Path = ".nixpulse-workspace") -> int:
    console = Console()
    daemon = NixPulseDaemon()
    console.print(Panel(
        "[bold cyan]NixPulse Command & Control[/bold cyan]\n"
        "Offline by default. Type [bold]quit[/bold] to leave.",
        border_style="cyan",
    ))
    try:
        intent = prompt("Intent> ").strip()
    except (EOFError, KeyboardInterrupt):
        return 0
    if not intent or intent.lower() in {"quit", "exit"}:
        return 0
    plan = daemon.plan(intent)
    shell_file = daemon.provision(plan, workspace)
    console.print(f"[green]Workspace ready:[/green] {shell_file}")
    console.print(f"[dim]Packages: {', '.join(plan.packages)}[/dim]")

    while True:
        try:
            failure = prompt("Paste stderr (or quit)> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if failure.lower() in {"quit", "exit"}:
            return 0
        remediation = daemon.diagnose(failure, exit_code=1)
        if remediation is None:
            console.print("[dim]No known incident signature detected.[/dim]")
            continue
        console.print(
            f"[bold red]Incident:[/bold red] {remediation.incident.summary}\n"
            f"[yellow]Proposal:[/yellow] add pkgs.{remediation.diagnosis.suggested_package}"
        )
        if remediation.diagnosis.suggested_package and Confirm.ask("Apply patch?", default=False):
            daemon.apply_remediation(remediation, approved=True)
            console.print("[green]Manifest updated. Reload the sandbox when idle.[/green]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(".nixpulse-workspace"))
    args = parser.parse_args(argv)
    return run_control_deck(args.workspace)


if __name__ == "__main__":
    raise SystemExit(main())
