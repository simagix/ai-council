"""Local web server for ai-council: browser UI + history + live updates.

No third-party dependencies — the server is built on ``http.server`` and
serves one self-contained HTML page (CSS + JS inlined, no external
assets).

Features
--------
* ``python ai_council.py --server`` runs a web daemon on ``127.0.0.1:8080`` by default
  (``--host``/``--port`` override it; ``--daemonize``/``--stop`` manage a
  background process).
* ``POST /api/sessions`` accepts ``{"question": ..., "file": {"name", "
  content"}}`` — a question typed by the user plus a context document
  picked or dropped in the browser.
* Each council runs in a worker thread; structural changes persist via
  the session store and live token streams reach the UI over
  Server-Sent Events (``GET /api/sessions/{id}/events``).
* ``GET /api/sessions/{id}/download?format=report|md|html`` downloads the
  "Council Final Report" or a full transcript.

The council orchestration itself is untouched: ``preflight`` +
``run_council`` from :mod:`council` are called with the same callbacks the
CLI uses.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import store as store_module
from cli import markdown_to_html, save_html_transcript, save_markdown_transcript
from config import Config, load_config
from council import (
    CouncilError,
    CouncilResult,
    MemberFailure,
    MemberResponse,
    preflight,
    run_council,
)
from ollama import (
    ModelNotFoundError,
    OllamaClient,
    OllamaConnectionError,
    OllamaError,
    ThinkingStreamFilter,
)
from store import STATUS_ABORTED, STATUS_COMPLETE, STATUS_FAILED, member_to_dict

try:
    __version__ = (
        (Path(__file__).resolve().parent / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    )
except OSError:
    __version__ = "0.2.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
MAX_QUESTION_CHARS = 200_000
MAX_UPLOAD_CHARS = 1_000_000

_STAGE_PHASE = {"ROUND 1": 1, "ROUND 2": 2, "FINAL COUNCIL REPORT": 3}


def _real_client(config: Config) -> OllamaClient:
    return OllamaClient(config.ollama_host, config.timeout_seconds)


def _friendly_error(exc: Exception) -> str:
    """Human-readable preflight errors, mirroring the CLI's wording."""
    if isinstance(exc, OllamaConnectionError):
        return "Cannot connect to Ollama.\n\nMake sure Ollama is running and try again."
    if isinstance(exc, ModelNotFoundError):
        return (
            f"Model not found: {exc.model}\n\n"
            f"Install it with:\n\nollama pull {exc.model}"
        )
    return str(exc)
# ---------------------------------------------------------------------------
# session snapshots + restores (for the API and transcript downloads)
# ---------------------------------------------------------------------------

def _phase_for_stage(title: str) -> int:
    for marker, phase in _STAGE_PHASE.items():
        if marker in title.upper():
            return phase
    return 3


def snapshot(session: "store_module.Session") -> Dict[str, Any]:
    """Public view of a session for the JSON API (strings, no objects)."""
    return {
        "id": session.id,
        "question": session.question,
        "question_html": markdown_to_html(session.question),
        "created_at": session.created_at,
        "completed_at": session.completed_at,
        "status": session.status,
        "error": session.error,
        "stage": session.stage,
        "phase": session.phase,
        "current_member": session.current_member,
        "members": session.members,
        "round1": session.round1,
        "round2": session.round2,
        "report": session.report,
        "report_html": session.report_html,
        "report_member": session.report_member,
        "failures": session.failures,
        "notes": session.notes,
    }


def restore_result(session: "store_module.Session") -> CouncilResult:
    """Rebuild a :class:`council.CouncilResult` so the CLI's transcript
    renderers can be reused for downloads."""

    def responses(entries: List[Dict[str, Any]]) -> List[MemberResponse]:
        from models import CouncilMember

        return [
            MemberResponse(CouncilMember(**entry["member"]), entry["text"])
            for entry in entries
        ]

    def failures() -> List[MemberFailure]:
        from models import CouncilMember

        return [
            MemberFailure(CouncilMember(**f["member"]), f["stage"], f["error"])
            for f in session.failures
        ]

    report_member = None
    if session.report_member is not None:
        from models import CouncilMember

        report_member = CouncilMember(**session.report_member)
    return CouncilResult(
        question=session.question,
        round1=responses(session.round1),
        round2=responses(session.round2),
        failures=failures(),
        notes=list(session.notes),
        report=session.report,
        report_member=report_member,
    )


# ---------------------------------------------------------------------------
# CouncilServer
# ---------------------------------------------------------------------------

