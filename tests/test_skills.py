import tempfile
import unittest
from pathlib import Path

from nixpulse.skills import SkillRegistry, SkillResult


class TestSkills(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name)
        self.registry = SkillRegistry(self.workspace)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_read_file_and_tree(self):
        test_file = self.workspace / "test.txt"
        test_file.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")

        res = self.registry.execute("read_file", {"path": "test.txt", "start_line": 2, "end_line": 4})
        self.assertTrue(res.ok)
        self.assertIn("line 2", res.output)
        self.assertIn("line 4", res.output)
        self.assertNotIn("line 1", res.output)

        res_tree = self.registry.execute("read_file", {"path": ".", "tree": True})
        self.assertTrue(res_tree.ok)
        self.assertIn("test.txt", res_tree.output)

    def test_write_script(self):
        res = self.registry.execute("write_script", {
            "name": "benchmark",
            "content": "#!/usr/bin/env bash\nset -euo pipefail\necho 'hello from benchmark'\n",
        })
        self.assertTrue(res.ok)
        script_path = self.workspace / ".nixpulse-scripts" / "benchmark.sh"
        self.assertTrue(script_path.is_file())
        self.assertTrue(bool(script_path.stat().st_mode & 0o111))

    def test_flake_manage(self):
        res = self.registry.execute("flake_manage", {
            "packages": ["go", "sqlite"],
            "build_inputs": ["zlib"],
        })
        self.assertTrue(res.ok)
        flake_path = self.workspace / "flake.nix"
        self.assertTrue(flake_path.is_file())
        content = flake_path.read_text()
        self.assertIn("devShells", content)
        self.assertIn("go", content)
        self.assertIn("sqlite", content)
        self.assertIn("zlib", content)


if __name__ == "__main__":
    unittest.main()
