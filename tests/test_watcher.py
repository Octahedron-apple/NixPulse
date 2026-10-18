import tempfile
import unittest
from pathlib import Path

from nixpulse.tmux.watcher import StderrWatcher, detect_incident, strip_ansi, truncate_output


class WatcherTests(unittest.TestCase):
    def test_ansi_is_removed_and_header_is_detected(self):
        output = "\x1b[31mfatal error: zlib.h: No such file or directory\x1b[0m"
        self.assertEqual(strip_ansi(output), "fatal error: zlib.h: No such file or directory")
        incident = detect_incident(output)
        self.assertIsNotNone(incident)
        self.assertEqual(incident.suggested_package, "zlib")

    def test_truncation_keeps_the_tail(self):
        output = "\n".join(f"line-{number}" for number in range(50))
        bounded = truncate_output(output, lines=3)
        self.assertNotIn("line-0", bounded)
        self.assertIn("line-49", bounded)

    def test_file_watcher_deduplicates_same_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stderr.log"
            source.write_text("fatal error: zlib.h: No such file or directory\n")
            watcher = StderrWatcher(source)
            self.assertIsNotNone(watcher.poll_once())
            source.write_text(source.read_text() + "fatal error: zlib.h: No such file or directory\n")
            self.assertIsNone(watcher.poll_once())


if __name__ == "__main__":
    unittest.main()
