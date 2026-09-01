"""Command-line interface for ai-council."""

from __future__ import annotations

import argparse
import html
import sys
import zlib
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


_ROLE_PALETTE = (
    "#4f46e5",  # indigo
    "#0f766e",  # teal
    "#b45309",  # amber
    "#be123c",  # rose
    "#7c3aed",  # violet
    "#15803d",  # green
    "#db2777",  # pink
    "#0284c7",  # sky
)


def _role_color(role: str) -> str:
    """Pick a stable colour for a member role (any role name is safe)."""
    return _ROLE_PALETTE[zlib.crc32(role.encode("utf-8")) % len(_ROLE_PALETTE)]


def _md_wrap_pairs(text, marker, open_tag, close_tag):
    """Wrap balanced ``marker`` runs (e.g. ``**`` / ``*``) in HTML tags."""
    if text.count(marker) < 2 or text.count(marker) % 2 == 1:
        return text
    out = []
    depth = 0
    rest = text
    while True:
        at = rest.find(marker)
        if at < 0:
            out.append(rest)
            break
        out.append(rest[:at])
        out.append(open_tag if depth % 2 == 0 else close_tag)
        depth += 1
        rest = rest[at + len(marker):]
    return "".join(out)


def _md_links(text):
    """Convert ``[text](url)`` links to ``<a>`` tags (text already escaped)."""
    out = []
    i = 0
    while True:
        a = text.find("[", i)
        if a < 0:
            out.append(text[i:])
            break
        b = text.find("]", a + 1)
        c = text.find("(", b + 1)
        d = text.find(")", c + 1)
        if b > a and c == b + 1 and d > c and "." in text[c + 1:d]:
            url = text[c + 1:d]
            label = text[a + 1:b]
            out.append(text[i:a])
            out.append(
                '<a href="' + html.escape(url, quote=True) + '">'
                + html.escape(label, quote=True) + "</a>"
            )
            i = d + 1
        else:
            out.append(text[i:a + 1])
            i = a + 1
    return "".join(out)


def _md_inline(text):
    """Render inline Markdown: ``code``, **bold**, *italic*, [links]."""
    parts = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "`":
            end = text.find("`", i + 1)
            if end > i:
                parts.append("<code>" + html.escape(text[i + 1:end], quote=True) + "</code>")
                i = end + 1
                continue
            parts.append(html.escape(ch, quote=False))
            i += 1
            continue
        parts.append(html.escape(ch, quote=False))
        i += 1
    body = "".join(parts)
    body = _md_wrap_pairs(body, "**", "<strong>", "</strong>")
    body = _md_wrap_pairs(body, "*", "<em>", "</em>")
    body = _md_links(body)
    return body


def _md_is_heading(text):
    stripped = text.lstrip()
    if not stripped.startswith("#"):
        return False
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level > 6:
        return False
    return level < len(stripped) and stripped[level] == " "


def _md_heading_level(text):
    stripped = text.lstrip()
    return len(stripped) - len(stripped.lstrip("#"))


def _md_heading_text(text):
    stripped = text.lstrip()
    level = len(stripped) - len(stripped.lstrip("#"))
    return stripped[level + 1:].strip()


def _md_is_hr(stripped):
    return len(stripped) >= 3 and set(stripped) <= set("-*_") and stripped[0] in "-*_"


def _md_is_fence(text):
    t = text.lstrip()
    return t.startswith("```") or t.startswith("~~~")


