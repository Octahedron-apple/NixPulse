"""Interactive command-and-control deck used by the live tmux Pane 2.

Runs a dedicated, stateful TUI/REPL full-time:
- Tab completion and ghost text suggestions via prompt_toolkit.
- Calls local Gemma (via Ollama) with native tool/skill calling.
- Model has full access to the Skills Engine:
  read_file, write_script, manage_task, flake_manage, run_command, tmux_control.
- Modifies tmux war room dynamically (layouts, resizing, status messages).
- Human-in-the-loop approval dialogs before executing any mutating skills or shell reloads.
- Persistent bottom prompt bar accepting natural language developer instructions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys
import time
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from nixpulse.config import settings
from nixpulse.daemon import NixPulseDaemon
from nixpulse.skills import SkillRegistry, SkillResult


SYSTEM_PROMPT = """You are NixPulse, a terminal-native flight deck and coding agent.
You assist developers in managing Nix environments, diagnosing build/runtime errors,
inspecting workspace context, running deterministic tasks, and dynamically controlling the tmux war room.

You have access to the following tools:
- read_file: Targeted file inspection, line-range slicing, or directory trees.
- write_script: Generate validated bash scripts in .nixpulse-scripts/ with chmod +x.
- manage_task: Orchestrate deterministic tasks (build/test/bench) with bounded logs.
- flake_manage: Generate or update flake.nix (devShells) with syntax pre-flight checks.
- run_command: Send a guarded command to Pane 0 (dev shell) after checking idle prompt state.
- tmux_control: Modify the tmux session directly (action='layout' with 'main-horizontal', 'even-horizontal', 'tiled'; action='resize' with target and direction; action='notify' with message).

