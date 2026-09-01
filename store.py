"""Persistent session store for the ai-council web server.

Every council deliberation becomes one JSON file under
``<data-dir>/sessions/<id>.json`` so history survives server restarts.
Sessions are held in memory while the server is up and every structural
change (stage, request, response, failure, status) is written to disk
atomically (tmp file + rename).

Two things ride on top of the persisted state:

* a small append-only **event log** per session that powers the live
  (Server-Sent Events) view.  Token-level events are live-only and are
  *not* persisted — a reconnecting client receives a fresh snapshot.
* a :class:`threading.Condition` the SSE readers wait on so live events
  are pushed instead of polled.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import random
import string
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"       # the council ran but produced no report
STATUS_ABORTED = "aborted"     # preflight failed (Ollama unreachable, model missing)
STATUS_INTERRUPTED = "interrupted"  # server stopped while the session was running


def utc_iso() -> str:
    """UTC timestamp in ISO-8601, milliseconds precision."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return f"{stamp}-{suffix}"


def member_to_dict(member: Any) -> Dict[str, str]:
    """Convert a :class:`models.CouncilMember` to a storable dict."""
    return {
        "name": member.name,
        "model": member.model,
        "role": member.role,
        "role_key": member.role_key,
    }


class Session:
    """One council deliberation, its state, and its live event log."""

    def __init__(
        self,
        session_id: str,
        question: str,
        members: Optional[List[Dict[str, str]]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        self.id = session_id
        self.question = question
        self.members: List[Dict[str, str]] = members or []
        self.created_at = created_at or utc_iso()
        self.completed_at: Optional[str] = None

        self.status: str = STATUS_RUNNING
        self.error: Optional[str] = None
        self.stage: Optional[str] = None   # human title of the current round
        self.phase: int = 0               # 0 question, 1 Round 1, 2 Round 2, 3 moderator
        self.current_member: Optional[Dict[str, str]] = None

        self.round1: List[Dict[str, Any]] = []
        self.round2: List[Dict[str, Any]] = []
        self.report: Optional[str] = None
        self.report_html: Optional[str] = None
        self.report_member: Optional[Dict[str, str]] = None
        self.failures: List[Dict[str, Any]] = []
        self.notes: List[str] = []

        self.events: List[Dict[str, Any]] = []
        self._seq = 0
        self.cond = threading.Condition()

    # -- live event log -----------------------------------------------------

    def publish(self, event_type: str, **data: Any) -> Dict[str, Any]:
        """Append one event and wake up any SSE readers."""
        event = {"seq": self._seq, "type": event_type}
        event.update(data)
        self._seq += 1
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()
        return event

    # -- persistence ---------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "members": self.members,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "error": self.error,
            "stage": self.stage,
            "phase": self.phase,
            "current_member": self.current_member,
            "round1": self.round1,
            "round2": self.round2,
            "report": self.report,
            "report_html": self.report_html,
            "report_member": self.report_member,
            "failures": self.failures,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        session = cls(
            session_id=data["id"],
            question=data["question"],
            members=data.get("members") or [],
            created_at=data["created_at"],
        )
        session.completed_at = data.get("completed_at")
        session.status = data.get("status", STATUS_RUNNING)
        session.error = data.get("error")
        session.stage = data.get("stage")
        session.phase = int(data.get("phase", 0))
        session.current_member = data.get("current_member")
        session.round1 = data.get("round1") or []
        session.round2 = data.get("round2") or []
        session.report = data.get("report")
        session.report_html = data.get("report_html")
        session.report_member = data.get("report_member")
        session.failures = data.get("failures") or []
        session.notes = data.get("notes") or []
        return session
class SessionStore:
    """In-memory sessions with atomic JSON persistence on every change."""

    def __init__(self, base_dir: str) -> None:
        self.root = Path(base_dir)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions: Dict[str, Session] = {}
        self._load()

    # -- public API ----------------------------------------------------------

    def create(
        self, question: str, members: Optional[List[Dict[str, str]]] = None
    ) -> Session:
        with self.lock:
            session = Session(_new_session_id(), question, members=members)
            session.publish(
                "start",
                question=question,
                status=session.status,
                created_at=session.created_at,
            )
            self.sessions[session.id] = session
            self._save_locked(session)
            return session

    def get(self, session_id: str) -> Optional[Session]:
        with self.lock:
            return self.sessions.get(session_id)

    def list(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.lock:
            ordered = sorted(
                self.sessions.values(), key=lambda s: s.created_at, reverse=True
            )
            return [self._meta(s) for s in ordered[:limit]]

    def delete(self, session_id: str) -> bool:
        with self.lock:
            session = self.sessions.pop(session_id, None)
            if session is None:
                return False
            try:
                self._path(session_id).unlink(missing_ok=True)
            except OSError:
                pass
            session.publish("deleted", id=session_id)
            return True

    # -- state transitions (each persists + emits an event) -------------------

    def set_stage(self, session: Session, title: str, phase: int) -> None:
        with self.lock:
            session.stage = title
            session.phase = phase
            session.current_member = None
            self._save_locked(session)
            session.publish("stage", title=title, phase=phase)

    def set_current_member(
        self, session: Session, member: Dict[str, str], stage_label: str
    ) -> None:
        with self.lock:
            session.current_member = member
            self._save_locked(session)
            session.publish("request", member=member, stage=stage_label)

    def add_response(
        self,
        session: Session,
        round_key: str,
        entry: Dict[str, Any],
        stage_label: str,
    ) -> None:
        with self.lock:
            getattr(session, round_key).append(entry)
            session.current_member = None
            self._save_locked(session)
            session.publish("response", member=entry["member"], stage=stage_label)

    def add_failure(
        self, session: Session, entry: Dict[str, Any], stage_label: str
    ) -> None:
        with self.lock:
            session.failures.append(entry)
            session.current_member = None
            self._save_locked(session)
            session.publish("failure", member=entry["member"], stage=stage_label)

    def set_report(
        self,
        session: Session,
        text: str,
        html: str,
        member: Optional[Dict[str, str]],
    ) -> None:
        with self.lock:
            session.report = text
            session.report_html = html
            session.report_member = member
            session.phase = 3
            self._save_locked(session)
            session.publish("report", member=member)

    def add_notes(self, session: Session, notes: List[str]) -> None:
        with self.lock:
            session.notes.extend(notes)
            self._save_locked(session)
            if notes:
                session.publish("note", notes=list(notes))

    def set_status(
        self, session: Session, status: str, error: Optional[str] = None
    ) -> None:
        with self.lock:
            session.status = status
            if error is not None:
                session.error = error
            if status in (STATUS_COMPLETE, STATUS_FAILED, STATUS_ABORTED):
                session.completed_at = session.completed_at or utc_iso()
            session.current_member = None
            self._save_locked(session)
            session.publish("status", status=status, error=session.error)

    def publish_token(self, session: Session, text: str) -> None:
        """Live-only token event; deliberately not persisted."""
        session.publish("token", text=text)

    # -- internals -----------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def _save_locked(self, session: Session) -> None:
        path = self._path(session.id)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(session.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            # Persistence is best-effort: the in-memory session is authoritative.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _load(self) -> None:
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            session = Session.from_dict(data)
            if session.status == STATUS_RUNNING:
                # The previous server process died mid-session.
                session.status = STATUS_INTERRUPTED
            self.sessions[session.id] = session

    @staticmethod
    def _meta(session: Session) -> Dict[str, Any]:
        preview = " ".join(session.question.split())
        if len(preview) > 160:
            preview = preview[:157] + "..."
        return {
            "id": session.id,
            "question": preview,
            "created_at": session.created_at,
            "completed_at": session.completed_at,
            "status": session.status,
            "phase": session.phase,
            "error": session.error,
        }