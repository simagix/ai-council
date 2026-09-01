"""Representations of council members and their roles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CouncilMember:
    """One member of the council.

    Attributes:
        name: Display name used in the transcript (e.g. ``"Qwen 3.5 9B"``).
        model: The Ollama model tag to run (e.g. ``"qwen3.5:9b"``).
        role: Human-readable role label (e.g. ``"Analyst"``).
        role_key: Stable key used to look up the role's system prompt
            in :data:`prompts.ROLE_SYSTEM_PROMPTS`.
    """

    name: str
    model: str
    role: str
    role_key: str
