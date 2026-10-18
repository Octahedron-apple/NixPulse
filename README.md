# NixPulse

NixPulse is a privacy-first, terminal-native flight deck for disposable Nix
development shells. It plans a workspace, renders a safe `shell.nix`, watches
deterministic stderr/status signals, and presents operator-approved
remediation. The Ollama/Gemma adapter is scaffolded but never called by
default.

Configured model:

```text
hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M
```

## Run the actual libtmux war room through Nix

```bash
nix-shell --run 'python3 -m nixpulse.demo'
```

For the real three-pane session, use `--tmux`. This creates panes through
`libtmux`, starts telemetry, injects a deterministic failure into the sandbox,
detects it, patches the manifest, and guardedly reloads the sandbox. It does
not call Ollama.

```bash
nix-shell --run 'python3 -m nixpulse.demo --tmux --attach'
```

Without `--attach`, the session remains running in the background and the
command prints its pane IDs:

```bash
nix-shell --run 'python3 -m nixpulse.demo --tmux'
```

The top-level command is also the live launcher. In an interactive terminal
it creates and attaches to the default `nixpulse` session:

```bash
nix-shell --run 'python3 -m nixpulse.cli'
```

The compatibility entry point is also available:

```bash
nix-shell --run 'python3 demo_flow.py --workspace /tmp/nixpulse-demo'
```

## CLI scaffold

```bash
# Show configuration without creating tmux.
nix-shell --run 'python3 -m nixpulse.cli status'

# Produce a deterministic package plan.
nix-shell --run 'python3 -m nixpulse.cli plan "Set up a Go service with SQLite and a load generator"'

# Render a validated shell.nix.
nix-shell --run 'python3 -m nixpulse.cli render /tmp/nixpulse-workspace go sqlite hey \
  --library zlib'
```

`nixpulse start` is the real tmux/Nix integration boundary. It remains offline
with respect to the model; it only uses Nix and tmux when explicitly run.

## Optional Ollama integration

Install the project dependencies, start Ollama, and pull the configured model
only when you are ready to exercise local inference:

```bash
nix-shell --run 'ollama pull hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M'
```

The only CLI path that sends a request is explicit:

```bash
nix-shell --run 'python3 -m nixpulse.cli plan --use-model "Set up a Go service with SQLite"'
```

The model returns generic package tokens in JSON. Python validates and maps
those tokens through a small allow-list before writing Nix; raw model-provided
Nix attributes are rejected.

## Tests

```bash
nix-shell --run 'python3 -m unittest discover -s tests -v'
```

See [SPEC.md](SPEC.md) for the architecture and live-demo design.
