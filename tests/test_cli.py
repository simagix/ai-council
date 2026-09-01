"""Tests for the CLI: version, transcript rendering, --save, error paths."""

import contextlib
import io
import os
import tempfile
import unittest

from cli import main, run_session, __version__

from helpers import FakeClient, make_config

QUESTION = "Should I buy 256GB or 512GB?"


class TestVersion(unittest.TestCase):
    def test_version_flag(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), "ai-council v0.1.0")

    def test_version_matches_version_file(self):
        root = os.path.join(os.path.dirname(__file__), os.pardir)
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as fh:
            self.assertEqual(__version__, fh.read().strip())


class TestRunSession(unittest.TestCase):
    def test_transcript_contains_all_sections(self):
        out = io.StringIO()
        code = run_session(QUESTION, make_config(), FakeClient(), out=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        for expected in (
            "AI COUNCIL",
            QUESTION,
            "ROUND 1 — INDEPENDENT OPINIONS",
            "ROUND 2 — COUNCIL DISCUSSION",
            "FINAL COUNCIL REPORT",
            "[Qwen 3.5 9B — Analyst]",
            "[Gemma 4 — Independent Thinker]",
            "[Llama 3.2 — Skeptic]",
            "[Qwen 3.5 9B — Moderator]",
        ):
            self.assertIn(expected, text)

    def test_member_failure_is_shown_in_transcript(self):
        out = io.StringIO()
        client = FakeClient(fail_models={"gemma4:latest"})
        code = run_session(QUESTION, make_config(), client, out=out)
        self.assertEqual(code, 0)
        self.assertIn("FAILED — Gemma 4 (Independent Thinker) during Round 1", out.getvalue())

    def test_missing_model_reports_install_hint(self):
        out = io.StringIO()
        client = FakeClient(models=["llama3.2:latest"])
        code = run_session(QUESTION, make_config(), client, out=out)
        self.assertEqual(code, 2)
        self.assertIn("Model not found: qwen3.5:9b", out.getvalue())
        self.assertIn("ollama pull qwen3.5:9b", out.getvalue())

    def test_connection_error_reports_friendly_message(self):
        out = io.StringIO()
        client = FakeClient(connection_error=True)
        code = run_session(QUESTION, make_config(), client, out=out)
        self.assertEqual(code, 2)
        self.assertIn("Cannot connect to Ollama.", out.getvalue())

    def test_moderator_failure_returns_exit_code_1(self):
        from ollama import OllamaError

        class ModeratorFails(FakeClient):
            def generate(self, model, prompt, system=None, timeout=None):
                if system and "Do not manufacture consensus" in system:
                    raise OllamaError("moderator unavailable")
                return super().generate(model, prompt, system, timeout)

        out = io.StringIO()
        code = run_session(QUESTION, make_config(), ModeratorFails(), out=out)
        self.assertEqual(code, 1)
        self.assertIn("No final report", out.getvalue())

    def test_save_writes_markdown_file(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "council.md")
            code = run_session(
                QUESTION, make_config(), FakeClient(), out=out, save_path=path
            )
            self.assertEqual(code, 0)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        self.assertIn("# AI Council Session", content)
        self.assertIn(f"**Question:** {QUESTION}", content)
        self.assertIn("ROUND 1 — INDEPENDENT OPINIONS", content)
        self.assertIn("Transcript saved to", out.getvalue())


if __name__ == "__main__":
    unittest.main()
