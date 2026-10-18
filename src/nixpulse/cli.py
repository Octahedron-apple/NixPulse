"""NixPulse command-line entry point.

All commands are offline by default. The ``--use-model`` switch is the only
path that calls Ollama, which makes setup and demos safe on an unconfigured
machine.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys

from rich.console import Console
from rich.panel import Panel

from nixpulse.config import settings
from nixpulse.control import run_control_deck
from nixpulse.daemon import NixPulseDaemon
from nixpulse.demo import run_demo, run_tmux_demo
from nixpulse.llm.client import OllamaClient
from nixpulse.manifest import WorkspaceManifest, write_shell_nix
from nixpulse.tmux.manager import TmuxManager

console = Console()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nixpulse", description=__doc__)
    commands = parser.add_subparsers(dest="command")

    demo = commands.add_parser("demo", help="run the deterministic offline rehearsal")
    demo.add_argument("--workspace", type=Path)
    demo.add_argument("--pause", type=float, default=0.0)
    demo.add_argument("--tmux", action="store_true", help="create the real libtmux war room")
    demo.add_argument("--attach", action="store_true")

    plan = commands.add_parser("plan", help="turn intent into a safe package plan")
    plan.add_argument("intent")
    plan.add_argument("--use-model", action="store_true", help="explicitly call Ollama")

    render = commands.add_parser("render", help="write a validated shell.nix")
    render.add_argument("workspace", type=Path)
    render.add_argument("packages", nargs="+")
    render.add_argument("--library", action="append", default=[])

    start = commands.add_parser("start", help="provision and create the tmux war room")
    start.add_argument("intent")
    start.add_argument("--workspace", type=Path, default=Path(".nixpulse-workspace"))
    start.add_argument("--attach", action="store_true")

    deck = commands.add_parser("deck", help="run the interactive control pane")
    deck.add_argument("--workspace", type=Path, default=Path(".nixpulse-workspace"))

    commands.add_parser("status", help="show configuration without creating tmux")
    return parser


def _show_status() -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]NixPulse War Room Orchestrator[/bold cyan]\n"
            f"[green]Model:[/green] {settings.model_name}\n"
            f"[yellow]Ollama Host:[/yellow] {settings.ollama_host}\n"
            f"[magenta]Session:[/magenta] {settings.session_name}\n"
            "[dim]Inference: disabled unless --use-model is provided[/dim]",
            title="NixPulse",
            border_style="cyan",
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        # The top-level command is the actual flight-deck launcher.  Use
        # ``nixpulse status`` when a non-invasive configuration display is
        # desired.
        args = argparse.Namespace(
            command="start",
            intent="Set up a Go web service with SQLite, load testing, and telemetry",
            workspace=Path(".nixpulse-workspace"),
            attach=sys.stdin.isatty(),
        )
    if args.command == "status":
        _show_status()
        return 0
    if args.command == "demo":
        if args.tmux:
            run_tmux_demo(workspace=args.workspace, attach=args.attach)
        else:
            run_demo(workspace=args.workspace, pause=args.pause)
        return 0
    if args.command == "plan":
        client = OllamaClient(settings.ollama_host, settings.model_name) if args.use_model else None
        daemon = NixPulseDaemon(ollama=client)
        plan = daemon.plan(args.intent, use_model=args.use_model)
        console.print_json(json.dumps(plan.as_dict()))
        return 0
    if args.command == "render":
        manifest = WorkspaceManifest(
            packages=tuple(args.packages),
            libraries=tuple(args.library),
            state_dir=settings.state_dir,
        )
        path = write_shell_nix(args.workspace / "shell.nix", manifest)
        console.print(f"[green]Wrote[/green] {path}")
        return 0
    if args.command == "start":
        daemon = NixPulseDaemon()
        shell_file = daemon.provision(daemon.plan(args.intent), args.workspace)
        manager = TmuxManager(settings.session_name)
        layout = manager.create_layout(args.workspace)
        manager.send_keys(layout.telemetry, "btop || top || while sleep 5; do date; done")
        manager.send_keys(
            layout.control,
            f"python3 -m nixpulse.control --workspace {shlex.quote(str(args.workspace))}",
        )
        manager.reload_shell(layout.sandbox, shell_file)
        manager.wait_for_idle(layout.sandbox)
        console.print(f"[green]War room ready:[/green] {settings.session_name}")
        if args.attach:
            manager.attach()
        return 0
    if args.command == "deck":
        return run_control_deck(args.workspace)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