class CouncilServer:
    """Owns the HTTP daemon, the session store, and council worker threads."""

    def __init__(
        self,
        host: str,
        port: int,
        store: "store_module.SessionStore",
        config_factory: Optional[Callable[[], Config]] = None,
        client_factory: Optional[Callable[[Config], Any]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.store = store
        self._config_factory = config_factory or load_config
        self._client_factory = client_factory or _real_client
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self.httpd is not None:
            return
        self.httpd = ThreadingHTTPServer((self.host, self.port), CouncilHandler)
        self.httpd.daemon_threads = True
        self.httpd.council = self  # handler reaches us through self.server
        self.port = self.httpd.server_address[1]
        self._http_thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="ai-council-http",
            daemon=True,
        )
        self._http_thread.start()

    def stop(self) -> None:
        httpd = self.httpd
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            self.httpd = None
        self._http_thread = None

    # -- session lifecycle ---------------------------------------------------

    def create_session(self, question: str) -> "store_module.Session":
        """Create a session and start its council in a background thread."""
        config = self._config_factory()
        members = [member_to_dict(member) for member in config.members]
        session = self.store.create(question, members=members)
        client = self._client_factory(config)
        worker = threading.Thread(
            target=self._worker,
            args=(session, config, client),
            name=f"council-{session.id[:8]}",
            daemon=True,
        )
        worker.start()
        return session

    def _worker(
        self,
        session: "store_module.Session",
        config: Config,
        client: Any,
    ) -> None:
        """Run preflight + the full council and persist every milestone."""
        try:
            preflight(client, config)
        except (OllamaConnectionError, ModelNotFoundError, OllamaError) as exc:
            self.store.set_status(
                session, STATUS_ABORTED, error=_friendly_error(exc)
            )
            return

        live = {"filter": ThinkingStreamFilter()}

        def on_stage(title: str) -> None:
            self.store.set_stage(session, title, _phase_for_stage(title))

        def on_request(member, stage_label: str) -> None:
            live["filter"] = ThinkingStreamFilter()
            self.store.set_current_member(
                session, member_to_dict(member), stage_label
            )

        def on_token(token: str) -> None:
            visible = live["filter"].feed(token)
            if visible:
                self.store.publish_token(session, visible)

        def on_response(member, text: str, stage_label: str) -> None:
            round_key = {"Round 1": "round1", "Round 2": "round2"}.get(
                stage_label
            )
            if round_key is None:
                # Moderator / other stages are persisted via set_report().
                return
            entry = {
                "member": member_to_dict(member),
                "text": text,
                "html": markdown_to_html(text),
            }
            self.store.add_response(session, round_key, entry, stage_label)

        def on_failure(member, stage_label: str, error: str) -> None:
            entry = {
                "member": member_to_dict(member),
                "stage": stage_label,
                "error": error,
            }
            self.store.add_failure(session, entry, stage_label)

        try:
            result = run_council(
                session.question,
                config,
                client,
                on_stage=on_stage,
                on_request=on_request,
                on_response=on_response,
                on_failure=on_failure,
                on_token=on_token,
            )
        except CouncilError as exc:
            self.store.set_status(session, STATUS_FAILED, error=str(exc))
            return
        except Exception as exc:  # never leave a session stuck "running"
            self.store.set_status(
                session, STATUS_FAILED, error=f"Unexpected error: {exc}"
            )
            return

        if result.notes:
            self.store.add_notes(session, result.notes)

        if result.report is not None:
            self.store.set_report(
                session,
                result.report,
                markdown_to_html(result.report),
                member_to_dict(result.report_member)
                if result.report_member is not None
                else None,
            )
            self.store.set_status(session, STATUS_COMPLETE)
        else:
            self.store.set_status(
                session,
                STATUS_FAILED,
                error="The moderator did not respond; no final report was "
                "produced.",
            )
# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class CouncilHandler(BaseHTTPRequestHandler):
    """Routes the small JSON API and serves the single-page app."""

    server_version = "AI-Council/" + __version__

    # -- helpers ------------------------------------------------------------

    @property
    def council(self) -> "CouncilServer":
        return self.server.council  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path == "/favicon.ico":
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        detail = (fmt % args) if args else ""
        sys.stderr.write(f"[{stamp}] {self.command} {self.path} — {detail}\n")

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _split_path(path: str) -> Optional[List[str]]:
        parsed = urlparse(path)
        prefix = "/api/sessions/"
        if not parsed.path.startswith(prefix):
            return None
        rest = parsed.path[len(prefix):]
        return rest.split("/") if rest else None

    # -- routing -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/":
            return self._index()
        if path == "/favicon.ico":
            return self.send_error(404)
        if path == "/api/health":
            return self._health()
        if path == "/api/history":
            return self._history()

        parts = self._split_path(self.path)
        if parts is not None:
            session_id = parts[0]
            if len(parts) == 1:
                return self._session_get(session_id)
            if len(parts) == 2:
                if parts[1] == "events":
                    return self._sse(session_id)
                if parts[1] == "download":
                    return self._download(session_id, query)
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path == "/api/sessions":
            return self._session_create()
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parts = self._split_path(self.path)
        if parts is not None and len(parts) == 1:
            return self._session_delete(parts[0])
        self._json(404, {"error": "not found"})
# ---------------------------------------------------------------------------
# -- endpoints -------------------------------------------------------------

    def _index(self) -> None:
        body = _INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _health(self) -> None:
        config = self.council._config_factory()
        healthy = False
        try:
            self.council._client_factory(config).list_models()
            healthy = True
        except (OllamaError, OSError, ValueError):
            healthy = False
        self._json(
            200,
            {
                "status": "ok",
                "version": __version__,
                "ollama_host": config.ollama_host,
                "ollama": healthy,
            },
        )

    def _history(self) -> None:
        self._json(200, {"sessions": self.council.store.list()})

    def _session_get(self, session_id: str) -> None:
        session = self.council.store.get(session_id)
        if session is None:
            return self._json(404, {"error": "session not found"})
        self._json(200, snapshot(session))

    def _session_create(self) -> None:
        data = self._read_json_body()
        if not isinstance(data, dict):
            return self._json(400, {"error": "invalid JSON body"})

        question = str(data.get("question") or "").strip()
        file_meta = data.get("file")
        if isinstance(file_meta, dict):
            name = str(file_meta.get("name") or "document.txt")
            content = str(file_meta.get("content") or "")
            if len(content) > MAX_UPLOAD_CHARS:
                return self._json(400, {"error": "context file is too large"})
            if content.strip():
                context = f"Context document — {name}:\n\n{content}"
                question = f"{question}\n\n---\n\n{context}" if question else context

        if not question.strip():
            return self._json(400, {"error": "Enter a question or attach a file."})
        if len(question) > MAX_QUESTION_CHARS:
            return self._json(400, {"error": "question is too long"})

        session = self.council.create_session(question)
        self._json(201, snapshot(session))

    def _session_delete(self, session_id: str) -> None:
        deleted = self.council.store.delete(session_id)
        if not deleted:
            return self._json(404, {"error": "session not found"})
        self._json(200, {"ok": True})

    def _download(self, session_id: str, query: Dict[str, List[str]]) -> None:
        session = self.council.store.get(session_id)
        if session is None:
            return self._json(404, {"error": "session not found"})

        fmt = (query.get("format") or ["report"])[0]
        if fmt not in ("report", "md", "html"):
            return self._json(400, {"error": "format must be report, md or html"})

        result = restore_result(session)
        if fmt == "report":
            content, name, media = (
                self._report_markdown(result),
                f"ai-council-{session.id}-report.md",
                "text/markdown",
            )
        elif fmt == "md":
            content, name, media = (
                self._transcript_markdown(result),
                f"ai-council-{session.id}-transcript.md",
                "text/markdown",
            )
        else:
            content, name, media = (
                self._transcript_html(result),
                f"ai-council-{session.id}-transcript.html",
                "text/html",
            )

        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{media}; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _report_markdown(result: CouncilResult) -> str:
        header = "# Council Final Report\n\n"
        header += f"Question:\n\n{result.question}\n\n---\n\n"
        if result.report is None:
            return header + "_The moderator did not produce a report._"
        return header + result.report

    def _transcript_markdown(self, result: CouncilResult) -> str:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".md", prefix="ai-council-")
        try:
            save_markdown_transcript(path, result, io.StringIO())
            return Path(path).read_text(encoding="utf-8")
        finally:
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass

    def _transcript_html(self, result: CouncilResult) -> str:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".html", prefix="ai-council-")
        try:
            save_html_transcript(path, result, io.StringIO())
            return Path(path).read_text(encoding="utf-8")
        finally:
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- Server-Sent Events ---------------------------------------------------

    def _sse(self, session_id: str) -> None:
        session = self.council.store.get(session_id)
        if session is None:
            return self._json(404, {"error": "session not found"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_seq = -1
        try:
            while True:
                with session.cond:
                    timed_out = not session.cond.wait(timeout=15.0)
                if timed_out:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                with session.cond:
                    new_events = [
                        event for event in session.events if event["seq"] > last_seq
                    ]
                for event in new_events:
                    last_seq = max(last_seq, event["seq"])
                    self._sse_send(event)
                    if event["type"] == "deleted":
                        return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _sse_send(self, event: Dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"id: {event['seq']}\n".encode("utf-8"))
        self.wfile.write(f"event: {event['type']}\n".encode("utf-8"))
        for line in payload.splitlines() or [""]:
            self.wfile.write(f"data: {line}\n".encode("utf-8"))
        self.wfile.write(b"\n")
        self.wfile.flush()
# ---------------------------------------------------------------------------
# the ``--server`` flag
# ---------------------------------------------------------------------------

def _command_script() -> str:
    """Absolute path to the script the user invoked (works for
    ``python ai_council.py --server`` and any console-script wrapper)."""
    script = os.path.abspath(sys.argv[0])
    return script if os.path.exists(script) else sys.executable


def _spawn_daemon(
    host: str, port: int, data_dir: str, log_file: str, pid_file: str
) -> int:
    if os.name == "nt":
        print("--daemonize is only supported on macOS/Linux.", file=sys.stderr)
        return 1
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        _command_script(),
        "--server",
        "--host", host,
        "--port", str(port),
        "--data-dir", data_dir,
        "--no-open",
    ]
    devnull = open(os.devnull, "w")
    log = open(log_file, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=devnull,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        devnull.close()
        # log stays open in the child; don't close here.
    Path(pid_file).write_text(str(proc.pid), encoding="utf-8")
    print(f"ai-council started in the background (pid {proc.pid}).")
    print(f"  URL:  http://{host}:{port}")
    print(f"  Data: {data_dir}")
    print(f"  Log:  {log_file}")
    print("  Stop: python ai_council.py --server --stop")
    return 0


def _stop_daemon(pid_file: str) -> int:
    path = Path(pid_file)
    if not path.exists():
        print(f"No server pid file at {pid_file} — is the server running?", file=sys.stderr)
        return 1
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        print(f"Could not read pid file {pid_file}.", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"No process with pid {pid} — removing stale pid file.")
        path.unlink(missing_ok=True)
        return 1
    path.unlink(missing_ok=True)
    print(f"Stopped ai-council server (pid {pid}).")
    return 0


def _open_browser(url: str) -> None:
    time.sleep(0.6)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def serve_main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``ai-council --server``."""
    parser = argparse.ArgumentParser(
        prog="ai_council.py --server",
        description="Run the ai-council web interface as a local server.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"host to bind (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to bind (default {DEFAULT_PORT})")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="directory for session history (default $AI_COUNCIL_DATA_DIR or ~/.ai-council)",
    )
    parser.add_argument(
        "--daemonize",
        action="store_true",
        help="fork into the background and return immediately",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop the background daemon and exit",
    )
    parser.add_argument(
        "--pid-file", default=None, help="pid file used by --daemonize/--stop"
    )
    parser.add_argument(
        "--log-file", default=None, help="log file used by --daemonize"
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="do not auto-open a browser tab on startup",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ai-council --server v{__version__}",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    data_dir = (
        args.data_dir
        or os.environ.get("AI_COUNCIL_DATA_DIR")
        or str(Path.home() / ".ai-council")
    )
    pid_file = args.pid_file or str(Path(data_dir) / "server.pid")
    log_file = args.log_file or str(Path(data_dir) / "server.log")

    if args.stop:
        return _stop_daemon(pid_file)

    if args.daemonize:
        return _spawn_daemon(args.host, args.port, data_dir, log_file, pid_file)

    store = store_module.SessionStore(data_dir)
    server = CouncilServer(args.host, args.port, store)
    server.start()
    url = f"http://{args.host}:{server.port}"
    print(f"┌──────────────────────────────────────────────")
    print(f"  AI Council v{__version__} — web interface")
    print(f"  URL:  {url}")
    print(f"  Data: {data_dir}")
    print(f"  Stop: Ctrl-C")
    print(f"└──────────────────────────────────────────────")

    if not args.no_open:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.stop()
    return 0
_APP_CSS = r"""
:root {
  --bg: #070c1d;
  --bg-2: #0b1230;
  --panel: #0e1535;
  --card: #141c45;
  --card-2: #1a2457;
  --line: #253067;
  --line-2: #33407f;
  --ink: #e8ecff;
  --muted: #9aa4cc;
  --faint: #66719f;
  --brand: #6366f1;
  --brand-2: #a855f7;
  --good: #34d399;
  --warn: #fbbf24;
  --bad: #fb7185;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  min-height: 100vh;
  color: var(--ink);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  background:
    radial-gradient(1100px 600px at 85% -10%, rgba(99, 102, 241, 0.16), transparent 60%),
    radial-gradient(900px 500px at -10% 110%, rgba(168, 85, 247, 0.12), transparent 55%),
    linear-gradient(180deg, var(--bg), var(--bg-2) 60%, var(--bg));
  background-attachment: fixed;
}
button { font: inherit; cursor: pointer; }
a { color: var(--brand); }
::selection { background: rgba(99,102,241,.35); }

/* ---- top bar ---- */
.topbar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; height: 60px; padding: 0 20px;
  background: rgba(7, 12, 29, 0.82);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--line);
}
.brand { display: flex; align-items: baseline; gap: 10px; font-size: 19px; font-weight: 700; letter-spacing: .2px; }
.brand .mark { color: var(--brand-2); font-size: 20px; }
.brand .ver { color: var(--faint); font-size: 12px; font-weight: 500; }
.top-actions { display: flex; align-items: center; gap: 12px; }
.health { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
.health .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); box-shadow: 0 0 8px var(--warn); }
.health.ok .dot { background: var(--good); box-shadow: 0 0 8px var(--good); }

