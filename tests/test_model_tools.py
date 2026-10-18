"""Integration tests calling the real local model with tools in isolation.

These tests communicate directly with the local Ollama instance running
the target Gemma model: hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M.
They verify that:
1. The model receives tools in Ollama's native function schema.
2. The model reasons and outputs tool_calls.
3. The chosen tool executes against isolated test fixtures.
4. The tool result satisfies the query.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import requests

from nixpulse.config import settings
from nixpulse.llm.client import OllamaClient
from nixpulse.skills import SkillRegistry


def ollama_is_running(host: str) -> bool:
    try:
        url = host if host.startswith(("http://", "https://")) else f"http://{host}"
        r = requests.get(f"{url}/api/tags", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


class TestModelWithToolsIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ollama_is_running(settings.ollama_host):
            raise unittest.SkipTest(f"Ollama server not reachable at {settings.ollama_host}")
        cls.client = OllamaClient(
            host=settings.ollama_host,
            model=settings.model_name,
            timeout=45.0,
        )

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name)
        self.skills = SkillRegistry(self.workspace)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_model_calls_read_file_tree(self):
        """Test model decides to call read_file tool with tree=True to inspect structure."""
        # Create dummy directory structure
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "main.py").write_text("print('hello')\n")
        (self.workspace / "README.md").write_text("# Test Project\n")

        tools = self.skills.as_ollama_tools()
        messages = [
            {
                "role": "user",
                "content": "Please inspect this workspace directory structure. Use the read_file tool with tree=true.",
            }
        ]

        response = self.client.chat_with_tools(messages, tools=tools)
        tool_calls = response.get("tool_calls")
        self.assertIsNotNone(tool_calls, f"Expected tool_calls in response: {response}")
        self.assertTrue(len(tool_calls) > 0)

        call = tool_calls[0]
        fn = call.get("function", {})
        self.assertEqual(fn.get("name"), "read_file")

        # Execute the tool in isolation
        args = fn.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
        result = self.skills.execute(fn.get("name"), args)
        self.assertTrue(result.ok)
        self.assertIn("src", result.output)
        self.assertIn("README.md", result.output)

    def test_model_calls_write_script(self):
        """Test model calls write_script tool to generate a benchmark script."""
        tools = self.skills.as_ollama_tools()
        messages = [
            {
                "role": "user",
                "content": "Generate an executable bash script named 'bench' that runs a loop printing 'benchmarking'. Use write_script tool.",
            }
        ]

        response = self.client.chat_with_tools(messages, tools=tools)
        tool_calls = response.get("tool_calls")
        self.assertIsNotNone(tool_calls, f"Expected tool_calls in response: {response}")
        self.assertTrue(len(tool_calls) > 0)

        call = tool_calls[0]
        fn = call.get("function", {})
        self.assertEqual(fn.get("name"), "write_script")

        args = fn.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
        result = self.skills.execute("write_script", args)
        self.assertTrue(result.ok)

        # Verify the script exists on disk and is executable
        script_dir = self.workspace / ".nixpulse-scripts"
        files = list(script_dir.glob("*.sh"))
        self.assertTrue(len(files) > 0, "Script was not written to .nixpulse-scripts/")
        script = files[0]
        self.assertTrue(bool(script.stat().st_mode & 0o111), "Script is not executable")

    def test_model_calls_flake_manage(self):
        """Test model calls flake_manage to generate a flake with requested packages."""
        tools = self.skills.as_ollama_tools()
        messages = [
            {
                "role": "user",
                "content": "Configure a Nix flake environment with packages 'go' and 'sqlite'. Use flake_manage tool.",
            }
        ]

        response = self.client.chat_with_tools(messages, tools=tools)
        tool_calls = response.get("tool_calls")
        self.assertIsNotNone(tool_calls, f"Expected tool_calls in response: {response}")
        self.assertTrue(len(tool_calls) > 0)

        call = tool_calls[0]
        fn = call.get("function", {})
        self.assertEqual(fn.get("name"), "flake_manage")

        args = fn.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
        result = self.skills.execute("flake_manage", args)
        self.assertTrue(result.ok)

        flake_file = self.workspace / "flake.nix"
        self.assertTrue(flake_file.is_file())
        content = flake_file.read_text()
        self.assertIn("devShells", content)
        self.assertIn("go", content)
        self.assertIn("sqlite", content)


if __name__ == "__main__":
    unittest.main()