def markdown_to_html(text):
    """Render a Markdown subset to an HTML fragment.

    Supports headings, horizontal rules, bullet and ordered lists,
    blockquotes, fenced code blocks, inline code, bold/italic/links and
    paragraphs. All text is HTML-escaped, so model output can never
    inject markup. The result is a fragment (no ``<html>``/``<body>``).
    """
    if not text:
        return ""

    nl = chr(10)
    out = []
    lines = text.split(nl)
    para = []
    items = []
    ordered = None
    code = []
    quote = []
    in_code = False
    in_quote = False

    def flush_para():
        if para:
            out.append("<p>" + _md_inline(" ".join(para).strip()) + "</p>")
            para.clear()

    def flush_list():
        nonlocal ordered
        if not items:
            return
        tag = "ol" if ordered else "ul"
        out.append("<" + tag + ">")
        for item in items:
            out.append("<li>" + _md_inline(item) + "</li>")
        out.append("</" + tag + ">")
        items.clear()
        ordered = None

    def flush_code():
        nonlocal in_code
        if not in_code:
            return
        out.append("<pre><code>" + html.escape(nl.join(code), quote=True) + "</code></pre>")
        code.clear()
        in_code = False

    def flush_quote():
        nonlocal in_quote
        if not in_quote:
            return
        out.append("<blockquote>" + _md_inline(" ".join(quote).strip()) + "</blockquote>")
        quote.clear()
        in_quote = False

    def close_blocks():
        flush_para()
        flush_list()
        flush_code()
        flush_quote()

    for line in lines:
        if in_code:
            if _md_is_fence(line):
                flush_code()
            else:
                code.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            close_blocks()
            continue
        if _md_is_fence(line):
            close_blocks()
            in_code = True
            continue
        if _md_is_heading(line):
            close_blocks()
            level = _md_heading_level(line)
            out.append("<h" + str(level) + ">" + _md_inline(_md_heading_text(line)) + "</h" + str(level) + ">")
            continue
        if _md_is_hr(stripped):
            close_blocks()
            out.append("<hr>")
            continue
        if line.lstrip().startswith(">"):
            in_quote = True
            quote.append(line.lstrip()[1:].lstrip())
            flush_list()
            continue
        text_l = line.lstrip()
        is_numbered = bool(text_l) and text_l[0].isdigit() and ". " in text_l[:8]
        is_bullet = text_l.startswith("- ") or text_l.startswith("+ ") or text_l.startswith("* ")
        if is_numbered or is_bullet:
            flush_quote()
            flush_para()
            if is_numbered:
                idx = text_l.find(". ")
                item_text = text_l[idx + 2:]
            else:
                item_text = text_l[2:]
            new_ordered = is_numbered
            if items and ordered != new_ordered:
                flush_list()
            ordered = new_ordered
            items.append(item_text.strip())
            continue
        if in_quote:
            flush_quote()
        flush_list()
        para.append(stripped)

    close_blocks()
    return nl.join(out)


# Self-contained page style: inline CSS only, no external assets, so the
# file renders identically from any folder or over ``file://``.
_PAGE_CSS = """\
:root {
  --bg: #f3f4f6;
  --card: #ffffff;
  --ink: #1f2430;
  --muted: #6b7280;
  --border: #e5e7eb;
  --masthead: #111827;
  --masthead-ink: #f9fafb;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
}
main { max-width: 880px; margin: 0 auto; padding: 32px 20px 64px; }
.masthead {
  background: var(--masthead);
  color: var(--masthead-ink);
  border-radius: 14px;
  padding: 30px 34px;
  margin-bottom: 26px;
}
.masthead h1 { margin: 0 0 6px; font-size: 30px; font-weight: 700; }
.masthead .date { margin: 0; color: #cbd5e1; font-size: 14px; }
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 22px 26px;
  margin-bottom: 22px;
}
h2 { margin: 0 0 12px; font-size: 20px; color: var(--masthead); }
.round h2 {
  margin: 0 0 18px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}
.member {
  margin: 0 0 18px;
  padding: 14px 18px 16px;
  background: #fafafb;
  border-radius: 0 10px 10px 0;
}
.member:last-child { margin-bottom: 0; }
.member h3 {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 600;
}
.badge {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 999px;
  letter-spacing: 0.2px;
  white-space: nowrap;
}
.body {
  white-space: normal;
  overflow-wrap: anywhere;
  font-size: 15px;
  margin: 0;
}
.fail { background: #fef2f2; border-color: #fecaca; }
.fail h2 { color: #b91c1c; }
.fail ul { margin: 8px 0 0; padding-left: 20px; }
.fail li { margin-bottom: 8px; color: #7f1d1d; }
.note {
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  color: #1e40af;
  border-radius: 10px;
  padding: 12px 16px;
  margin-bottom: 18px;
  font-size: 15px;
}
.foot {
  color: var(--muted);
  font-size: 13px;
  text-align: center;
  margin-top: 34px;
}
"""


