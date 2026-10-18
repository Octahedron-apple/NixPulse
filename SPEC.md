# SPEC: NixPulse — Autonomous Ephemeral War Room (Hacktoberfest '26)

## 1. Executive Summary & Vision
- **Project Name:** NixPulse (or FlakeOps)
- **One-Line Pitch:** A zero-cloud, terminal-native developer flight deck that dynamically orchestrates isolated Nix sandboxes, monitors live processes via `tmux`, and autonomously diagnoses and patches build/runtime errors using local Gemma.
- **Target Hackathon Tracks:** Open Source AI & Google Gemma Track.
- **Core Philosophy:** Privacy-first, zero global host pollution, high live-demo visual appeal.

---

## 2. Hardware Environment & Model Constraints
- **Host Specs:** AMD Ryzen 7 5800HS (8c/16t @ 4.46 GHz), NVIDIA GeForce RTX 3050 Mobile (4 GB VRAM).
- **Target Inference Rate:** 25–45+ tokens/second.
- **Model Strategy:**
   - **Primary Model Choice:** `hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M` via Ollama.
   - **Invocation policy:** The scaffold never calls Ollama automatically; live inference is an explicit operator action.
  - **Structured Decoding:** Ollama JSON mode (`format: "json"`) enforced with strict Pydantic schemas, tight system prompts, and an internal lookup map for popular `nixpkgs` targets to eliminate attribute hallucination.
   - **Context Capping:** Capped via `PARAMETER num_ctx 2048` in Ollama to conserve memory and preserve sub-second response times.
   - **Warm Cache Defense:** Pre-populate local `/nix/store` with demo packages (`go`, `sqlite`, `hey`, `gcc`, `zlib`, `pkg-config`, `gnumake`) to eliminate network latency.

### 2.1 Benchmark Reference Scores

The following are the official reference scores for the **base
`gemma-4-E2B-it` model** across standard evaluation suites. They are not
separate official tables for the `Q4_K_M` GGUF; formal benchmark reporting is
performed against the FP16/BF16 weights.

| Benchmark | Evaluation focus | Official reference |
| :--- | :--- | ---: |
| **MMMLU** | Multilingual, multi-task language understanding | **67.0%** |
| **MMLU-Pro** | Multi-step reasoning and knowledge across 10 options | **60.0%** |
| **MATH-Vision** | Multimodal visual-mathematical problem solving | **52.0%** |
| **MMMU-Pro** | Rigorous multimodal reasoning and perception | **44.0%** |

> **Quantization note:** Standard 4-bit medium k-quants such as `Q4_K_M`
> generally remain within an approximately 1–2 percentage-point margin of
> the corresponding full-precision reference in validation tests. These
> figures are a practical expectation, not a replacement for an official
> per-quant benchmark table.

### 2.2 Local Runtime Measurements

These measurements describe the target NixPulse presentation hardware and
the configured Ollama GGUF model:

| Runtime metric | Local measurement |
| :--- | ---: |
| **Eval generation rate** | **73.81 tokens/s** |
| **Prompt processing / prefill** | **232.53 tokens/s** |
| **VRAM allocation** | **1.7 GB** |
| **GPU offload** | **100%** |

The approximately 1.7 GB quantized model and its capped context fit within the
4 GB RTX 3050 Mobile VRAM budget. Full GPU offload avoids layer transfers over
PCIe during generation. With roughly 192 GB/s of mobile GPU memory bandwidth,
the observed 73.81 tokens/s is suitable for short, structured JSON triage
requests during a live demo.

**Measurement boundary:** These are local session metrics, not official model
quality scores. Re-run the throughput measurement after changing the Ollama
version, GPU driver, quantization, context size, or offload settings.

---

## 3. High-Level Architecture & Layout