Always choose appropriate tool calls to fulfill the developer's instructions.
Keep your text concise and terminal-friendly."""


from prompt_toolkit.key_binding import KeyBindings

PROMPT_STYLE = Style.from_dict({
    "prompt": "ansicyan bold",
    "auto-suggestion": "#64748b italic",  # Slate ghost characters
    "completion-menu.completion": "bg:#1e293b #94a3b8",
    "completion-menu.completion.current": "bg:#38bdf8 #0f172a bold",
})


COMMAND_COMPLETIONS = [
    # Colon commands
    ":skills",
    ":tree",
    ":read",
    ":tasks",
    ":run",
    ":check",
    ":layout",
    ":resize",
    ":notify",
    ":exit",
    ":quit",
    # Skills
    "read_file",
    "write_script",
    "manage_task",
    "flake_manage",
    "run_command",
    "tmux_control",
]


class SkillAndCommandAutoSuggest(AutoSuggest):
    """Ghost text autosuggest strictly for skill names and colon commands."""

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = candidates

    def get_suggestion(self, buffer, document):
        text = document.text
        if not text:
            return None
        text_lower = text.lower()
        for candidate in self.candidates:
            if candidate.lower().startswith(text_lower) and len(candidate) > len(text):
                return Suggestion(candidate[len(text):])
        return None


class ControlDeck:
    """Stateful interactive Control Deck for Pane 2 with Tab-completion and ghost text."""

    def __init__(
        self,
        workspace: str | Path = ".nixpulse-workspace",
        *,
        tmux_manager: Any | None = None,
        daemon: NixPulseDaemon | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.console = Console()
        
        # If running inside tmux or a session exists, auto-attach to live TmuxManager
        if tmux_manager is None:
            from nixpulse.tmux.manager import TmuxManager
            session_name = settings.session_name
            try:
                tm = TmuxManager(session_name)
                if tm.session_exists():
                    tmux_manager = tm
            except Exception:
                tmux_manager = None

        self.daemon = daemon or NixPulseDaemon(tmux_manager=tmux_manager)
        self.skills = SkillRegistry(self.workspace, tmux_manager=tmux_manager)
        
        # Build candidates strictly from commands and registered skills
        skill_names = [s["name"] for s in self.skills.list_skills()]
        self.candidates = list(dict.fromkeys(COMMAND_COMPLETIONS + skill_names))

        import re
        self.completer = WordCompleter(self.candidates, ignore_case=True, pattern=re.compile(r"^[:\w]+"))
        self.auto_suggest = SkillAndCommandAutoSuggest(self.candidates)
        self.history = InMemoryHistory()

        kb = KeyBindings()

        @kb.add("tab")
        def _tab(event):
            b = event.current_buffer
            # If there is an auto-suggestion ghost text, Tab accepts it immediately
            if b.suggestion:
                b.insert_text(b.suggestion.text)
            elif b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=True)

        self.session = PromptSession(
            history=self.history,
            completer=self.completer,
            auto_suggest=self.auto_suggest,
            style=PROMPT_STYLE,
            key_bindings=kb,
            complete_while_typing=True,
        )
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def print_banner(self) -> None:
        self.console.print(
            Panel(
                "[bold cyan]NixPulse Command & Control Deck[/bold cyan] [dim](Pane 2)[/dim]\n"
                f"[dim]Model: [bold green]{self.daemon.config.model_name}[/bold green] • Host: {self.daemon.config.ollama_host}[/dim]\n"
                "[dim]Tab-to-Complete • Ghost Text (History & Suggestions) • Dynamic Tmux Control[/dim]\n"
                "Type instructions: [italic]e.g. check directory tree, switch layout to even-horizontal, add sqlite to flake[/italic]\n"
                "Commands: [yellow]:skills[/yellow], [yellow]:tree[/yellow], [yellow]:read <file>[/yellow], [yellow]:layout <name>[/yellow], [yellow]:tasks[/yellow], [yellow]:run <task>[/yellow], [yellow]:exit[/yellow]",
                title="[bold green]● LIVE AGENT[/bold green] NixPulse War Room",
                border_style="cyan",
            )
        )

    def log_event(self, category: str, message: str, style: str = "cyan") -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.console.print(f"[{style}][{timestamp}] [{category.upper()}][/{style}] {message}")

    def render_approval(self, title: str, description: str, diff_or_detail: str | None = None) -> bool:
        """Display confirmation dialog in the event stream."""
        content = f"[bold]{description}[/bold]"
        if diff_or_detail:
            content += f"\n[dim]{diff_or_detail}[/dim]"
        self.console.print(
            Panel(
                content,
                title=f"[bold yellow]⚠️ APPROVAL REQUIRED: {title}[/bold yellow]",
                border_style="yellow",
            )
        )
        return Confirm.ask("Execute action in workspace?", default=True)

    def _execute_skill_with_approval(self, name: str, args: dict[str, Any]) -> SkillResult:
        # Read-only skills execute without stopping for approval
        if name == "read_file":
            self.log_event("skill", f"Calling [cyan]read_file[/cyan]({args})")
            return self.skills.execute(name, args)

        # Tmux manipulation skills
        if name == "tmux_control":
            action = args.get("action", "")
            self.log_event("tmux", f"Model requested tmux control: {args}", "magenta")
            res = self.skills.execute(name, args)
            style = "green" if res.ok else "red"
            self.log_event("tmux", res.output, style)
            return res

        # Mutating skills require human-in-the-loop approval
        if name == "flake_manage":
            packages = args.get("packages", [])
            diff = f"Packages: {', '.join(packages)}"
            if not self.render_approval("Flake Update", "Generate/patch flake.nix", diff):
                return SkillResult(name, False, "Operation cancelled by operator.")
            res = self.skills.execute(name, args)
            self.log_event("skill", f"Flake updated: {res.output}", "green")
            if self.render_approval("Reload Shell", "Reload Pane 0 devShell with updated flake?"):
                self.skills.execute("run_command", {"command": "nix develop -c $SHELL || nix-shell || true"})
                self.log_event("tmux", "Dispatched devShell reload to Pane 0", "green")
            return res

        if name == "write_script":
            script_name = args.get("name", "script")
            if not self.render_approval("Write Script", f"Generate validated script .nixpulse-scripts/{script_name}.sh"):
                return SkillResult(name, False, "Operation cancelled by operator.")
            res = self.skills.execute(name, args)
            self.log_event("skill", res.output, "green")
            return res

        if name == "run_command":
            cmd = args.get("command", "")
            if not self.render_approval("Dev Pane Command", f"Dispatch command to Pane 0: {cmd}"):
                return SkillResult(name, False, "Operation cancelled by operator.")
            res = self.skills.execute(name, args)
            self.log_event("skill", res.output, "green")
            return res

        if name == "manage_task":
            task = args.get("task", "")
            action = args.get("action", "run")
            if action != "list" and not self.render_approval("Task Runner", f"Execute task: {task}"):
                return SkillResult(name, False, "Operation cancelled by operator.")
            res = self.skills.execute(name, args)
            style = "green" if res.ok else "red"
            self.log_event("skill", res.output, style)
            return res

        return self.skills.execute(name, args)

    def handle_intent(self, user_input: str) -> None:
        text = user_input.strip()
        if not text:
            return

        # Direct colon commands for fast manual operation
        if text.startswith(":"):
            parts = text[1:].strip().split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in {"q", "quit", "exit"}:
                raise EOFError()
            elif cmd == "skills":
                table = Table(title="Available Skills Engine Modules", border_style="cyan")
                table.add_column("Skill", style="green")
                table.add_column("Description")
                for s in self.skills.list_skills():
                    table.add_row(s["name"], s["description"])
                self.console.print(table)
                return
            elif cmd == "tree":
                res = self.skills.execute("read_file", {"path": arg or ".", "tree": True})
                self.console.print(Panel(res.output, title="Workspace Tree", border_style="dim"))
                return
            elif cmd == "read":
                if not arg:
                    self.console.print("[red]Usage: :read <relative_path>[/red]")
                    return
                res = self.skills.execute("read_file", {"path": arg, "max_lines": 50})
                self.console.print(Panel(res.output, title=f"File: {arg}", border_style="dim"))
                return
            elif cmd == "layout":
                if not arg:
                    self.console.print("[red]Usage: :layout <layout_name or natural language description>[/red]")
                    return
                # If exact layout name provided, apply directly
                if arg.lower() in {"main-horizontal", "even-horizontal", "tiled", "main-vertical", "even-vertical"}:
                    res = self.skills.execute("tmux_control", {"action": "layout", "layout": arg})
                    self.log_event("tmux", res.output, "magenta")
                    return
                # Otherwise, route through the model as natural language instruction
                text = f"Adjust tmux window layout to match: {arg}"
                self.log_event("model", f"Resolving layout request with {self.daemon.config.model_name}...")
                # Fall through to model handler below with modified text

            elif cmd == "resize":
                # :resize <direction> <cells>
                rparts = arg.split()
                direction = rparts[0] if rparts else "U"
                cells = int(rparts[1]) if len(rparts) > 1 else 5
                res = self.skills.execute("tmux_control", {"action": "resize", "direction": direction, "cells": cells})
                self.log_event("tmux", res.output, "magenta")
                return
            elif cmd == "notify":
                res = self.skills.execute("tmux_control", {"action": "notify", "message": arg or "NixPulse Alert"})
                self.log_event("tmux", res.output, "magenta")
                return
            elif cmd == "tasks":
                res = self.skills.execute("manage_task", {"action": "list"})
                self.console.print(res.output)
                return
            elif cmd == "run":
                if not arg:
                    self.console.print("[red]Usage: :run <task_name>[/red]")
                    return
                if self.render_approval("Task Execution", f"Run task: {arg}"):
                    res = self.skills.execute("manage_task", {"task": arg, "action": "run"})
                    style = "green" if res.ok else "red"
                    self.log_event("task", res.output, style=style)
                return
            elif cmd == "check":
                flake_path = self.workspace / "flake.nix"
                if not flake_path.exists():
                    self.console.print("[yellow]No flake.nix found in workspace.[/yellow]")
                else:
                    self.log_event("flake", f"Validating {flake_path.name}...")
                    from nixpulse.flake import validate_flake
                    res = validate_flake(flake_path)
                    if res.ok:
                        self.log_event("flake", "Flake syntax check PASSED", "green")
                    else:
                        self.log_event("flake", f"Check failed: {res.stderr}", "red")
                return
            else:
                self.console.print(f"[red]Unknown command: {cmd}[/red]")
                return

        # ---------------------------------------------------------------------
        # Live Model Inference with Tools
        # ---------------------------------------------------------------------
        self.log_event("model", f"Sending instruction to {self.daemon.config.model_name}...")
        self.messages.append({"role": "user", "content": text})

        try:
            tools = self.skills.as_ollama_tools()
            response = self.daemon.ollama.chat_with_tools(self.messages, tools=tools)
            tool_calls = response.get("tool_calls")
            content = response.get("content", "")

            if content:
                self.console.print(Panel(content, title=f"[cyan]{self.daemon.config.model_name}[/cyan]", border_style="cyan"))

            if tool_calls:
                for call in tool_calls:
                    fn = call.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", {})
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except Exception:
                            fn_args = {}

                    self.log_event("agent", f"Model requested skill: [bold green]{fn_name}[/bold green]")
                    result = self._execute_skill_with_approval(fn_name, fn_args)

                    # Feed result back to conversation context
                    self.messages.append({
                        "role": "tool",
                        "content": result.output,
                    })

                    self.console.print(Panel(
                        result.output,
                        title=f"Skill Result: {fn_name}",
                        border_style="green" if result.ok else "red",
                    ))
            else:
                self.messages.append({"role": "assistant", "content": content})

        except Exception as exc:
            self.log_event("fallback", f"Direct model call failed ({exc}); falling back to deterministic intent plan.", "yellow")
            plan = self.daemon.plan(text, use_model=False)
            self.log_event("plan", f"Inferred package set: [green]{', '.join(plan.packages)}[/green]")
            if self.render_approval("Workspace Provision", f"Provision environment with {', '.join(plan.packages)}"):
                shell_file = self.daemon.provision(plan, self.workspace)
                self.skills.execute("flake_manage", {"packages": list(plan.packages)})
                self.log_event("provision", f"Generated {shell_file.name} & flake.nix", "green")
                if self.daemon.tmux and self.daemon.tmux.layout:
                    self.daemon.tmux.reload_shell(self.daemon.tmux.layout.sandbox, shell_file)
                    self.log_event("tmux", "Reloaded Pane 0 devShell", "green")

    def run(self) -> int:
        self.print_banner()
        while True:
            try:
                user_input = self.session.prompt(
                    HTML("<ansicyan><b>Pane2:ControlDeck</b></ansicyan> &gt; ")
                )
            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[dim]Leaving Control Deck...[/dim]")
                break

            try:
                self.handle_intent(user_input)
            except EOFError:
                break
            except Exception as exc:
                self.console.print(f"[bold red]Deck Error:[/bold red] {exc}")

        return 0


def run_control_deck(workspace: str | Path = ".nixpulse-workspace", tmux_manager: Any | None = None) -> int:
    deck = ControlDeck(workspace, tmux_manager=tmux_manager)
    return deck.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(".nixpulse-workspace"))
    args = parser.parse_args(argv)
    return run_control_deck(args.workspace)


if __name__ == "__main__":
    raise SystemExit(main())
