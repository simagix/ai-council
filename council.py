"""Council orchestration: Round 1 -> Round 2 -> Moderator -> final report.

The protocol is intentionally simple and transparent:

1. Round 1 — every member answers the question independently (no member
   sees another member's response).
2. Round 2 — every surviving member sees all Round 1 responses and
   critiques the discussion.
3. Round 3 — one configured model acts as moderator and produces the
   final report. No majority voting: reasoning is evaluated, not counted.

All models run sequentially to minimize memory pressure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from config import Config
from models import CouncilMember
from ollama import (
    ModelNotFoundError,
    OllamaClient,
    OllamaError,
    normalize_model_name,
)
from prompts import (
    MODERATOR_SYSTEM,
    MODERATOR_TEMPLATE,
    ROLE_SYSTEM_PROMPTS,
    ROUND2_TEMPLATE,
)


class CouncilError(Exception):
    """The council could not be convened at all."""


@dataclass
class MemberResponse:
    """A single member's response in one round."""

    member: CouncilMember
    text: str


@dataclass
class MemberFailure:
    """A member's failure in one round, reported rather than hidden."""

    member: CouncilMember
    stage: str
    error: str


@dataclass
class CouncilResult:
    """Everything the council produced in one session."""

    question: str
    round1: List[MemberResponse] = field(default_factory=list)
    round2: List[MemberResponse] = field(default_factory=list)
    failures: List[MemberFailure] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    report: Optional[str] = None
    report_member: Optional[CouncilMember] = None


def preflight(client: OllamaClient, config: Config) -> None:
    """Verify Ollama is reachable and every configured model is installed.

    Raises:
        OllamaConnectionError: Ollama is not reachable.
        ModelNotFoundError: The first missing configured model.
    """
    installed = {normalize_model_name(name) for name in client.list_models()}
    needed = [m.model for m in config.members]
    needed.append(config.moderator_model)
    for model in needed:
        if normalize_model_name(model) not in installed:
            raise ModelNotFoundError(model)


def _transcript_of(responses: List[MemberResponse]) -> str:
    """Format responses as a labelled transcript for the next round."""
    return "\n\n".join(
        f"[{r.member.name} — {r.member.role}]\n{r.text}" for r in responses
    )


def _moderator_member(config: Config) -> CouncilMember:
    """The council member acting as moderator (falls back to a stand-in)."""
    for member in config.members:
        if normalize_model_name(member.model) == normalize_model_name(
            config.moderator_model
        ):
            return CouncilMember(
                name=member.name,
                model=member.model,
                role="Moderator",
                role_key=member.role_key,
            )
    return CouncilMember(
        name=config.moderator_model,
        model=config.moderator_model,
        role="Moderator",
        role_key="analyst",
    )



def run_council(
    question: str,
    config: Config,
    client: Optional[OllamaClient] = None,
    *,
    on_stage: Optional[Callable[[str], None]] = None,
    on_request: Optional[Callable[[CouncilMember, str], None]] = None,
    on_response: Optional[Callable[[CouncilMember, str, str], None]] = None,
    on_failure: Optional[Callable[[CouncilMember, str, str], None]] = None,
    on_token: Optional[Callable[[str], None]] = None,
) -> CouncilResult:
    """Run the full council protocol sequentially.

    The ``on_*`` callbacks let the CLI display progress live without the
    orchestrator knowing anything about terminals:

    - ``on_stage(title)`` — a new round begins.
    - ``on_request(member, stage)`` — a member is being consulted.
    - ``on_token(token)`` — a token of the current response arrived.
    - ``on_response(member, text, stage)`` — a member finished.
    - ``on_failure(member, stage, error)`` — a member failed.
    """
    client = client or OllamaClient(config.ollama_host, config.timeout_seconds)
    result = CouncilResult(question=question)

    def fail(member: CouncilMember, stage: str, error: str) -> None:
        result.failures.append(MemberFailure(member, stage, error))
        if on_failure:
            on_failure(member, stage, error)

    def generate(model: str, prompt: str, system: str) -> str:
        kwargs = {"on_token": on_token} if on_token else {}
        return client.generate(model, prompt, system=system, **kwargs)

    # -- Round 1: independent opinions ------------------------------------
    if on_stage:
        on_stage("ROUND 1 — INDEPENDENT OPINIONS")
    for member in config.members:
        if on_request:
            on_request(member, "Round 1")
        try:
            text = generate(
                member.model,
                question,
                ROLE_SYSTEM_PROMPTS[member.role_key],
            )
        except OllamaError as exc:
            fail(member, "Round 1", str(exc))
            continue
        response = MemberResponse(member, text)
        result.round1.append(response)
        if on_response:
            on_response(member, text, "Round 1")

    if not result.round1:
        raise CouncilError("All council members failed to respond.")

    # -- Round 2: council discussion ---------------------------------------
    if len(result.round1) < 2:
        result.notes.append(
            "Round 2 skipped: fewer than two council members responded."
        )
    else:
        if on_stage:
            on_stage("ROUND 2 — COUNCIL DISCUSSION")
        round1_transcript = _transcript_of(result.round1)
        prompt = ROUND2_TEMPLATE.format(
            question=question, transcript=round1_transcript
        )
        for response in result.round1:
            member = response.member
            if on_request:
                on_request(member, "Round 2")
            try:
                text = generate(
                    member.model,
                    prompt,
                    ROLE_SYSTEM_PROMPTS[member.role_key],
                )
            except OllamaError as exc:
                fail(member, "Round 2", str(exc))
                continue
            round2_response = MemberResponse(member, text)
            result.round2.append(round2_response)
            if on_response:
                on_response(member, text, "Round 2")

    # -- Round 3: moderator / consensus report ------------------------------
    moderator = _moderator_member(config)
    if on_stage:
        on_stage("FINAL COUNCIL REPORT")
    if on_request:
        on_request(moderator, "Moderator")
    moderator_prompt = MODERATOR_TEMPLATE.format(
        question=question,
        round1=_transcript_of(result.round1),
        round2=(
            _transcript_of(result.round2)
            if result.round2
            else "(Round 2 did not take place.)"
        ),
    )
    try:
        text = generate(moderator.model, moderator_prompt, MODERATOR_SYSTEM)
    except OllamaError as exc:
        fail(moderator, "Moderator", str(exc))
        return result

    result.report = text
    result.report_member = moderator
    if on_response:
        on_response(moderator, text, "Moderator")
    return result
