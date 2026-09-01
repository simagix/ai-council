"""Client for the local Ollama HTTP API.

Uses only the Python standard library (``urllib``) — no third-party
dependencies. Talks to the API documented at
https://github.com/ollama/ollama/blob/main/docs/api.md
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 300


class OllamaError(Exception):
    """Base class for Ollama client errors."""


class OllamaConnectionError(OllamaError):
    """Ollama could not be reached at the configured host."""


class ModelNotFoundError(OllamaError):
    """A configured model is not installed in Ollama."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"Model not found: {model}")


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove ``<think>...</think>`` reasoning blocks emitted by some models.

    Reasoning models (e.g. Qwen3 family) include their hidden chain of
    thought in the raw response. The council protocol is about the models'
    stated positions, so the thinking blocks are stripped for readability.
    An unterminated ``<think>`` block (no closing tag) is dropped as well.
    """
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text.strip()


def normalize_model_name(name: str) -> str:
    """Normalize a model tag so ``llama3.2`` matches ``llama3.2:latest``."""
    normalized = name.strip().lower()
    if normalized.endswith(":latest"):
        normalized = normalized[: -len(":latest")]
    return normalized


class OllamaClient:
    """Small synchronous client for a local Ollama server."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.host = host.rstrip("/")
        self.timeout = timeout

    # -- public API ---------------------------------------------------------

    def list_models(self) -> list[str]:
        """Return the model tags currently installed in Ollama."""
        data = self._request_json("GET", "/api/tags")
        return [m.get("name", "") for m in data.get("models", [])]

    def generate(
        self,
        model: str,
        prompt: str,
        system: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """Send a single prompt to ``model`` and return the full response."""
        payload: dict = {"model": model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        data = self._request_json(
            "POST",
            "/api/generate",
            body=json.dumps(payload).encode("utf-8"),
            timeout=timeout,
            model=model,
        )
        if data.get("error"):
            raise OllamaError(f"Ollama error from {model}: {data['error']}")
        return strip_thinking(data.get("response", ""))

    # -- internals ----------------------------------------------------------

    def _request_json(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        timeout: int | None = None,
        model: str | None = None,
    ) -> dict:
        request = urllib.request.Request(
            self.host + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except OSError:
                pass
            if exc.code == 404 and model is not None:
                raise ModelNotFoundError(model) from exc
            raise OllamaError(
                f"Ollama returned HTTP {exc.code} for {method} {path}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.host}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise OllamaError(
                f"Ollama request timed out after {timeout or self.timeout}s"
            ) from exc
        except OSError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.host}: {exc}"
            ) from exc
