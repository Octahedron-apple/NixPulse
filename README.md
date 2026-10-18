# NixPulse

NixPulse is a terminal-native, privacy-first coding flight deck and autonomous agent for Nix development environments. It combines live three-pane `libtmux` orchestration, real-time system telemetry, and a stateful Command & Control Deck with an extensible Skills Engine powered by local Gemma (via Ollama) or cloud models via OpenRouter.

```text
┌─────────────────────────────────────────────────────────────┐
│                       Pane 0: DevShell                      │
│      Isolated Nix sandbox running bash with live toolchain  │
├──────────────────────────────┬──────────────────────────────┤
│    Pane 1: Telemetry (btop)  │    Pane 2: Control Deck      │
│  Real-time CPU/mem resource  │  Interactive agent REPL with │
│  monitoring & incident triage│  Skills Engine & ghost text  │
└──────────────────────────────┴──────────────────────────────┘
```

---

## Key Features

- **Autonomous Agent Flight Deck**: Interactive REPL with ghost text suggestions and Tab completion strictly focused on skills and commands.
- **Skills Engine**:
  - `list_files` / `:ls`: Workspace directory inspection.
  - `read_file` / `:read`: Targeted file reading, line slicing, or directory trees (`:tree`).
  - `write_file`: Create and edit workspace source and config files.
  - `write_script`: Generate validated executable bash scripts in `.nixpulse-scripts/`.
  - `manage_task` / `:tasks` / `:run`: Workspace task runner parsing `Makefile`, `justfile`, `tasks.yaml`, and `tasks.json`.
  - `flake_manage`: Generate and patch `flake.nix` devShells with pre-flight syntax checks.
  - `run_command`: Send guarded shell commands to the dev pane with idle-state verification.
  - `tmux_control` / `:layout`: Dynamic window layouts (`even-horizontal`, `main-horizontal`, `tiled`), pane splitting, and resizing.
- **Live Background Crash Watcher**: Monitors the dev shell for compiler errors (e.g. missing C headers, missing commands) and automatically offers one-click package remediation.
- **Impure by Default**: Dev shell inherits host utilities (like `micro`, `git`, and system configs) while tracking packages cleanly in Nix.
- **Live CWD Synchronization**: The agent automatically follows whatever directory you `cd` into inside the dev shell.
- **Dual Inference Engine**: Run completely offline with local Ollama (`hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M`) or with cloud models via OpenRouter (`google/gemma-4-26b-a4b-it:free`).

---

## Getting Started

### 1. Launch a Clean Vanilla Sandbox (Recommended)

Start an impure sandbox attached to the war room:

```bash
# Using local Ollama
nix-shell --run 'python3 -m nixpulse.cli start --vanilla --attach'

# Using OpenRouter (e.g. google/gemma-4-26b-a4b-it:free)
nix-shell --run 'python3 -m nixpulse.cli start --openrouter --vanilla --attach'
```

### 2. Launch with an Initial Intent

Provision an environment pre-seeded with tools:

```bash
nix-shell --run 'python3 -m nixpulse.cli start "Set up a C environment with zlib" --attach'
```

### 3. Open Only the Interactive Control Deck

If you just want to run the REPL directly:

```bash
nix-shell --run 'python3 -m nixpulse.cli deck'
```

---

## Interactive Control Deck Commands

| Command | Action |
| :--- | :--- |
| *Natural Language* | Ask the agent directly: <br>• `"fix the c file"`<br>• `"write a script to benchmark the web service"`<br>• `"change layout to even-horizontal"`<br>• `"add sqlite to the flake"` |
| `:skills` | List all available Skills Engine modules. |
| `:ls` | List files and folders in the active workspace directory. |
| `:tree` | Display the directory hierarchy tree. |
| `:read <file>` | Inspect a file. |
| `:tasks` | List configured tasks in `justfile`, `Makefile`, `tasks.yaml`, or `tasks.json`. |
| `:run <task>` | Execute a configured task through the deterministic runner. |
| `:layout <name>` | Change tmux layout (`even-horizontal`, `main-horizontal`, `tiled`, `main-vertical`). |
| `:check` | Validate `flake.nix` syntax. |
| `:exit` | Exit the Control Deck. |

---

## Running Tests

All 21 comprehensive test suites (including live tool execution) can be run inside `nix-shell`:

```bash
nix-shell --run 'python3 -m unittest discover tests'
```
