import unittest

from nixpulse.llm.client import OllamaClient
from nixpulse.llm.prompts import parse_incident, parse_intent


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"response": '{"packages":["go"]}'}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


class PromptTests(unittest.TestCase):
    def test_intent_uses_safe_mapping(self):
        plan = parse_intent('{"packages":["sqlite3", "load-test"], "layout":"tiled"}')
        self.assertEqual(plan.packages, ("sqlite", "hey"))

    def test_intent_rejects_raw_or_unknown_nix_attribute(self):
        with self.assertRaises(ValueError):
            parse_intent('{"packages":["pkgs.zlib"]}')

    def test_incident_canonicalizes_suggestion(self):
        diagnosis = parse_incident(
            '{"missing_library":"zlib.h", "suggested_package":"zlib-devel", "explanation":"header missing"}'
        )
        self.assertEqual(diagnosis.suggested_package, "zlib")

    def test_ollama_request_is_explicit_and_structured(self):
        session = FakeSession()
        client = OllamaClient(session=session)
        self.assertEqual(client.generate_json("intent"), {"packages": ["go"]})
        _, request = session.calls[0]
        self.assertEqual(request["json"]["model"], "hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M")
        self.assertEqual(request["json"]["format"], "json")
        self.assertEqual(request["json"]["options"]["num_ctx"], 2048)


if __name__ == "__main__":
    unittest.main()