def _write_transcript(path: str, content: str, out: IO) -> None:
    """Write ``content`` to ``path`` and confirm on ``out``."""
    try:
        directory = Path(path).parent
        if directory and not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"\nTranscript saved to {path}", file=out)
    except OSError as exc:
        print(f"\nCould not save transcript to {path}: {exc}", file=out)


def save_markdown_transcript(path: str, result, out: IO) -> None:
    """Write the session to ``path`` as readable Markdown (``--md``).

    Unlike the terminal transcript (ASCII banners for a terminal), the
    saved file uses real Markdown structure so it renders well in any
    Markdown viewer: the question and each member's output keep their
    own formatting instead of being fenced into one code block.
    """
    lines: List[str] = [
        "# AI Council Session",
        "",
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Question",
        "",
        result.question,
        "",
    ]
    if result.round1:
        lines += ["## Round 1 — Independent Opinions", ""]
        for r in result.round1:
            lines += [f"### {r.member.name} — {r.member.role}", "", r.text, ""]
    if result.round2:
        lines += ["## Round 2 — Council Discussion", ""]
        for r in result.round2:
            lines += [f"### {r.member.name} — {r.member.role}", "", r.text, ""]
    if result.report is not None:
        lines += ["## Final Council Report", ""]
        if result.report_member is not None:
            lines += [
                f"### {result.report_member.name} — Moderator",
                "",
            ]
        lines += [result.report, ""]
    if result.failures:
        lines += ["## Failures", ""]
        for f in result.failures:
            lines += [
                f"- **{f.member.name} ({f.member.role})** failed during "
                f"{f.stage}: {f.error}",
                "",
            ]
    for note in result.notes:
        lines += [f"> {note}", ""]
    _write_transcript(path, "\n".join(lines), out)