/* ---- buttons ---- */
.btn {
  display: inline-flex; align-items: center; gap: 8px;
  border: 1px solid var(--line-2); border-radius: 11px;
  background: var(--card-2); color: var(--ink);
  padding: 9px 16px; font-size: 14px; font-weight: 600;
  transition: transform .08s ease, border-color .15s ease, box-shadow .15s ease, filter .15s ease;
}
.btn:hover { border-color: var(--brand); box-shadow: 0 4px 18px rgba(99,102,241,.25); }
.btn:active { transform: translateY(1px); }
.btn.primary {
  background: linear-gradient(135deg, var(--brand), var(--brand-2));
  border: none; color: #fff; padding: 12px 24px; font-size: 15px;
  box-shadow: 0 8px 26px rgba(99, 102, 241, 0.35);
}
.btn.primary:hover { filter: brightness(1.08); box-shadow: 0 10px 30px rgba(99,102,241,.45); }
.btn.primary:disabled { opacity: .55; cursor: not-allowed; box-shadow: none; }
.btn.ghost { background: transparent; }
.btn.danger { color: var(--bad); border-color: rgba(251,113,133,.35); }
.btn.danger:hover { border-color: var(--bad); box-shadow: 0 4px 18px rgba(251,113,133,.2); }
.arr { transition: transform .15s ease; }
.btn.primary:hover .arr { transform: translateX(4px); }