```
+----------------------------------------------------------------------+
| tmux Session: nixpulse                                               |
| +----------------------------------+-------------------------------+ |
| | Pane 0: Sandboxed Shell          | Pane 1: Telemetry & Monitoring| |
| | (Managed via dynamic shell.nix;  | (btop / live resource gauges) | |
| |  guarded reload via nix-shell)   |                               | |
| +----------------------------------+-------------------------------+ |
| | Pane 2: Command & Control Deck                                   | |
| | (Natural-language intent, triage alerts, interactive y/N prompts)| |
+----------------------------------------------------------------------+
         ^                                      |
         | libtmux send_keys & state reload     | captures terminal/FIFO output
         |                                      v
+----------------------------------------------------------------------+
| Background Daemon: nixpulse-daemon                                   |
| - ANSI-stripped buffer poller & crash signature matcher              |
| - PROMPT_COMMAND / FIFO stderr capture hook                          |
| - Manifest state manager (in-memory package store -> shell.nix)      |
| - Local Ollama Gemma inference client (structured JSON)              |
+----------------------------------------------------------------------+
```

### Component Breakdown
1. **Pane 0 (Sandboxed Dev Environment):** Dynamic development sandbox running in `nix-shell` generated by the daemon.
2. **Pane 1 (Telemetry & Monitoring):** Real-time resource utilization via `btop`/`htop` or live metric pipes.
3. **Pane 2 (Command & Control Deck):** Interactive terminal UI built with `rich` and `prompt_toolkit` handling natural language intent and interactive confirmation dialogs.
4. **Daemon Core (`nixpulse-daemon` / background thread):**
   - In-memory manifest generator.
   - FIFO/log watcher for exit codes and stderr streams.
   - Ollama Gemma inference client.
   - Guarded reloader invoking `nix-instantiate` and `nix-shell` refresh.

---

## 4. Subsystems & Technical Implementation

### A. Dynamic Manifest Generation & Initial Provisioning
1. User provides natural language intent into Pane 2 (e.g. *"Set up an environment to test this Go web service and benchmark it against SQLite with an interactive load generator"*).
2. Local Gemma parses the intent into a constrained JSON payload:
   ```json
   {
     "packages": ["go", "sqlite", "hey", "htop"],
     "layout": "split_h",
     "start_command": "clear"
   }
   ```
3. Python maps generic package tokens against a deterministic whitelist / lookup dictionary:
   - `"zlib"` -> `pkgs.zlib`
   - `"sqlite"` -> `pkgs.sqlite`
   - `"go"` -> `pkgs.go`
   - `"load-test"` / `"hey"` -> `pkgs.hey`
4. The daemon writes the initial `shell.nix`:
   ```nix
   { pkgs ? import <nixpkgs> {} }:
   let
     libs = with pkgs; [ zlib ];
   in
   pkgs.mkShell {
     buildInputs = with pkgs; [ go sqlite hey htop ] ++ libs;
     shellHook = ''
       export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath libs}:$LD_LIBRARY_PATH"
     '';
   }
   ```
5. `libtmux` tiles the session, assigns directories, and launches `nix-shell` in Pane 0.

---

### B. Daemon Log Scraper & Telemetry Interception
- **Deterministic Hooking:** Rather than scraping raw ANSI characters from the visual screen, Pane 0's shell registers a `PROMPT_COMMAND` trap capturing `$?` (last exit code) and redirecting command `stderr` to `$NIXPULSE_STDERR_FIFO` (`/tmp/nixpulse/stderr.pipe` or log file).
- **ANSI Stripping:** Any visual buffer fallback is sanitized through regex filters to remove VT100 / cursor sequences.
- **Crash Signature Matching:** Flags deterministic compiler/runtime failures:
  - Missing headers (`fatal error: *.h: No such file or directory`)
  - Linker errors (`cannot find -lz`, `undefined reference to`)
  - Missing binaries (`command not found`, exit code 127)
  - Port collisions (`EADDRINUSE`)
- **Aggressive Truncation:** Retains trailing 25–35 lines (< 400 tokens) to guarantee zero Ollama context overflow on `num_ctx 2048`.

---

### C. Safe Hot-Patching & Guarded Shell Refresh
1. **Error Extraction:** The daemon isolates the stack trace and feeds the sanitized prompt to Gemma.
2. **Deterministic Resolution:** Gemma returns JSON:
   ```json
   {
     "missing_library": "zlib",
     "suggested_package": "zlib",
     "explanation": "Compilation failed due to missing zlib.h header."
   }
   ```
3. **Control Prompt in Pane 2:**
   ```
   [CRASH DETECTED] Missing zlib headers.
   Add 'pkgs.zlib' and reload shell? [Y/n]
   ```
