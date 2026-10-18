"""Minimal background watcher entry point for the tmux control deck."""

from __future__ import annotations

import argparse
import time

from rich.console import Console

from nixpulse.config import settings
from nixpulse.tmux.watcher import StderrWatcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=settings.stderr_log)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    console = Console()

    def report(incident):
        console.print(
            f"[bold red]Crash detected[/bold red] {incident.kind}: "
            f"{incident.summary}"
        )

    watcher = StderrWatcher(args.source, on_incident=report)
    if args.once:
        watcher.poll_once()
        return 0
    console.print(f"[cyan]Watching[/cyan] {args.source}; Ctrl-C to stop")
    try:
        while True:
            watcher.poll_once()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
