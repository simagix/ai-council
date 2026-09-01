"""Command-line interface for ai-council."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import IO, List, Optional

from . import __version__
from .config import Config, load_config
from .council import preflight, run_council, CouncilError
from .ollama import (
    ModelNotFoundError,
    OllamaClient,
    OllamaConnectionError,
    OllamaError,
)

DOUBLE_LINE = "═" * 52
SINGLE_LINE = "─" * 52

CANNOT_CONNECT = (
    "Cannot connect to Ollama.\n\nMake sure Ollama is running and try again."
)


def _model_not_found(model: str) -> str:
    return (
        f"Model not found: {model}\n\nInstall it with:\n\nollama pull {model}"
    )


def _banner() -> str:
    return f"{DOUBLE_LINE}\n{'AI COUNCIL'.center(52)}\n{DOUBLE_LINE}"


def _section(title: str) -> str:
    return f"\n{SINGLE_LINE}\n{title}\n{SINGLE_LINE}\n"


def _label(member, role_override: Optional[str] = None) -> str:
    role = role_override or member.role
    return f"[{member.name} — {role}]"


def _header(question: str) -> str:
    return f"{_banner()}\n\nQUESTION\n\n{question}"


def _interactive_question() -> str:
    print("AI Council\n")
    print("Enter your question:")
    print("(finish with Ctrl-D on a new line)")
    print("> ", end="", flush=True)
    text = sys.stdin.read().strip()
    print()
    return text


def _failure_block(member, stage: str, error: str) -> str:
    return (
        f"\n[FAILED — {member.name} ({member.role}) during {stage}]\n"
        f"{error}\n"
    )


def save_transcript(
    path: str, question: str, transcript: List[str], out: IO
) -> None:
    """Write the whole session to ``path`` as a Markdown file."""
    content = "\n".join(
        [
            "# AI Council Session",
            "",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"**Question:** {question}",
            "",
            "```",
            *transcript,
            "```",
            "",
        ]
    )
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"\nTranscript saved to {path}", file=out)
    except OSError as exc:
        print(f"\nCould not save transcript to {path}: {exc}", file=out)


def run_session(
    question: str,
    config: Optional[Config] = None,
    client: Optional[OllamaClient] = None,
    out: Optional[IO] = None,
    save_path: Optional[str] = None,
) -> int:
    """Run one council session and render the transcript to ``out``."""
    out = out if out is not None else sys.stdout
    config = config or load_config()
    client = client or OllamaClient(config.ollama_host, config.timeout_seconds)

    try:
        preflight(client, config)
    except OllamaConnectionError:
        print(CANNOT_CONNECT, file=out)
        return 2
    except ModelNotFoundError as exc:
        print(_model_not_found(exc.model), file=out)
        return 2
    except OllamaError as exc:
        print(str(exc), file=out)
        return 2

    transcript: List[str] = []

    def record(text: str) -> None:
        transcript.append(text)
        print(text, file=out)

    record(_header(question))

    def on_stage(title: str) -> None:
        record(_section(title))

    def on_request(member, stage: str) -> None:
        role = "Moderator" if stage == "Moderator" else member.role
        print(f"Consulting {member.name} ({role})...", file=out)

    def on_response(member, text: str, stage: str) -> None:
        role = "Moderator" if stage == "Moderator" else None
        record(f"{_label(member, role)}\n\n{text}\n")

    def on_failure(member, stage: str, error: str) -> None:
        record(_failure_block(member, stage, error))

    try:
        result = run_council(
            question,
            config,
            client=client,
            on_stage=on_stage,
            on_request=on_request,
            on_response=on_response,
            on_failure=on_failure,
        )
    except CouncilError as exc:
        print(f"\nCouncil aborted: {exc}", file=out)
        return 1

    if result.report is None:
        print("\nNo final report: the moderator did not respond.", file=out)
        return 1

    record(DOUBLE_LINE)

    if save_path:
        save_transcript(save_path, question, transcript, out)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai-council",
        description=(
            "Convene three local Ollama models to deliberate a question "
            "and produce a consensus report."
        ),
    )
    parser.add_argument(
        "question", nargs="*", help="the question for the council"
    )
    parser.add_argument(
        "--save",
        metavar="FILE",
        help="save the full session transcript to a Markdown file",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ai-council v{__version__}",
    )
    args = parser.parse_args(argv)

    question = " ".join(args.question).strip()
    if not question:
        question = _interactive_question()
    if not question:
        print("No question provided.", file=sys.stderr)
        return 2

    try:
        return run_session(question, config=load_config(), save_path=args.save)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
