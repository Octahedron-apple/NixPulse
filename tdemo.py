import subprocess
import threading
import time

SESSION = "dynamic-demo"

def tmux(*args):
    """Run a tmux command safely."""
    return subprocess.run(["tmux", *args], capture_output=True, text=True)

def orchestrate():
    """Background controller that drives the layout changes while you watch."""
    time.sleep(1.5)

    # 1. Split Pane 0 vertically (creates Pane 1 at the bottom)
    tmux("display-message", "-t", SESSION, "Splitting pane vertically...")
    pane_1 = tmux("split-window", "-v", "-PF", "#{pane_id}", "-t", f"{SESSION}:0.0").stdout.strip()
    tmux("send-keys", "-t", pane_1, 'echo ">> New bottom pane spawned"; for i in {1..30}; do echo "Worker task $i"; sleep 0.1; done', "C-m")
    time.sleep(2.5)

    # 2. Split right side horizontally (creates Pane 2 on the right)
    tmux("display-message", "-t", SESSION, "Splitting pane horizontally...")
    pane_2 = tmux("split-window", "-h", "-PF", "#{pane_id}", "-t", pane_1).stdout.strip()
    tmux("send-keys", "-t", pane_2, 'echo ">> Telemetry pane active"; while true; do echo "[LIVE] $(date +%T.%N)"; sleep 0.15; done', "C-m")
    time.sleep(2.5)

    # 3. Resize Pane 2 dynamically (animate widening)
    tmux("display-message", "-t", SESSION, "Resizing panes...")
    for _ in range(8):
        tmux("resize-pane", "-t", pane_2, "-R", "3")
        time.sleep(0.1)
    time.sleep(1.5)

    # 4. Kill the bottom-left worker pane (closing it)
    tmux("display-message", "-t", SESSION, "Killing worker pane...")
    tmux("kill-pane", "-t", pane_1)
    time.sleep(2.0)

    # 5. Open an ephemeral top-right alert pane
    tmux("display-message", "-t", SESSION, "Opening ephemeral alert pane...")
    pane_alert = tmux("split-window", "-v", "-PF", "#{pane_id}", "-t", pane_2).stdout.strip()
    tmux("send-keys", "-t", pane_alert, 'echo "=== CRITICAL LOG ALERT ==="; sleep 2; echo "Closing self..."; sleep 1; exit', "C-m")
    time.sleep(4.0)

    # 6. Cycle layouts
    tmux("display-message", "-t", SESSION, "Cycling layout to even-horizontal...")
    tmux("select-layout", "-t", SESSION, "even-horizontal")
    time.sleep(2.0)

    tmux("display-message", "-t", SESSION, "Closing remaining side pane...")
    tmux("kill-pane", "-t", pane_2)
    time.sleep(1.0)

    tmux("display-message", "-t", SESSION, "Sequence complete!")

def main():
    # Clean up any lingering session with this name
    tmux("kill-session", "-t", SESSION)

    # Create session detached first
    tmux("new-session", "-d", "-s", SESSION, "-n", "orchestrator")
    tmux("send-keys", "-t", f"{SESSION}:0.0", 'clear; echo "=== MASTER CONTROLLER PANE ==="; while true; do echo "Master idle $(date +%T)"; sleep 0.5; done', "C-m")

    # Start the automation thread
    thread = threading.Thread(target=orchestrate, daemon=True)
    thread.start()

    # Attach the terminal foreground to the session immediately
    subprocess.run(["tmux", "attach-session", "-t", SESSION])

if __name__ == "__main__":
    main()
