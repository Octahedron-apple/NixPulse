import tempfile
import unittest
from pathlib import Path

from nixpulse.tmux.manager import TmuxError, TmuxManager


class FakePane:
    counter = 0

    def __init__(self):
        self.id = f"%fake{FakePane.counter}"
        FakePane.counter += 1
        self.pane_current_command = "bash"
        self.sent = []

    def send_keys(self, command, enter=True):
        self.sent.append((command, enter))

    def capture_pane(self, start=None):
        return ["captured output"]


class FakeWindow:
    def __init__(self):
        self.panes = [FakePane()]
        self.layout = None

    def split(self, *, target=None, start_directory=None, direction=None):
        pane = FakePane()
        self.panes.append(pane)
        return pane

    def select_layout(self, layout):
        self.layout = layout


class FakeSession:
    def __init__(self):
        self.windows = [FakeWindow()]


class FakeSessions:
    def __init__(self):
        self.session = None

    def get(self, default=None, **kwargs):
        return self.session if self.session is not None else default


class FakeServer:
    def __init__(self):
        self.sessions = FakeSessions()
        self.created = None

    def has_session(self, name, exact=True):
        return self.sessions.session is not None

    def new_session(self, **kwargs):
        self.created = FakeSession()
        self.sessions.session = self.created
        return self.created

    def attach_session(self, name):
        return None

    def kill_session(self, name):
        self.sessions.session = None


class TmuxManagerTests(unittest.TestCase):
    def test_libtmux_layout_and_guarded_reload(self):
        server = FakeServer()
        manager = TmuxManager("test-session", server=server)
        with tempfile.TemporaryDirectory() as directory:
            layout = manager.create_layout(directory)
            manager.preflight = lambda path: None
            manager.reload_shell(layout.sandbox, Path(directory) / "shell.nix")
        self.assertEqual(server.created.windows[0].layout, "main-horizontal")
        self.assertIn("nix-shell --pure --run 'bash -i' ||", layout.sandbox.sent[0][0])
        self.assertIn("env -u NIX_SHELL", layout.sandbox.sent[0][0])
        self.assertIn("-u NIX_BUILD_TOP", layout.sandbox.sent[0][0])
        self.assertNotIn("exec nix-shell", layout.sandbox.sent[0][0])
        self.assertEqual(manager.capture_pane(layout.control), "captured output")

    def test_newlines_are_not_sent_to_a_pane(self):
        manager = TmuxManager("test-session", server=FakeServer())
        with self.assertRaises(TmuxError):
            manager.send_keys(FakePane(), "unsafe\ncommand")


if __name__ == "__main__":
    unittest.main()