4. **Pre-flight Nix Evaluation:**
   Before applying changes, the daemon validates the syntax:
   ```bash
   nix-instantiate --parse shell.nix
   ```
5. **Guarded Shell Reload:**
   The daemon verifies Pane 0 is idle (`#{pane_current_command}` is `bash`/`zsh`). To prevent pane suicide if an error ever occurred, it dispatches:
   ```bash
   nix-shell || echo "Nix reload failed; maintaining current shell."
   ```

---

## 5. Architectural Gotchas & Battle-Tested Defenses

| Failure Mode | Root Cause | NixPulse Defense |
| :--- | :--- | :--- |
| **Model Hallucination** | Small 2B models invent invalid Nix attribute paths (e.g. `pkgs.zlib-dev`). | Model only emits canonical names; Python maps via deterministic `NIXPKGS_ATTR_MAP`. |
| **Pane Suicide** | `exec nix-shell` exits on error, closing the tmux pane mid-demo. | Run `nix-instantiate --parse` preflight + execute guarded fallback `nix-shell \|\| echo ...`. |
| **ANSI / Scraper Drift** | Fullscreen tools (`btop`) or escape sequences scramble parser. | Use `PROMPT_COMMAND` / `$NIXPULSE_STDERR_FIFO` deterministic trap instead of visual scraping. |
| **Context Overflow** | Massive C/Go build logs exceed `num_ctx 2048`. | Truncate to trailing 30 lines / 400 tokens before sending to Ollama. |
| **Keystroke Collision** | `send_keys` fires while user or process is typing in Pane 0. | Pane 2 requires interactive user confirmation (`[Y/n]`) before dispatching to Pane 0. |
| **Dynamic Linker Failure** | Headers present at compile time, but `.so` missing at runtime. | Template exports `LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath libs}:$LD_LIBRARY_PATH"` in `shellHook`. |

---

## 6. Project Layout

```
MLH/
├── shell.nix                     # Nix development & demo environment
├── pyproject.toml                # Standard Python package manifest
├── SPEC.md                       # This architecture specification
├── README.md                     # Quickstart & demo guide
├── tdemo.py                      # Original dynamic tmux reference script
└── src/
    └── nixpulse/
        ├── __init__.py           # Version metadata
        ├── config.py             # Settings (Ollama host, models, paths)
        ├── cli.py                # Command & Control Deck (Pane 2 interface)
        ├── control.py            # Interactive operator approval loop
        ├── daemon.py             # Background watcher and orchestrator
        ├── daemon_cli.py         # FIFO/log polling entry point
        ├── manifest.py           # In-memory Nix manifest generator & validator
        ├── llm/
        │   ├── client.py         # Ollama HTTP client with JSON mode
        │   └── prompts.py        # System prompts & schema definitions
        └── tmux/
            ├── manager.py        # libtmux session, pane, and layout controller
            └── watcher.py        # Pane state and FIFO/stderr monitor
```

---

## 7. Live Demo Script (2-Minute Judging Walkthrough)

- **0:00 – 0:30 (Workspace Setup):**
  Launch `nixpulse`. User types intent into Pane 2: *"Setup Go web service with SQLite and load tester"*. Tmux automatically tiles (Panes 0, 1, 2), loads `btop`, generates `shell.nix`, and initializes Pane 0 instantaneously from cached `/nix/store`.
- **0:30 – 1:15 (Deterministic Failure Trigger):**
  Run a build command requiring `zlib.h` in Pane 0 (`gcc -lz -o test test.c`). The failure is captured via the stderr hook. Pane 2 flags the incident within ~1–2 seconds with Gemma’s diagnosis and remediation recommendation.
- **1:15 – 1:45 (In-Place Hot Patch):**
  User presses `Enter` in Pane 2. Daemon validates `shell.nix` via `nix-instantiate`, updates `shellHook` with `LD_LIBRARY_PATH`, and issues guarded reload in Pane 0. Re-running the command succeeds immediately.
- **1:45 – 2:00 (Pitch Close):**
  Reiterate zero-cloud, 100% local inference on RTX 3050 Mobile, reproducible isolation, and instant recovery.
