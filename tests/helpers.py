"""Shared test fixtures: a fake Ollama client and a test config."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config  # noqa: E402
from ollama import OllamaConnectionError, OllamaError  # noqa: E402


class FakeClient:
    """Deterministic stand-in for OllamaClient.

    - Returns ``reply(model, prompt)`` for every generate call.
    - Models listed in ``fail_models`` raise OllamaError on generate.
    - If ``connection_error`` is True, every call raises a connection
      error (simulating Ollama not running).
    """

    def __init__(
        self,
        models=None,
        fail_models=(),
        connection_error=False,
        reply=None,
    ):
        self.models = models if models is not None else [
            "qwen3.5:9b",
            "gemma4:latest",
            "llama3.2:latest",
        ]
        self.fail_models = set(fail_models)
        self.connection_error = connection_error
        self._reply = reply
        self.calls = []  # (model, prompt, system)

    def list_models(self):
        if self.connection_error:
            raise OllamaConnectionError("connection refused")
        return list(self.models)

    def generate(self, model, prompt, system=None, timeout=None, on_token=None):
        if self.connection_error:
            raise OllamaConnectionError("connection refused")
        self.calls.append((model, prompt, system))
        if model in self.fail_models:
            raise OllamaError(f"model {model} exploded")
        if self._reply is not None:
            text = self._reply(model, prompt)
        else:
            text = f"[{model}] my answer"
        if on_token is not None:
            # Simulate streaming in small chunks.
            for i in range(0, len(text), 7):
                on_token(text[i:i + 7])
        return text


def make_config():
    return load_config(environ={})
