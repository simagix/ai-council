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
from typing import Callable, Optional

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 600


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


class ThinkingStreamFilter:
    """Suppress ``<think>...</think>`` spans from a live token stream.

    Reasoning models (e.g. the Qwen3 family) emit their hidden chain of
    thought wrapped in ``<think>...</think>``. This filter hides those
    spans from the terminal as tokens arrive, correctly handling tags
    split across token boundaries. Feed tokens with :meth:`feed` and
    print only what it returns.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._pending = ""

    @staticmethod
    def _partial_suffix_len(text: str, tag: str) -> int:
        """Length of the longest suffix of ``text`` that prefixes ``tag``."""
        for k in range(min(len(text), len(tag) - 1), 0, -1):
            if text.endswith(tag[:k]):
                return k
        return 0

    def feed(self, token: str) -> str:
        """Consume ``token`` and return the text that should be displayed."""
        self._pending += token
        visible: list[str] = []
        while self._pending:
            if self._inside:
                idx = self._pending.find(self._CLOSE)
                if idx != -1:
                    self._pending = self._pending[idx + len(self._CLOSE):]
                    self._inside = False
                    continue
                # Drop everything except a possible partial close tag.
                hold = self._partial_suffix_len(self._pending, self._CLOSE)
                self._pending = self._pending[-hold:] if hold else ""
                break
            idx = self._pending.find(self._OPEN)
            if idx != -1:
                visible.append(self._pending[:idx])
                self._pending = self._pending[idx + len(self._OPEN):]
                self._inside = True
                continue
            hold = self._partial_suffix_len(self._pending, self._OPEN)
            if hold:
                visible.append(self._pending[:-hold])
                self._pending = self._pending[-hold:]
            else:
                visible.append(self._pending)
                self._pending = ""
            break
        return "".join(visible)


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
        on_token: "Callable[[str], None] | None" = None,
    ) -> str:
        """Send a single prompt to ``model`` and return the full response.

        If ``on_token`` is given, the request is streamed and the callback
        is invoked with each token as it arrives from Ollama.
        """
        payload: dict = {"model": model, "prompt": prompt, "stream": bool(on_token)}
        if system:
            payload["system"] = system
        if on_token is None:
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
        return self._generate_stream(
            model, payload, timeout or self.timeout, on_token
        )

    def _generate_stream(self, model, payload, timeout, on_token) -> str:
        """Stream ``/api/generate`` (NDJSON lines), invoking ``on_token``."""
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        collected: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise OllamaError(
                            f"Ollama error from {model}: {data['error']}"
                        )
                    token = data.get("response", "")
                    if token:
                        collected.append(token)
                        on_token(token)
                    if data.get("done"):
                        break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ModelNotFoundError(model) from exc
            raise OllamaError(
                f"Ollama returned HTTP {exc.code} for {model}"
            ) from exc
        except urllib.error.URLError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.host}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.host}: {exc}"
            ) from exc
        return strip_thinking("".join(collected))

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
