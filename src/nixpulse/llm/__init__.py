"""Optional local-model integration for NixPulse.

Importing this package never contacts Ollama.  Network inference only occurs
when :meth:`OllamaClient.generate_json` is called explicitly.
"""

from .client import OllamaClient, OllamaError
from .prompts import IncidentDiagnosis, IntentPlan, parse_incident, parse_intent

__all__ = [
    "IncidentDiagnosis",
    "IntentPlan",
    "OllamaClient",
    "OllamaError",
    "parse_incident",
    "parse_intent",
]