def save_html_transcript(path: str, result, out: IO) -> None:
    """Write the session to ``path`` as a self-contained HTML page.

    All styling is inline CSS — no external assets or scripts — so the
    file renders identically from any folder or over ``file://``.  Markdown
    content (the question and each model output) is rendered to real HTML
    so it reads as a page, not as raw Markdown; all text is HTML-escaped,
    so model output can never inject markup.
    """
    nl = chr(10)

    def member_article(member, text: str) -> str:
        color = _role_color(member.role)
        badge = (
            f'<span class="badge" style="color:{color};'
            f'background:{color}1f">{html.escape(member.role, quote=True)}'
            "</span>"
        )
        return (
            f'<article class="member" style="border-left:5px solid {color}">'
            f"<h3>{html.escape(member.name, quote=True)} {badge}</h3>"
            f'<div class="body">{markdown_to_html(text)}</div>'
            "</article>"
        )

    def round_section(title: str, entries) -> str:
        blocks = "".join(member_article(e.member, e.text) for e in entries)
        return (
            '<section class="card round">'
            f'<h2>{html.escape(title, quote=True)}</h2>{blocks}</section>'
        )

    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts: List[str] = [
        '<header class="masthead">',
        "<h1>AI Council Session</h1>",
        f'<p class="date">Date: {html.escape(date, quote=True)}</p>',
        "</header>",
        '<section class="card">',
        "<h2>Question</h2>",
        f'<div class="body">{markdown_to_html(result.question)}</div>',
        "</section>",
    ]
    if result.round1:
        parts.append(
            round_section("Round 1 — Independent Opinions", result.round1)
        )
    if result.round2:
        parts.append(
            round_section("Round 2 — Council Discussion", result.round2)
        )
    if result.report is not None:
        parts.append('<section class="card round">')
        parts.append("<h2>Final Council Report</h2>")
        if result.report_member is not None:
            parts.append(member_article(result.report_member, result.report))
        else:
            parts.append(
                f'<div class="body">{markdown_to_html(result.report)}</div>'
            )
        parts.append("</section>")
    if result.failures:
        items = "".join(
            f"<li><strong>{html.escape(f.member.name, quote=True)} "
            f"({html.escape(f.member.role, quote=True)})</strong> failed "
            f"during {html.escape(f.stage)}: "
            f"{html.escape(f.error, quote=True)}</li>"
            for f in result.failures
        )
        parts.append(
            '<section class="card fail"><h2>Failures</h2>'
            f'<ul>{items}</ul></section>'
        )
    for note in result.notes:
        parts.append(f'<div class="note">{html.escape(note, quote=True)}</div>')
    parts.append(
        f'<footer class="foot">Generated by ai-council v{__version__} on '
        f'{html.escape(date, quote=True)}</footer>'
    )

    content = (
        "<!DOCTYPE html>" + nl
        + '<html lang="en">' + nl
        + "<head>" + nl
        + '<meta charset="utf-8">' + nl
        + '<meta name="viewport" content="width=device-width, initial-scale=1">' + nl
        + "<title>AI Council Session</title>" + nl
        + f"<style>{nl}{_PAGE_CSS}</style>{nl}"
        + "</head>" + nl
        + "<body>" + nl
        + "<main>" + nl
        + (nl.join(parts))
        + nl
        + "</main>" + nl
        + "</body>" + nl
        + "</html>" + nl
    )
    _write_transcript(path, content, out)
def run_session(
    question: str,
    config: Optional[Config] = None,
    client: Optional[OllamaClient] = None,
    out: Optional[IO] = None,
    md_path: Optional[str] = None,
    html_path: Optional[str] = None,
) -> int:
    """Run one council session and render the transcript to ``out``.

    ``md_path`` (``--md``) and ``html_path`` (``--html``) optionally save
    the transcript as Markdown and/or as a self-contained HTML page; the
    two flags can be used together.
    """
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

    if md_path:
        save_markdown_transcript(md_path, result, out)
    if html_path:
        save_html_transcript(html_path, result, out)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # ``--server`` launches the web interface (the ``serve`` subcommand
    # remains as a legacy alias).  The flag is stripped before dispatch so
    # the server parser only sees its own options.
    if "--server" in argv:
        argv = [a for a in argv if a != "--server"]
        from server import serve_main

        return serve_main(argv)

    # Legacy alias: ``... serve ...``.  Only treat the word "serve" as a
    # subcommand when nothing else follows it or when the next token is a
    # flag, so questions like "serve me a sandwich" still reach the council.
    if argv and argv[0] == "serve" and (len(argv) == 1 or argv[1].startswith("-")):
        from server import serve_main

        return serve_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="ai-council",
        description=(
            "Convene three local Ollama models to deliberate a question "
            "and produce a consensus report. Run with --server to start "
            "the web interface."
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
        "--md",
        metavar="FILE",
        help="save the full session transcript to a readable Markdown file",
    )
    parser.add_argument(
        "--html",
        metavar="FILE",
        help=(
            "save the full session transcript to a self-contained HTML "
            "page; without --md/--html a timestamped HTML file is "
            "written by default"
        ),
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

    # Default output: a timestamped HTML transcript whenever the user
    # gave neither flag (so repeated sessions never overwrite each
    # other).  --md alone means "Markdown only"; --html FILE picks the
    # name explicitly.  The two flags can still be combined.
    html_path = args.html
    if html_path is None and args.md is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_path = f"out/council-{stamp}.html"

    try:
        return run_session(
            question,
            config=load_config(),
            md_path=args.md,
            html_path=html_path,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
