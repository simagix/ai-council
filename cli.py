"""Command-line interface for ai-council."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import IO, List, Optional

from config import Config, load_config
from council import preflight, run_council, CouncilError
from ollama import (
    ModelNotFoundError,
    OllamaClient,
    OllamaConnectionError,
    OllamaError,
    ThinkingStreamFilter,
)

_ROOT = Path(__file__).resolve().parent
try:
    __version__ = (_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.1.0"
except OSError:
    __version__ = "0.1.0"

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


def _read_question_file(path: str) -> Optional[str]:
    """Read a question (with context) from a file; ``-`` means stdin.

    Returns the stripped text, or ``None`` after printing an error.
    The file content is used verbatim as the council's question.
    """
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            print(f"Question file not found: {path}", file=sys.stderr)
            return None
        except IsADirectoryError:
            print(f"Not a file: {path}", file=sys.stderr)
            return None
        except OSError as exc:
            print(f"Could not read question file {path}: {exc}", file=sys.stderr)
            return None
    text = text.strip()
    if not text:
        print(f"Question file is empty: {path}", file=sys.stderr)
        return None
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
        state["label"] = _label(
            member, "Moderator" if stage == "Moderator" else None
        )
        state["started"] = False
        state["filter"] = ThinkingStreamFilter()

    def on_token(token: str) -> None:
        if not state["started"]:
            # First visible token: print the member label, then stream.
            print(f"\n{state['label']}\n", file=out)
            state["started"] = True
        print(state["filter"].feed(token), end="", file=out, flush=True)

    def on_response(member, text: str, stage: str) -> None:
        role = "Moderator" if stage == "Moderator" else None
        label = _label(member, role)
        if state["started"]:
            # Text was already streamed live; just close the block and
            # file the clean text into the transcript.
            print(state["filter"].feed(""), end="", file=out)
            print(file=out)
            transcript.append(f"{label}\n\n{text}\n")
        else:
            record(f"{label}\n\n{text}\n")
        state["started"] = False

    def on_failure(member, stage: str, error: str) -> None:
        record(_failure_block(member, stage, error))

    state = {"label": "", "started": False, "filter": ThinkingStreamFilter()}
    try:
        result = run_council(
            question,
            config,
            client=client,
            on_stage=on_stage,
            on_request=on_request,
            on_response=on_response,
            on_failure=on_failure,
            on_token=on_token,
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
        "--file",
        "-f",
        metavar="FILE",
        help=(
            "read the question (and its context) from a file; "
            "use - to read from stdin. See use_cases/ for examples."
        ),
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

    if args.file and args.question:
        parser.error("--file cannot be combined with a question argument")

    if args.file:
        question = _read_question_file(args.file)
        if question is None:
            return 2
    else:
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
