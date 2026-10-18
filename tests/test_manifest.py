import tempfile
import unittest
from pathlib import Path

from nixpulse.manifest import (
    ManifestError,
    WorkspaceManifest,
    render_shell_nix,
    resolve_package,
    write_shell_nix,
)


class ManifestTests(unittest.TestCase):
    def test_aliases_resolve_to_canonical_attributes(self):
        self.assertEqual(resolve_package("zlib-devel"), "zlib")
        self.assertEqual(resolve_package("load-test"), "hey")

    def test_unknown_package_is_rejected_before_rendering(self):
        with self.assertRaises(ManifestError):
            WorkspaceManifest(("go-sqlite3",))

    def test_render_contains_library_runtime_path_and_status_hook(self):
        manifest = WorkspaceManifest(("gcc",), libraries=("zlib",))
        rendered = render_shell_nix(manifest)
        self.assertIn("buildInputs = with pkgs; [ gcc ] ++ libs;", rendered)
        self.assertIn("pkgs.lib.makeLibraryPath libs", rendered)
        self.assertIn("nixpulse_prompt_status", rendered)

    def test_write_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_shell_nix(Path(directory) / "shell.nix", WorkspaceManifest(("go",)))
            self.assertTrue(path.is_file())
            self.assertIn("[ go ]", path.read_text())


if __name__ == "__main__":
    unittest.main()
