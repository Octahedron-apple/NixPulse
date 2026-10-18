{ pkgs ? import <nixpkgs> {} }:
let
  libs = with pkgs; [  ];
in
pkgs.mkShell {
  buildInputs = with pkgs; [ go sqlite hey htop ] ++ libs;
  shellHook = ''
    export NIXPULSE_STATE_DIR='/tmp/nixpulse'
    export NIXPULSE_STDERR_LOG='/tmp/nixpulse/stderr.log'
    mkdir -p "$NIXPULSE_STATE_DIR"
    # Commands can opt into deterministic stderr capture with: 2>"$NIXPULSE_STDERR_LOG"
    nixpulse_prompt_status() {
      local status=$?
      printf '{"event":"prompt","status":%s}\n' "$status" >> "$NIXPULSE_STATE_DIR/status.log"
      return "$status"
    }
    case ";''${PROMPT_COMMAND:-};" in
      *';nixpulse_prompt_status;'*) ;;
      *) PROMPT_COMMAND="nixpulse_prompt_status''${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
    esac
  '';
}
