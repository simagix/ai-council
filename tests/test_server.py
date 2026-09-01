"""End-to-end tests for the ai-council web server.

The tests start a real ``http.server`` on an ephemeral port (``port 0``)
and talk to it over plain HTTP, exactly like a browser would.  Instead of
Ollama they inject the test :class:`helpers.FakeClient`, so the full
council protocol runs end to end (Round 1 -> Round 2 -> Moderator ->
report) inside the server's worker thread.
"""

import http.client
import json
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from helpers import FakeClient, make_config
from server import CouncilServer
from store import SessionStore

QUESTION = "Should I buy 256GB or 512GB?"


def _wait_until(predicate, timeout=20.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SessionStore(tmp.name)
        self.server = CouncilServer(
            "127.0.0.1",
            0,
            self.store,
            config_factory=make_config,
            client_factory=lambda config: FakeClient(),
        )
        self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.port}"

    # -- HTTP helpers ---------------------------------------------------------

    def get_json(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body) if body.strip() else {}

    def raw_get(self, path):
        """GET returning (status, headers, body bytes) without JSON parsing."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def post_json(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body) if body.strip() else {}

    def delete_json(self, path):
        req = urllib.request.Request(self.base + path, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body) if body.strip() else {}

    def wait_for_status(self, session_id, statuses, timeout=30.0):
        def reached():
            _, snap = self.get_json(f"/api/sessions/{session_id}")
            return snap["status"] in statuses

        self.assertTrue(
            _wait_until(reached, timeout=timeout),
            f"session {session_id} did not reach {statuses} in time",
        )
        return self.get_json(f"/api/sessions/{session_id}")[1]

class BasicEndpointTests(ServerTestBase):
    """Health, index page, and 404 behaviour."""

    def test_health_reports_ok_and_ollama_up(self):
        status, payload = self.get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ollama"])
        self.assertIn("version", payload)

    def test_index_serves_html_page(self):
        status, headers, body = self.raw_get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        html = body.decode("utf-8")
        self.assertIn("AI Council", html)

    def test_unknown_path_returns_404_json(self):
        status, payload = self.get_json("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_unknown_session_returns_404(self):
        status, payload = self.get_json("/api/sessions/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "session not found")


class SessionApiTests(ServerTestBase):
    """Session creation, validation, history, and deletion."""

    def test_create_requires_question_or_file(self):
        status, payload = self.post_json("/api/sessions", {})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

        status, payload = self.post_json("/api/sessions", {"question": "   "})
        self.assertEqual(status, 400)

    def test_create_accepts_file_only_upload(self):
        status, snap = self.post_json(
            "/api/sessions",
            {"question": "", "file": {"name": "notes.txt", "content": "hello world"}},
        )
        self.assertEqual(status, 201)
        self.assertIn("Context document — notes.txt", snap["question"])
        self.assertIn("hello world", snap["question"])

    def test_create_rejects_oversized_file(self):
        status, payload = self.post_json(
            "/api/sessions",
            {"file": {"name": "big.txt", "content": "x" * 1_000_001}},
        )
        self.assertEqual(status, 400)
        self.assertIn("too large", payload["error"])

    def test_delete_session_returns_ok_then_404(self):
        _, snap = self.post_json("/api/sessions", {"question": QUESTION})
        session_id = snap["id"]
        status, payload = self.delete_json(f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        status, _ = self.get_json(f"/api/sessions/{session_id}")
        self.assertEqual(status, 404)

        status, payload = self.delete_json(f"/api/sessions/{session_id}")
        self.assertEqual(status, 404)


class CouncilFlowTests(ServerTestBase):
    """The full Round 1 -> Round 2 -> Moderator pipeline over HTTP."""

    def create_session(self, question=QUESTION):
        status, snap = self.post_json("/api/sessions", {"question": question})
        self.assertEqual(status, 201)
        self.assertEqual(snap["status"], "running")
        return snap

    def test_full_council_completes_with_report(self):
        snap = self.create_session()
        session_id = snap["id"]
        self.assertTrue(snap["members"], "session should record its members")

        snap = self.wait_for_status(session_id, {"complete"})
        self.assertIsNone(snap["error"])
        self.assertEqual(snap["phase"], 3)
        self.assertEqual(len(snap["round1"]), len(snap["members"]))
        self.assertEqual(len(snap["round2"]), len(snap["members"]))
        self.assertTrue(snap["report"], "moderator report should be present")
        self.assertTrue(snap["report_html"])
        self.assertIsNotNone(snap["report_member"])
        self.assertTrue(snap["completed_at"])

    def test_history_lists_completed_session(self):
        snap = self.create_session()
        self.wait_for_status(snap["id"], {"complete"})

        status, payload = self.get_json("/api/history")
        self.assertEqual(status, 200)
        sessions = payload["sessions"]
        self.assertTrue(sessions)
        first = sessions[0]
        self.assertEqual(first["id"], snap["id"])
        self.assertEqual(first["status"], "complete")
        self.assertIn(QUESTION[:40], first["question"])
        self.assertIn("created_at", first)

    def test_download_report_markdown(self):
        _, snap = self.post_json("/api/sessions", {"question": QUESTION})
        session_id = snap["id"]
        self.wait_for_status(session_id, {"complete"})

        status, headers, body = self.raw_get(
            f"/api/sessions/{session_id}/download?format=report"
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/markdown"))
        self.assertIn("attachment", headers["Content-Disposition"])
        text = body.decode("utf-8")
        self.assertIn("# Council Final Report", text)
        self.assertIn(QUESTION, text)
        self.assertIn("[", text)  # model answers / report content

    def test_download_full_transcript_markdown_and_html(self):
        _, snap = self.post_json("/api/sessions", {"question": QUESTION})
        session_id = snap["id"]
        self.wait_for_status(session_id, {"complete"})

        status, headers, body = self.raw_get(
            f"/api/sessions/{session_id}/download?format=md"
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/markdown"))
        text = body.decode("utf-8")
        self.assertIn(QUESTION, text)
        self.assertIn("[", text)

        status, headers, body = self.raw_get(
            f"/api/sessions/{session_id}/download?format=html"
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("<html", body.decode("utf-8").lower())

    def test_download_rejects_unknown_format(self):
        _, snap = self.post_json("/api/sessions", {"question": QUESTION})
        session_id = snap["id"]
        status, payload = self.get_json(
            f"/api/sessions/{session_id}/download?format=pdf"
        )
        self.assertEqual(status, 400)

    def test_sse_streams_live_events_until_status(self):
        _, snap = self.post_json("/api/sessions", {"question": QUESTION})
        session_id = snap["id"]

        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=15
        )
        self.addCleanup(conn.close)
        conn.request("GET", f"/api/sessions/{session_id}/events")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(
            resp.getheader("Content-Type").startswith("text/event-stream")
        )

        seen = set()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8").strip()
            if text.startswith("event:"):
                seen.add(text.split(":", 1)[1].strip())
            if "status" in seen:
                break

        # The worker streams tokens and pushes the whole event lifecycle.
        self.assertIn("start", seen)
        self.assertIn("status", seen)
        _, done = self.get_json(f"/api/sessions/{session_id}")
        self.assertEqual(done["status"], "complete")


class FailurePathTests(ServerTestBase):
    """Preflight and council failures must surface as terminal statuses."""

    def setUp(self):
        super().setUp()
        # Replace the default happy-path client factory.
        self.server._client_factory = lambda config: FakeClient(
            connection_error=self.client_connection_error,
            fail_models=self.fail_models,
        )
        self.client_connection_error = False
        self.fail_models = frozenset()

    def test_ollama_unreachable_aborts_session(self):
        self.client_connection_error = True
        _, snap = self.post_json("/api/sessions", {"question": QUESTION})
        snap = self.wait_for_status(snap["id"], {"aborted"})
        self.assertEqual(snap["status"], "aborted")
        self.assertTrue(snap["error"])
        self.assertIsNone(snap["report"])

    def test_all_members_failing_ends_as_failed(self):
        config = make_config()
        self.fail_models = frozenset(m.model for m in config.members)
        _, snap = self.post_json("/api/sessions", {"question": QUESTION})
        snap = self.wait_for_status(snap["id"], {"failed"})
        self.assertEqual(snap["status"], "failed")
        self.assertTrue(snap["error"])


class SessionStoreTests(unittest.TestCase):
    """Persistence: history survives restarts; running sessions are marked."""

    def test_sessions_persist_across_store_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            session = store.create("Why?")
            session.status = "complete"
            store._save_locked(session)

            reopened = SessionStore(tmp)
            meta = reopened.list()
            self.assertEqual(len(meta), 1)
            self.assertEqual(meta[0]["id"], session.id)
            self.assertEqual(meta[0]["status"], "complete")

            loaded = reopened.get(session.id)
            self.assertEqual(loaded.question, "Why?")

    def test_interrupted_sessions_are_marked_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            session = store.create("Still running?")  # status == running

            reopened = SessionStore(tmp)
            loaded = reopened.get(session.id)
            self.assertEqual(loaded.status, "interrupted")

    def test_delete_removes_file_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            session = store.create("Delete me")
            path = store.sessions_dir / f"{session.id}.json"
            self.assertTrue(path.exists())

            self.assertTrue(store.delete(session.id))
            self.assertFalse(path.exists())
            self.assertFalse(store.delete(session.id))
