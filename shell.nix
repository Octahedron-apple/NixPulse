{ pkgs ? import <nixpkgs> {} }:

let
  # Use pkgs.python3 (or pkgs.python312) instead of python311
  pythonEnv = pkgs.python3.withPackages (ps: with ps; [
    libtmux
    rich
    prompt-toolkit
    requests
    pydantic
  ]);

  demoPackages = with pkgs; [
    go
    sqlite
    hey
    gcc
    zlib
    pkg-config
    gnumake
  ];

  systemTools = with pkgs; [
    tmux
    btop
    htop
    ollama
    nix-index
    jq
    curl
  ];

in
pkgs.mkShell {
  name = "nixpulse-env";

  buildInputs = [ pythonEnv ] ++ demoPackages ++ systemTools;

  shellHook = ''
    export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath demoPackages}:$LD_LIBRARY_PATH"
    export OLLAMA_HOST="127.0.0.1:11434"
    export LC_ALL="C.UTF-8"
    export LANG="C.UTF-8"

    export NIXPULSE_STATE_DIR="''${XDG_RUNTIME_DIR:-/tmp}/nixpulse"
    export NIXPULSE_STDERR_FIFO="$NIXPULSE_STATE_DIR/stderr.pipe"
    export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
    mkdir -p "$NIXPULSE_STATE_DIR"

    echo "=========================================================="
    echo "  NixPulse Ephemeral War Room Environment Initialized     "
    echo "  Python: $(python3 --version) | tmux: $(tmux -V)         "
    echo "  Target Model: hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M (Ollama @ $OLLAMA_HOST) "
    echo "=========================================================="
  '';
}