/* ---- layout ---- */
.layout { display: grid; grid-template-columns: 300px 1fr; gap: 0; min-height: calc(100vh - 60px); }
.sidebar {
  border-right: 1px solid var(--line);
  padding: 18px 14px;
  overflow-y: auto;
  background: rgba(10, 15, 40, 0.35);
}
.side-title { color: var(--faint); font-size: 11px; text-transform: uppercase; letter-spacing: 1.4px; padding: 4px 8px 10px; }
.history { display: flex; flex-direction: column; gap: 8px; }
.hist-item {
  text-align: left; width: 100%;
  background: var(--card); border: 1px solid transparent; border-radius: 12px;
  padding: 11px 13px; color: var(--ink);
  transition: border-color .15s ease, background .15s ease, transform .1s ease;
}
.hist-item:hover { border-color: var(--line-2); background: var(--card-2); }
.hist-item.active { border-color: var(--brand); background: linear-gradient(180deg, rgba(99,102,241,.16), rgba(168,85,247,.10)); }
.hist-row { display: flex; gap: 8px; align-items: flex-start; }
.hist-q { font-size: 13.5px; line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.hist-meta { margin-top: 6px; color: var(--faint); font-size: 11.5px; }
.hist-item .dot { margin-top: 4px; flex: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; display: inline-block; }
.dot.running { background: var(--warn); box-shadow: 0 0 0 0 rgba(251,191,36,.6); animation: pulse 1.8s infinite; }
.dot.complete { background: var(--good); }
.dot.failed, .dot.aborted { background: var(--bad); }
.dot.interrupted { background: var(--faint); }
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(251,191,36,.55); }
  70% { box-shadow: 0 0 0 8px rgba(251,191,36,0); }
  100% { box-shadow: 0 0 0 0 rgba(251,191,36,0); }
}
.empty { color: var(--faint); font-size: 13px; padding: 14px 8px; line-height: 1.6; }"""
_APP_CSS_2 = r"""
/* ---- stage / home ---- */
.stage { padding: 28px 34px 60px; max-width: 1060px; width: 100%; margin: 0 auto; }
.view { animation: fadein .28s ease; }
@keyframes fadein { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
.hero { text-align: center; margin: 26px 0 30px; }
.kicker { color: var(--brand-2); letter-spacing: 2.4px; font-size: 11px; font-weight: 700; text-transform: uppercase; margin: 0 0 12px; }
.hero h1 {
  margin: 0 0 10px; font-size: clamp(30px, 5vw, 44px); font-weight: 800; letter-spacing: -.5px;
  background: linear-gradient(100deg, #eef1ff 10%, #a5b4fc 55%, var(--brand-2) 95%);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.hero-sub { margin: 0 auto; max-width: 560px; color: var(--muted); font-size: 15.5px; }
.form-card {
  background: linear-gradient(180deg, rgba(20,28,69,.9), rgba(16,23,54,.9));
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 26px 28px 22px;
  box-shadow: 0 22px 60px rgba(0,0,0,.35);
}
.lbl { display: block; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; color: var(--muted); margin-bottom: 10px; }
#question {
  width: 100%; resize: vertical; min-height: 118px;
  background: rgba(7,12,29,.55); border: 1px solid var(--line-2); border-radius: 14px;
  color: var(--ink); font: inherit; line-height: 1.65; padding: 14px 16px;
  outline: none; transition: border-color .15s ease, box-shadow .15s ease;
}
#question:focus { border-color: var(--brand); box-shadow: 0 0 0 4px rgba(99,102,241,.18); }
#question::placeholder { color: var(--faint); }
.drop {
  margin-top: 16px; border: 1.6px dashed var(--line-2); border-radius: 15px;
  padding: 22px; display: flex; align-items: center; justify-content: center;
  transition: border-color .15s ease, background .15s ease;
  background: rgba(7,12,29,.3);
}
.drop.dragover, .drop:hover { border-color: var(--brand); background: rgba(99,102,241,.08); }
.drop-inner { display: flex; align-items: center; gap: 14px; width: 100%; justify-content: center; flex-wrap: wrap; }
.drop-icon { font-size: 26px; color: var(--brand-2); }
.drop-copy { text-align: left; }
.drop-title { font-weight: 600; font-size: 14.5px; }
.drop-sub { color: var(--muted); font-size: 13px; }
.chip {
  display: inline-flex; align-items: center; gap: 8px;
  background: rgba(99,102,241,.16); border: 1px solid var(--brand);
  border-radius: 999px; padding: 6px 12px; font-size: 13px; max-width: 100%;
}
.chip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; }
.chip-x { border: none; background: transparent; color: var(--brand-2); font-size: 14px; padding: 0 2px; }
.form-foot { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 20px; flex-wrap: wrap; }
.hint { color: var(--faint); font-size: 12.5px; }
.flow { display: flex; align-items: center; justify-content: center; gap: 10px; margin: 26px auto 0; flex-wrap: wrap; max-width: 820px; }
.flow-step { display: flex; align-items: center; gap: 9px; background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 9px 14px; font-size: 13px; color: var(--muted); }
.flow-step b { color: var(--brand-2); font-size: 13px; }
.flow-arrow { color: var(--faint); font-size: 16px; }
"""
_APP_CSS_3 = r"""
/* ---- session ---- */
.sess-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; }
.sess-info { min-width: 0; }
.sess-status { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.stat-badge { font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; padding: 3px 11px; border-radius: 999px; }
.stat-badge.running { color: #fcd34d; background: rgba(251,191,36,.14); border: 1px solid rgba(251,191,36,.35); }
.stat-badge.complete { color: #6ee7b7; background: rgba(52,211,153,.14); border: 1px solid rgba(52,211,153,.35); }
.stat-badge.failed, .stat-badge.aborted { color: #fda4af; background: rgba(251,113,133,.14); border: 1px solid rgba(251,113,133,.35); }
.stat-badge.interrupted { color: #cbd5e1; background: rgba(148,163,184,.14); border: 1px solid rgba(148,163,184,.35); }
.sess-date { color: var(--faint); font-size: 12.5px; }
.sess-title { margin: 0 0 12px; font-size: 22px; font-weight: 700; letter-spacing: -.2px; }
.members-strip { display: flex; gap: 8px; flex-wrap: wrap; }
.member-chip { display: inline-flex; align-items: center; gap: 7px; background: var(--card); border: 1px solid var(--line); border-radius: 999px; padding: 4px 12px 4px 5px; font-size: 12.5px; color: var(--muted); }
.member-chip .m-ava { width: 22px; height: 22px; font-size: 11px; }
.sess-actions { display: flex; gap: 10px; align-items: center; position: relative; }
.dl-menu { position: absolute; right: 0; top: 44px; z-index: 10; display: flex; flex-direction: column; gap: 4px; background: var(--card-2); border: 1px solid var(--line-2); border-radius: 12px; padding: 6px; min-width: 210px; box-shadow: 0 14px 40px rgba(0,0,0,.5); }
.dl-menu button { text-align: left; border: none; background: transparent; color: var(--ink); border-radius: 8px; padding: 9px 12px; font-size: 13.5px; }
.dl-menu button:hover { background: rgba(99,102,241,.16); }

/* stepper */
.stepper { list-style: none; display: flex; margin: 0 0 18px; padding: 0; gap: 0; }
.stepper li { flex: 1; display: flex; align-items: center; gap: 10px; position: relative; }
.stepper li::before { content: ""; position: absolute; top: 14px; left: -50%; right: 50%; height: 2px; background: var(--line-2); }
.stepper li:first-child::before { display: none; }
.stepper li.done::before { background: var(--brand); }
.stepper .dot { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 50%; background: var(--card-2); border: 2px solid var(--line-2); color: var(--faint); font-size: 13px; font-weight: 700; position: relative; z-index: 1; line-height: 1; }
.stepper li.done .dot { background: linear-gradient(135deg, var(--brand), var(--brand-2)); border-color: transparent; color: #fff; }
.stepper li.done .dot::after { content: "✓"; }
.stepper li.active .dot { border-color: var(--brand); color: var(--brand); animation: pulse 1.6s infinite; }
.stepper li.fail .dot { border-color: var(--bad); color: var(--bad); }
.stepper li.fail .dot::after { content: "!"; }
.stepper span { display: flex; flex-direction: column; line-height: 1.25; }
.stepper b { font-size: 13px; }
.stepper small { color: var(--faint); font-size: 11px; }

/* tabs */
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin-bottom: 20px; overflow-x: auto; }
.tab { border: none; background: transparent; color: var(--muted); font-size: 14px; font-weight: 600; padding: 10px 16px; border-bottom: 2.5px solid transparent; margin-bottom: -1px; transition: color .15s ease, border-color .15s ease; white-space: nowrap; }
.tab:hover { color: var(--ink); }
.tab.active { color: var(--ink); border-bottom-color: var(--brand); }
.tab[hidden] { display: none; }
.pane { animation: fadein .25s ease; }
"""
_APP_CSS_4 = r"""
/* cards + members */
.card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 20px 22px; margin-bottom: 16px; }
.q-card { background: linear-gradient(180deg, rgba(20,28,69,.85), rgba(16,23,54,.85)); }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 16px; }
.member { background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--accent, var(--brand)); border-radius: 14px; padding: 16px 18px; }
.m-head { display: flex; align-items: center; gap: 11px; margin-bottom: 12px; }
.m-ava { display: grid; place-items: center; flex: none; width: 34px; height: 34px; border-radius: 50%; font-weight: 700; font-size: 15px; }
.m-id { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1; min-width: 0; }
.m-name { font-weight: 700; font-size: 14.5px; }
.badge { font-size: 11px; font-weight: 700; padding: 2px 9px; border-radius: 999px; letter-spacing: .3px; white-space: nowrap; }
.m-body { color: #dce2fb; font-size: 14.5px; }
.m-body h1, .m-body h2, .m-body h3, .m-body h4 { margin: 14px 0 6px; font-size: 16px; color: var(--ink); }
.m-body p { margin: 8px 0; }
.m-body ul, .m-body ol { margin: 8px 0; padding-left: 22px; }
.m-body li { margin: 3px 0; }
.m-body code { background: rgba(120,140,220,.14); border-radius: 6px; padding: 1px 5px; font-size: 13px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.m-body pre { background: rgba(7,12,29,.6); border: 1px solid var(--line); border-radius: 10px; padding: 12px; overflow: auto; }
.m-body blockquote { margin: 8px 0; padding: 4px 14px; border-left: 3px solid var(--brand); color: var(--muted); }
.m-body hr { border: none; border-top: 1px solid var(--line); margin: 14px 0; }
.report-hero { background: linear-gradient(135deg, rgba(99,102,241,.20), rgba(168,85,247,.14)); border: 1px solid rgba(99,102,241,.45); }
.err-box { background: rgba(251,113,133,.1); border: 1px solid rgba(251,113,133,.35); border-radius: 14px; padding: 16px 18px; color: #fda4af; margin-bottom: 16px; white-space: pre-wrap; }
.note-box { background: rgba(251,191,36,.08); border: 1px solid rgba(251,191,36,.3); border-radius: 12px; padding: 12px 16px; color: #fcd34d; font-size: 13.5px; margin-bottom: 14px; }

/* live + toast + responsive */
.live-stage { display: flex; align-items: center; gap: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.6px; font-size: 12px; color: var(--brand-2); margin: 16px 0 10px; }
.live-stage::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.live-member { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; margin-bottom: 12px; }
.live-mhead { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.live-mhead b { font-size: 14px; }
.live-stage-label { color: var(--faint); font-size: 12px; margin-left: auto; }
.live-pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; line-height: 1.7; color: #cfd6f7; }
.live-pre::after { content: "▌"; color: var(--brand); animation: blink 1s steps(2) infinite; }
@keyframes blink { 50% { opacity: 0; } }
.live-ok { color: var(--good); font-size: 12px; font-weight: 700; }
.live-empty { color: var(--faint); font-size: 14px; padding: 30px 0; text-align: center; }
.toast { position: fixed; bottom: 26px; left: 50%; transform: translateX(-50%); z-index: 60; background: #1e2a5e; border: 1px solid var(--line-2); color: var(--ink); padding: 11px 20px; border-radius: 12px; font-size: 14px; box-shadow: 0 12px 34px rgba(0,0,0,.45); animation: fadein .2s ease; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--line-2); border-radius: 8px; }
::-webkit-scrollbar-track { background: transparent; }
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .sidebar { border-right: none; border-bottom: 1px solid var(--line); max-height: 230px; }
  .history { flex-direction: row; overflow-x: auto; }
  .hist-item { min-width: 260px; }
  .stage { padding: 18px 16px 50px; }
  .stepper span { display: none; }
  .stepper li { justify-content: center; }
}
"""
_APP_HTML = """
<div id="app">
  <header class="topbar">
    <div class="brand"><span class="mark">◈</span> AI Council <span class="ver" id="ver"></span></div>
    <div class="top-actions">
      <span class="health" id="health"><i class="dot"></i><span id="health-text">checking Ollama…</span></span>
      <button class="btn ghost" id="btn-new">＋ New session</button>
    </div>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="side-title">Session history</div>
      <div id="history" class="history"></div>
    </aside>
    <main class="stage">
      <section id="view-home" class="view">
        <div class="hero">
          <p class="kicker">Three local models · one deliberation</p>
          <h1>Summon the Council</h1>
          <p class="hero-sub">Independent opinions. Structured debate. A consensus report — or an honest admission of disagreement.</p>
        </div>
        <form id="form" class="form-card" autocomplete="off">
          <label class="lbl" for="question">The question</label>
          <textarea id="question" rows="5" placeholder="e.g.  Should I buy 256 GB or 512 GB of storage for my new Mac mini?"></textarea>
          <div class="drop" id="drop">
            <input type="file" id="file" hidden accept=".md,.txt,.markdown,.text,text/plain">
            <div class="drop-inner">
              <div class="drop-icon">⤒</div>
              <div class="drop-copy">
                <div class="drop-title">Drag &amp; drop a context file</div>
                <div class="drop-sub">or <a href="#" id="browse">browse</a> — .md / .txt appended as context</div>
              </div>
              <div class="chip" id="chip" hidden><span class="chip-name" id="chip-name"></span><button type="button" class="chip-x" id="chip-x" aria-label="remove file">✕</button></div>
            </div>
          </div>
          <div class="form-foot">
            <span class="hint">Runs fully on this machine via Ollama · history kept locally</span>
            <button class="btn primary" id="submit" type="submit"><span>Convene Council</span><span class="arr">→</span></button>
          </div>
        </form>
        <div class="flow">
          <div class="flow-step"><b>01</b><span>Round 1 · independent opinions</span></div>
          <div class="flow-arrow">→</div>
          <div class="flow-step"><b>02</b><span>Round 2 · council discussion</span></div>
          <div class="flow-arrow">→</div>
          <div class="flow-step"><b>03</b><span>Final report · moderator synthesis</span></div>
        </div>
      </section>

      <section id="view-session" class="view" hidden>
        <div class="sess-head">
          <div class="sess-info">
            <div class="sess-status"><span class="stat-badge" id="stat-badge">…</span><span class="sess-date" id="sess-date"></span></div>
            <h2 class="sess-title" id="sess-title"></h2>
            <div class="members-strip" id="members-strip"></div>
          </div>
          <div class="sess-actions">
            <button class="btn ghost" id="btn-report-dl" disabled>⬇ Download</button>
            <div class="dl-menu" id="dl-menu" hidden>
              <button data-fmt="report">Council Final Report (.md)</button>
              <button data-fmt="md">Full transcript (.md)</button>
              <button data-fmt="html">Full transcript (.html)</button>
            </div>
            <button class="btn ghost danger" id="btn-delete">Delete</button>
          </div>
        </div>

        <ol class="stepper" id="stepper">
          <li data-idx="0"><i class="dot"></i><span><b>Question</b><small>received</small></span></li>
          <li data-idx="1"><i class="dot"></i><span><b>Round 1</b><small>opinions</small></span></li>
          <li data-idx="2"><i class="dot"></i><span><b>Round 2</b><small>discussion</small></span></li>
          <li data-idx="3"><i class="dot"></i><span><b>Report</b><small>moderator</small></span></li>
        </ol>

        <nav class="tabs" id="tabs">
          <button data-tab="live" class="tab live-tab" hidden>Live</button>
          <button data-tab="question" class="tab">Question</button>
          <button data-tab="round1" class="tab">Round 1</button>
          <button data-tab="round2" class="tab">Round 2</button>
          <button data-tab="report" class="tab">Final Report</button>
        </nav>

        <section id="pane-live" class="pane" hidden><div id="live-log"></div></section>
        <section id="pane-question" class="pane" hidden></section>
        <section id="pane-round1" class="pane" hidden></section>
        <section id="pane-round2" class="pane" hidden></section>
        <section id="pane-report" class="pane" hidden></section>
      </section>
    </main>
  </div>
</div>
<div id="toast" class="toast" hidden></div>
"""
_APP_JS = """
"use strict";

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);
const ROLE_COLORS = {
  "Analyst": "#60a5fa",
  "Independent Thinker": "#34d399",
  "Skeptic": "#fbbf24",
  "Moderator": "#f472b6"
};
const STATUS_LABEL = {
  running: "Running",
  complete: "Complete",
  failed: "No report",
  aborted: "Aborted",
  interrupted: "Interrupted"
};
function roleColor(role) { return ROLE_COLORS[role] || "#a78bfa"; }
function esc(s) {
  const d = document.createElement("div");
  d.textContent = (s == null) ? "" : String(s);
  return d.innerHTML;
}
function fmtTime(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
}
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { t.hidden = true; }, 2800);
}
let toastTimer = null;
async function api(path, opts) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (err) { /* non-JSON */ }
  if (!res.ok) {
    const msg = (data && data.error) ? data.error : res.statusText || ("HTTP " + res.status);
    throw new Error(msg);
  }
  return data;
}

/* ------------------------------------------------------------------ */
/* app state                                                           */
/* ------------------------------------------------------------------ */
let SESSION = null;       // current snapshot object
let SSE = null;           // EventSource
let lastSeq = -1;         // last SSE event id seen
let liveCursor = null;    // element receiving streamed tokens
let attachedFile = null;  // {name, content}

/* ------------------------------------------------------------------ */
/* health + history                                                    */
/* ------------------------------------------------------------------ */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("ver").textContent = "v" + (h.version || "");
    const el = $("health"), tx = $("health-text");
    el.classList.toggle("ok", !!h.ollama);
    tx.textContent = h.ollama ? "Ollama connected" : "Ollama offline — start it to convene";
  } catch (err) {
    $("health-text").textContent = "server unreachable";
  }
}
async function refreshHistory() {
  let items;
  try { items = (await api("/api/history")).sessions || []; }
  catch (err) { return; }
  const el = $("history");
  el.innerHTML = "";
  if (!items.length) {
    el.innerHTML = '<div class="empty">No sessions yet.<br>Ask your first question.</div>';
    return;
  }
  items.forEach((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "hist-item" + (SESSION && SESSION.id === s.id ? " active" : "");
    b.innerHTML =
      '<div class="hist-row"><i class="dot ' + esc(s.status) + '"></i>' +
      '<span class="hist-q">' + esc(s.question) + "</span></div>" +
      '<div class="hist-meta">' + esc(fmtTime(s.created_at)) + "</div>";
    b.addEventListener("click", () => openSession(s.id));
    el.appendChild(b);
  });
}

/* ------------------------------------------------------------------ */
/* home form + file drop                                               */
/* ------------------------------------------------------------------ */
function showChip() {
  if (!attachedFile) return;
  $("chip-name").textContent = attachedFile.name + "  ·  " +
    Math.max(1, Math.round(attachedFile.content.length / 1024)) + " KB";
  $("chip").hidden = false;
}
function clearFile() {
  attachedFile = null;
  $("chip-name").textContent = "";
  $("chip").hidden = true;
  $("file").value = "";
  $("drop").classList.remove("dragover");
}
function readFile(f) {
  if (f.size > 1048576) { toast("File is too large (max 1 MB)."); return; }
  const r = new FileReader();
  r.onload = () => {
    attachedFile = { name: f.name, content: String(r.result || "") };
    showChip();
  };
  r.onerror = () => toast("Could not read that file.");
  r.readAsText(f);
}
function bindDrop() {
  const drop = $("drop"), file = $("file");
  ["dragenter", "dragover"].forEach((t) =>
    drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((t) =>
    drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) readFile(f);
  });
  $("browse").addEventListener("click", (e) => { e.preventDefault(); file.click(); });
  file.addEventListener("change", () => { if (file.files && file.files[0]) readFile(file.files[0]); });
  $("chip-x").addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    clearFile();
    $("chip").hidden = true;
  });
}
async function submitSession(ev) {
  ev.preventDefault();
  const question = $("question").value.trim();
  if (!question && !attachedFile) { toast("Enter a question or drop a file."); return; }
  const body = { question: question };
  if (attachedFile) body.file = { name: attachedFile.name, content: attachedFile.content };
  const btn = $("submit");
  btn.disabled = true;
  try {
    const snap = await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    $("question").value = "";
    clearFile();
    openSession(snap.id, snap);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* rendering                                                           */
/* ------------------------------------------------------------------ */
function memberCard(m) {
  const c = roleColor(m.role);
  const initial = esc((m.name || "?").trim().charAt(0).toUpperCase());
  const name = esc(m.name);
  const role = esc(m.role);
  return `
    <article class="member" style="--accent:${c}">
      <div class="m-head">
        <span class="m-ava" style="background:${c}26;color:${c}">${initial}</span>
        <div class="m-id"><span class="m-name">${name}</span>
          <span class="badge" style="color:${c};background:${c}22">${role}</span>
        </div>
      </div>
      <div class="m-body">${m.html || ""}</div>
    </article>`;
}
function memberChips(members) {
  if (!members || !members.length) return "";
  return members.map((m) => {
    const c = roleColor(m.role);
    const initial = esc((m.name || "?").trim().charAt(0).toUpperCase());
    return `
      <span class="member-chip">
        <span class="m-ava" style="background:${c}26;color:${c}">${initial}</span>
        ${esc(m.name)} · ${esc(m.role)}
      </span>`;
  }).join("");
}
function failureBoxes(failures) {
  if (!failures || !failures.length) return "";
  const items = failures.map((f) => {
    const n = f.member ? f.member.name : "?";
    return "<b>" + esc(n) + "</b> failed during <b>" + esc(f.stage) + "</b>: " + esc(f.error);
  }).join("<br>");
  return '<div class="err-box">' + items + "</div>";
}
function noteBoxes(notes) {
  if (!notes || !notes.length) return "";
  return notes.map((n) => '<div class="note-box">' + esc(n) + "</div>").join("");
}
function renderStepper(s) {
  const steps = document.querySelectorAll("#stepper li");
  let phase = s.phase || 0;
  if (s.status === "complete") phase = 4;
  if (s.status !== "running" && phase < 4) phase = Math.min(4, phase);
  const failing = (s.status === "failed" || s.status === "aborted" || s.status === "interrupted");
  steps.forEach((li) => {
    const idx = parseInt(li.dataset.idx, 10);
    li.classList.toggle("done", idx < phase || s.status === "complete");
    li.classList.toggle("active", idx === phase && s.status === "running");
    li.classList.toggle("fail", failing && idx === Math.min(phase, 3));
  });
}
function renderSnapshot(s) {
  SESSION = s;

  const badge = $("stat-badge");
  badge.className = "stat-badge " + esc(s.status);
  badge.textContent = STATUS_LABEL[s.status] || s.status;

  $("sess-date").textContent = fmtTime(s.created_at) +
    (s.completed_at ? "  ·  completed " + fmtTime(s.completed_at) : "");

  const qOneLine = s.question.split(/\\r?\\n/)[0] || "Council session";
  $("sess-title").textContent = qOneLine.length > 140 ? qOneLine.slice(0, 137) + "…" : qOneLine;
  $("sess-title").title = s.question;
  $("members-strip").innerHTML = memberChips(s.members);

  const running = s.status === "running";
  $("tabs").querySelector('[data-tab="live"]').hidden = !running;

  $("pane-question").innerHTML =
    '<div class="card q-card"><h3 class="pane-hed" style="margin-bottom:14px">Question</h3>' +
    '<div class="m-body">' + (s.question_html || esc(s.question)) + "</div></div>" +
    failureBoxes(s.failures) + noteBoxes(s.notes);

  const mkRound = (title, entries) => {
    if (!entries || !entries.length) {
      return '<div class="card"><h3 class="pane-hed"><span>.</span></h3>' +
        '<div class="live-empty">' + title + " — no responses yet.</div></div>";
    }
    return '<div class="pane-hed"><h3>' + esc(title) + "</h3></div>" +
      '<div class="grid2">' + entries.map(memberCard).join("") + "</div>";
  };
  $("pane-round1").innerHTML = mkRound("Round 1 — Independent Opinions", s.round1);
  $("pane-round2").innerHTML = mkRound("Round 2 — Council Discussion", s.round2);

  let reportHtml = noteBoxes(s.notes);
  if (s.report_html) {
    const m = s.report_member || {};
    const modColor = roleColor("Moderator");
    reportHtml += `
      <div class="card report-hero">
        <div class="m-head" style="margin-bottom:16px">
          <span class="m-ava" style="background:${modColor}26;color:${modColor}">${esc((m.name || "M").charAt(0))}</span>
          <div class="m-id"><span class="m-name">${esc(m.name || "Moderator")}</span>
            <span class="badge" style="color:${modColor};background:${modColor}1f">Moderator</span>
          </div>
        </div>
        <div class="m-body">${s.report_html}</div>
      </div>
      ${failureBoxes(s.failures)}`;
  } else if (running) {
    reportHtml = '<div class="card q-card"><h3 class="pane-hed">Final report</h3>' +
      '<div class="live-empty">The moderator is still synthesizing the report…</div></div>';
  } else {
    reportHtml += '<div class="err-box">' +
      esc(s.error || "No final report was produced.") + "</div>";
  }
  $("pane-report").innerHTML = reportHtml;

  const reportReady = !!s.report_html;
  $("btn-report-dl").disabled = !reportReady;
  $("btn-report-dl").title = reportReady ? "Download the Council Final Report" : "Waiting for the report…";

  renderStepper(s);
}
function setTab(name) {
  document.querySelectorAll(".tabs .tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  ["live", "question", "round1", "round2", "report"].forEach((id) => {
    $("pane-" + id).hidden = id !== name;
  });
}

/* ------------------------------------------------------------------ */
/* navigation + live events                                            */
/* ------------------------------------------------------------------ */
function showSessionView() { $("view-home").hidden = true; $("view-session").hidden = false; }
function showHome() {
  stopSSE();
  SESSION = null;
  liveCursor = null;
  $("view-home").hidden = false;
  $("view-session").hidden = true;
  refreshHistory();
}
function scrollLive() {
  const log = $("live-log");
  if (log) log.scrollTop = log.scrollHeight;
}
async function openSession(id, snap) {
  stopSSE();
  liveCursor = null;
  lastSeq = -1;
  if (!snap) {
    try { snap = await api("/api/sessions/" + encodeURIComponent(id)); }
    catch (err) { toast(err.message); return; }
  }
  SESSION = snap;
  showSessionView();
  renderSnapshot(snap);
  $("pane-live").querySelector("#live-log").innerHTML = "";
  if (snap.status === "running") {
    setTab("live");
    connectSSE(snap.id);
  } else if (snap.status === "complete") {
    setTab("report");
  } else {
    setTab("report");
  }
  refreshHistory();
}
function connectSSE(id) {
  stopSSE();
  lastSeq = SESSION ? -1 : -1;
  const es = new EventSource("/api/sessions/" + encodeURIComponent(id) + "/events");
  SSE = es;
  [
    "start", "stage", "request", "token", "response",
    "failure", "status", "note", "report", "deleted"
  ].forEach((t) => es.addEventListener(t, (ev) => handleLive(t, ev)));
  es.onerror = () => { /* EventSource reconnects automatically */ };
}
function stopSSE() {
  if (SSE) { try { SSE.close(); } catch (e) { /* noop */ } SSE = null; }
}
async function refreshFromServer(finalStatus) {
  try {
    const snap = await api("/api/sessions/" + encodeURIComponent(SESSION.id));
    SESSION = snap;
    renderSnapshot(snap);
    refreshHistory();
  } catch (err) { /* session may have been deleted */ }
  if (finalStatus === "complete") {
    stopSSE();
    setTab("report");
  } else if (finalStatus && finalStatus !== "running") {
    stopSSE();
    setTab("report");
  }
}
function handleLive(type, ev) {
  let d;
  try { d = JSON.parse(ev.data); } catch (err) { return; }
  if (d.seq !== undefined && d.seq <= lastSeq) return;   // de-dupe on reconnect
  if (d.seq !== undefined) lastSeq = d.seq;

  const log = $("live-log");
  if (!log) return;

  if (type === "stage") {
    const h = document.createElement("div");
    h.className = "live-stage";
    h.textContent = d.title || "…";
    log.appendChild(h);
    scrollLive();
  } else if (type === "request") {
    const m = d.member || {};
    const c = roleColor(m.role);
    const blk = document.createElement("div");
    blk.className = "live-member";
    blk.innerHTML = `
      <div class="live-mhead">
        <span class="m-ava" style="background:${c}26;color:${c}">${esc((m.name || "?").charAt(0))}</span>
        <b>${esc(m.name)}</b>
        <span class="badge" style="color:${c};background:${c}22">${esc(m.role)}</span>
        <span class="live-stage-label">${esc(d.stage || "")}</span>
      </div>
      <pre class="live-pre"></pre>`;
    log.appendChild(blk);
    liveCursor = blk.querySelector(".live-pre");
    scrollLive();
  } else if (type === "token") {
    if (liveCursor) { liveCursor.textContent += d.text || ""; scrollLive(); }
  } else if (type === "response") {
    if (liveCursor) {
      const ok = document.createElement("span");
      ok.className = "live-ok";
      ok.textContent = " ✓ complete";
      liveCursor.parentElement.querySelector(".live-mhead").appendChild(ok);
      liveCursor.classList.remove("live-pre");
    }
    liveCursor = null;
  } else if (type === "failure") {
    const m = d.member || {};
    const blk = document.createElement("div");
    blk.className = "err-box";
    blk.textContent = (m.name || "?") + " failed during " + (d.stage || "") + ": " + (d.error || "");
    log.appendChild(blk);
    liveCursor = null;
    scrollLive();
  } else if (type === "status") {
    refreshFromServer(d.status);
  } else if (type === "deleted") {
    stopSSE();
  }
}

/* ------------------------------------------------------------------ */
/* downloads + delete                                                  */
/* ------------------------------------------------------------------ */
function download(fmt) {
  if (!SESSION) return;
  window.location.href = "/api/sessions/" + encodeURIComponent(SESSION.id) +
    "/download?format=" + encodeURIComponent(fmt);
}
async function deleteSession() {
  if (!SESSION) return;
  if (!window.confirm("Delete this session and its history?")) return;
  try { await api("/api/sessions/" + encodeURIComponent(SESSION.id), { method: "DELETE" }); }
  catch (err) { toast(err.message); return; }
  stopSSE();
  showHome();
  toast("Session deleted.");
}

/* ------------------------------------------------------------------ */
/* init                                                                */
/* ------------------------------------------------------------------ */
function initializeApp() {
  bindDrop();
  loadHealth();
  refreshHistory();

  $("form").addEventListener("submit", submitSession);
  $("btn-new").addEventListener("click", showHome);
  $("btn-delete").addEventListener("click", deleteSession);
  $("btn-report-dl").addEventListener("click", () => {
    const menu = $("dl-menu");
    menu.hidden = !menu.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!$("dl-menu").hidden && !e.target.closest(".sess-actions")) $("dl-menu").hidden = true;
  });
  $("dl-menu").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-fmt]");
    if (b) { download(b.dataset.fmt); $("dl-menu").hidden = true; }
  });
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (b) setTab(b.dataset.tab);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initializeApp);
} else {
  initializeApp();
}
"""
_INDEX_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "<title>AI Council — web interface</title>\n"
    "<style>\n" + _APP_CSS + _APP_CSS_2 + _APP_CSS_3 + _APP_CSS_4 + "\n</style>\n"
    "</head>\n"
    "<body>\n" + _APP_HTML + "\n<script>\n" + _APP_JS + "\n</script>\n"
    "</body>\n</html>\n"
)
