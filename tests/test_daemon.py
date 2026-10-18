import tempfile
import unittest
from pathlib import Path

from nixpulse.daemon import NixPulseDaemon


class DaemonTests(unittest.TestCase):
    def test_offline_flow_writes_then_patches_manifest(self):
        daemon = NixPulseDaemon()
        with tempfile.TemporaryDirectory() as directory:
            shell_file = daemon.provision(daemon.plan("Go web service with SQLite"), directory)
            before = Path(shell_file).read_text()
            remediation = daemon.diagnose("fatal error: zlib.h: No such file or directory", exit_code=1)
            self.assertIsNotNone(remediation)
            daemon.apply_remediation(remediation, approved=True)
            after = Path(shell_file).read_text()
            self.assertNotIn("pkgs.zlib", before)
            self.assertIn("zlib", after)
            self.assertIn("makeLibraryPath libs", after)

    def test_unapproved_remediation_does_not_write(self):
        daemon = NixPulseDaemon()
        with tempfile.TemporaryDirectory() as directory:
            shell_file = daemon.provision(daemon.plan("Go"), directory)
            before = Path(shell_file).read_text()
            remediation = daemon.diagnose("fatal error: zlib.h: No such file or directory")
            with self.assertRaises(PermissionError):
                daemon.apply_remediation(remediation, approved=False)
            self.assertEqual(Path(shell_file).read_text(), before)


if __name__ == "__main__":
    unittest.main()
