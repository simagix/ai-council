"""Council configuration: model names and basic settings.

Defaults live here so nothing is hard-coded throughout the application.
Every value can be overridden with environment variables:

============================  =============================================
Variable                      Meaning
============================  =============================================
``AI_COUNCIL_OLLAMA_HOST``    Ollama API base URL (default localhost:11434)
``AI_COUNCIL_TIMEOUT``        Per-request timeout in seconds (default 300)
``AI_COUNCIL_MODEL_ANALYST``  Model for the Analyst role
``AI_COUNCIL_MODEL_THINKER``  Model for the Independent Thinker role
``AI_COUNCIL_MODEL_SKEPTIC``  Model for the Skeptic role
``AI_COUNCIL_MODERATOR``      Model used as the Round 3 moderator
============================  =============================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from .models import CouncilMember

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MODERATOR_MODEL = "qwen3.5:9b"

_MEMBER_ENV_VARS = {
    "analyst": "AI_COUNCIL_MODEL_ANALYST",
    "independent_thinker": "AI_COUNCIL_MODEL_THINKER",
    "skeptic": "AI_COUNCIL_MODEL_SKEPTIC",
}


def default_members() -> Tuple[CouncilMember, ...]:
    """The default council: Analyst, Independent Thinker, Skeptic."""
    return (
        CouncilMember(
            name="Qwen 3.5 9B",
            model="qwen3.5:9b",
            role="Analyst",
            role_key="analyst",
        ),
        CouncilMember(
            name="Gemma 4",
            model="gemma4:latest",
            role="Independent Thinker",
            role_key="independent_thinker",
        ),
        CouncilMember(
            name="Llama 3.2",
            model="llama3.2:latest",
            role="Skeptic",
            role_key="skeptic",
        ),
    )


@dataclass(frozen=True)
class Config:
    """Runtime configuration for a council session."""

    ollama_host: str = DEFAULT_OLLAMA_HOST
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    members: Tuple[CouncilMember, ...] = field(default_factory=default_members)
    moderator_model: str = DEFAULT_MODERATOR_MODEL


def load_config(environ: Optional[Mapping[str, str]] = None) -> Config:
    """Build a :class:`Config`, applying environment-variable overrides."""
    env = os.environ if environ is None else environ

    members: list[CouncilMember] = []
    for member in default_members():
        env_name = _MEMBER_ENV_VARS[member.role_key]
        model = env.get(env_name, member.model)
        members.append(
            CouncilMember(
                name=member.name,
                model=model,
                role=member.role,
                role_key=member.role_key,
            )
        )

    return Config(
        ollama_host=env.get("AI_COUNCIL_OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
        timeout_seconds=int(env.get("AI_COUNCIL_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
        members=tuple(members),
        moderator_model=env.get("AI_COUNCIL_MODERATOR", DEFAULT_MODERATOR_MODEL),
    )
