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
- list_files: List files and folders in workspace (equivalent to 'ls'). Always use this first if you do not know which files exist!
- read_file: Targeted file inspection, line-range slicing, or directory trees.
- write_file: Create or edit project files (e.g. source files like test.c, config files, Makefiles, justfiles).
- write_script: Generate validated bash scripts in .nixpulse-scripts/ with chmod +x.
- manage_task: Orchestrate deterministic tasks (build/test/bench) with bounded logs.
- flake_manage: Generate or update flake.nix (devShells) with syntax pre-flight checks.
- run_command: Send a guarded command to Pane 0 (dev shell) after checking idle prompt state.
- tmux_control: Modify the tmux session directly:
  * action='split': Use when the user asks to "add more panes", "spawn pane", "split pane", or "open another terminal".
  * action='layout': Use when the user asks to arrange existing panes (e.g. 'main-horizontal', 'even-horizontal', 'tiled', 'main-vertical').
  * action='resize': Adjust pane dimensions (direction='U'/'D'/'L'/'R', cells=number).
  * action='notify': Flash a message in tmux status bar.

When the developer asks you to fix or examine code (e.g. 'fix the c file'):
1. If the exact file name isn't stated, call list_files to see what files exist (e.g. find test.c).
2. Read the file with read_file.
3. Propose and apply the fix with write_file.
Always choose the appropriate tool call to fulfill the developer's instructions.
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
    ":ls",
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
    "list_files",
    "read_file",
    "write_file",
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
            from nixpulse.tmux.manager import TmuxManager, PaneLayout
            session_name = settings.session_name
            try:
                tm = TmuxManager(session_name)
                if tm.session_exists():
                    sess = tm._existing_session()
                    if sess and sess.windows:
                        win = sess.windows[0]
                        panes = list(win.panes)
                        if len(panes) >= 3:
                            tm.layout = PaneLayout(panes[0], panes[1], panes[2])
                    tmux_manager = tm
            except Exception:
                tmux_manager = None

        self.daemon = daemon or NixPulseDaemon(tmux_manager=tmux_manager)
        
        # If tmux sandbox pane exists, lock workspace to its live CWD
        if tmux_manager and tmux_manager.layout and tmux_manager.layout.sandbox:
            pane_cwd = tmux_manager.get_pane_cwd(tmux_manager.layout.sandbox)
            if pane_cwd and pane_cwd.is_dir():
                self.workspace = pane_cwd

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

        # Active background incident watcher on dev shell (Pane 0)
        self._seen_incidents: set[str] = set()
        self._pending_incident = None
        self._watcher_running = True
        import threading
        self._watcher_thread = threading.Thread(target=self._pane0_incident_watcher, daemon=True)
        self._watcher_thread.start()

    def _pane0_incident_watcher(self) -> None:
        """Continuously check Pane 0 buffer for compiler/runtime failure signatures."""
        import time
        from nixpulse.tmux.watcher import detect_incident
        while self._watcher_running:
            try:
                time.sleep(1.0)
                if not self.skills.tmux_manager or not self.skills.tmux_manager.layout:
                    continue
                sandbox = self.skills.tmux_manager.layout.sandbox
                content = self.skills.tmux_manager.capture_pane(sandbox, lines=30)
                if not content:
                    continue
                incident = detect_incident(content)
                if incident:
                    import hashlib
                    fingerprint = hashlib.sha256(incident.evidence.encode()).hexdigest()
                    if fingerprint not in self._seen_incidents:
                        self._seen_incidents.add(fingerprint)
                        # Queue incident cleanly to avoid concurrent stdin reads with prompt_toolkit
                        self._pending_incident = incident
                        # If prompt_toolkit is currently waiting, wake it up cleanly:
                        try:
                            if hasattr(self.session, "app") and self.session.app.is_running:
                                self.session.app.exit(result="__INCIDENT_DETECTED__")
                        except Exception:
                            pass
            except Exception:
                pass

    def check_pending_incidents(self) -> None:
        """Process any pending crash incidents from the main thread where stdin is owned."""
        if not self._pending_incident:
            return
        incident = self._pending_incident
        self._pending_incident = None

        self.console.print("\n")
        self.log_event("crash", f"[bold red]Compiler/runtime crash detected in Pane 0![/bold red]", "red")
        self.log_event("crash", f"Evidence: [yellow]{incident.summary}[/yellow]", "yellow")
        
        remediation = self.daemon.diagnose(incident.evidence, use_model=True)
        if remediation and remediation.diagnosis.suggested_package:
            pkg = remediation.diagnosis.suggested_package
            expl = remediation.diagnosis.explanation or f"Missing dependency {pkg}"
            if self.render_approval(
                f"Auto-Remediate Crash ({incident.kind})",
                f"Add pkgs.{pkg} to environment?\nExplanation: {expl}",
                diff_or_detail=f"Package: {pkg}\nEvidence: {incident.evidence}"
            ):
                # Update flake and shell
                self.skills.execute("flake_manage", {"packages": [pkg], "build_inputs": [pkg]})
                self.log_event("skill", f"Added pkgs.{pkg} to workspace flake.nix", "green")
                self.skills.execute("run_command", {"command": "nix develop -c $SHELL || nix-shell || true"})
                self.log_event("tmux", "Guarded reload dispatched to Pane 0", "green")

    def print_banner(self) -> None:
        model_name = self.daemon.ollama.model if self.daemon.ollama else self.daemon.config.model_name
        host_name = self.daemon.ollama.base_url if self.daemon.ollama else self.daemon.config.ollama_host
        self.console.print(
            Panel(
                "[bold cyan]NixPulse Command & Control Deck[/bold cyan]\n"
                f"[dim]Model: [bold green]{model_name}[/bold green] • Host: {host_name}[/dim]\n"
                "[dim]Tab-to-Complete • Ghost Text (History & Suggestions) • Dynamic Tmux Control[/dim]\n"
                "Type instructions: [italic]e.g. check directory tree, switch layout to even-horizontal, add sqlite to flake[/italic]\n"
                "Commands: [yellow]:skills[/yellow], [yellow]:ls[/yellow], [yellow]:tree[/yellow], [yellow]:read <file>[/yellow], [yellow]:layout <name>[/yellow], [yellow]:tasks[/yellow], [yellow]:run <task>[/yellow], [yellow]:exit[/yellow]",
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
        if name in {"read_file", "list_files"}:
            self.log_event("skill", f"Calling [cyan]{name}[/cyan]({args})")
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

        if name == "write_file":
            target_path = args.get("path", "file")
            content = args.get("content", "")
            preview = content[:200] + ("..." if len(content) > 200 else "")
            if not self.render_approval("Write File", f"Write to {target_path}", preview):
                return SkillResult(name, False, "Operation cancelled by operator.")
            res = self.skills.execute(name, args)
            self.log_event("skill", res.output, "green")
            return res

        if name == "write_script":
            script_name = str(args.get("name", "script")).strip()
            if script_name.endswith(".sh"):
                script_name = script_name[:-3]
            target_rel = Path(".nixpulse-scripts") / f"{script_name}.sh"
            if not self.render_approval("Write Script", f"Generate validated script {target_rel}"):
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

    def sync_workspace_with_pane(self) -> None:
        """Keep agent strictly locked to the dev shell's live current working directory."""
        if self.skills.tmux_manager and self.skills.tmux_manager.layout and self.skills.tmux_manager.layout.sandbox:
            pane_cwd = self.skills.tmux_manager.get_pane_cwd(self.skills.tmux_manager.layout.sandbox)
            if pane_cwd and pane_cwd.is_dir() and pane_cwd != self.workspace:
                self.workspace = pane_cwd
                self.skills = SkillRegistry(self.workspace, tmux_manager=self.skills.tmux_manager)
                self.log_event("workspace", f"Agent workspace locked to live dev shell CWD: {pane_cwd}", "cyan")

    def handle_intent(self, user_input: str) -> None:
        self.sync_workspace_with_pane()
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
            elif cmd == "ls":
                res = self.skills.execute("list_files", {"path": arg or "."})
                self.console.print(Panel(res.output, title=f"Directory Listing: {arg or '.'}", border_style="dim"))
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
                text = arg
                self.log_event("model", f"Resolving instruction with {self.daemon.config.model_name}: {arg}")
                # Fall through to model handler below with user text

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
            
            # Agent loop: allow multi-turn tool execution until model produces final answer
            max_turns = 5
            for turn in range(max_turns):
                response = self.daemon.ollama.chat_with_tools(self.messages, tools=tools)
                tool_calls = response.get("tool_calls")
                content = response.get("content", "")

                if content:
                    self.console.print(Panel(content, title=f"[cyan]{self.daemon.config.model_name}[/cyan]", border_style="cyan"))

                if not tool_calls:
                    self.messages.append({"role": "assistant", "content": content})
                    break

                # Append assistant's turn with tool_calls so the conversation structure is valid
                self.messages.append({
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": tool_calls,
                })

                # Deduplicate identical tool calls in the same turn
                seen_calls_in_turn = set()
                for call in tool_calls:
                    call_id = call.get("id", f"call_{turn}")
                    fn = call.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", {})
                    if isinstance(fn_args, str):
                        try:
                            fn_args = json.loads(fn_args)
                        except Exception:
                            fn_args = {}

                    call_key = (fn_name, json.dumps(fn_args, sort_keys=True))
                    if call_key in seen_calls_in_turn:
                        continue
                    seen_calls_in_turn.add(call_key)

                    self.log_event("agent", f"Model requested skill: [bold green]{fn_name}[/bold green]")
                    result = self._execute_skill_with_approval(fn_name, fn_args)

                    # Feed result back to conversation context
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result.output,
                    })

                    self.console.print(Panel(
                        result.output,
                        title=f"Skill Result: {fn_name}",
                        border_style="green" if result.ok else "red",
                    ))

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
            self.check_pending_incidents()
            try:
                user_input = self.session.prompt(
                    HTML("<ansicyan><b>NixPulse</b></ansicyan> &gt; ")
                )
            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[dim]Leaving Control Deck...[/dim]")
                break

            if user_input == "__INCIDENT_DETECTED__":
                self.check_pending_incidents()
                continue

            try:
                self.handle_intent(user_input)
            except EOFError:
                break
            except Exception as exc:
                self.console.print(f"[bold red]Deck Error:[/bold red] {exc}")

        return 0


def run_control_deck(
    workspace: str | Path = ".nixpulse-workspace",
    tmux_manager: Any | None = None,
    *,
    model: str = "",
    openrouter: bool = False,
) -> int:
    client = None
    if openrouter or model:
        m_name = model or "google/gemma-4-26b-a4b-it:free"
        host = "https://openrouter.ai" if (openrouter or "openrouter.ai" in m_name) else settings.ollama_host
        api_key = settings.api_key if (openrouter or "openrouter.ai" in host) else ""
        from nixpulse.llm.client import OllamaClient
        client = OllamaClient(host=host, model=m_name, api_key=api_key)
    
    daemon = NixPulseDaemon(ollama=client) if client else None
    deck = ControlDeck(workspace, tmux_manager=tmux_manager, daemon=daemon)
    return deck.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(".nixpulse-workspace"))
    parser.add_argument("--model", type=str, default="")
    parser.add_argument("--openrouter", action="store_true")
    args = parser.parse_args(argv)
    return run_control_deck(args.workspace, model=args.model, openrouter=args.openrouter)


if __name__ == "__main__":
    raise SystemExit(main())
