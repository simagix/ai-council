"""Tests for council orchestration and the preflight check."""

import unittest

from council import preflight, run_council, CouncilError
from ollama import ModelNotFoundError, OllamaError, normalize_model_name

from helpers import FakeClient, make_config

QUESTION = "Should I buy 256GB or 512GB?"


class TestRunCouncil(unittest.TestCase):
    def test_full_session_produces_round1_round2_and_report(self):
        config = make_config()
        client = FakeClient()
        result = run_council(QUESTION, config, client=client)

        self.assertEqual(len(result.round1), 3)
        self.assertEqual(len(result.round2), 3)
        self.assertIsNotNone(result.report)
        self.assertEqual(result.report_member.role, "Moderator")
        # qwen3.5:9b (the Analyst) acts as moderator
        self.assertEqual(
            normalize_model_name(result.report_member.model), "qwen3.5:9b"
        )
        # 3 (round1) + 3 (round2) + 1 (moderator) generate calls
        self.assertEqual(len(client.calls), 7)

    def test_round1_models_never_see_each_other(self):
        config = make_config()
        client = FakeClient()
        run_council(QUESTION, config, client=client)
        first_three_prompts = [c[1] for c in client.calls[:3]]
        for prompt in first_three_prompts:
            self.assertEqual(prompt, QUESTION)
            self.assertNotIn("my answer", prompt)

    def test_round2_sees_the_other_members(self):
        config = make_config()
        client = FakeClient()
        run_council(QUESTION, config, client=client)
        round2_prompts = [c[1] for c in client.calls[3:6]]
        for prompt in round2_prompts:
            self.assertIn(QUESTION, prompt)
            # every member's round-1 answer appears in the discussion prompt
            self.assertEqual(prompt.count("my answer"), 3)
            self.assertIn("council discussion", prompt)

    def test_moderator_receives_question_round1_and_round2(self):
        config = make_config()
        client = FakeClient()
        run_council(QUESTION, config, client=client)
        model, prompt, system = client.calls[-1]
        self.assertIn(QUESTION, prompt)
        self.assertIn("Round 1 — Independent Opinions", prompt)
        self.assertIn("Round 2 — Council Discussion", prompt)
        self.assertIn("Do not manufacture consensus", system)

    def test_member_failure_is_recorded_not_hidden(self):
        config = make_config()
        client = FakeClient(fail_models={"gemma4:latest"})
        result = run_council(QUESTION, config, client=client)

        self.assertEqual(len(result.round1), 2)
        self.assertEqual(len(result.failures), 1)  # gemma failed Round 1
        self.assertEqual(result.failures[0].member.model, "gemma4:latest")
        self.assertEqual(result.failures[0].stage, "Round 1")
        # round 2 only includes survivors, and the council still concludes
        self.assertEqual(len(result.round2), 2)
        self.assertIsNotNone(result.report)

    def test_all_members_failing_aborts_the_council(self):
        config = make_config()
        client = FakeClient(fail_models={"qwen3.5:9b", "gemma4:latest", "llama3.2:latest"})
        with self.assertRaises(CouncilError):
            run_council(QUESTION, config, client=client)

    def test_moderator_failure_returns_result_without_report(self):
        config = make_config()

        class ModeratorFails(FakeClient):
            def generate(self, model, prompt, system=None, timeout=None):
                # the moderator prompt is the only one using MODERATOR_SYSTEM
                if system and "Do not manufacture consensus" in system:
                    raise OllamaError("moderator unavailable")
                return super().generate(model, prompt, system, timeout)

        result = run_council(QUESTION, config, client=ModeratorFails())
        self.assertIsNone(result.report)
        self.assertEqual(len(result.round1), 3)
        self.assertEqual(len(result.round2), 3)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].stage, "Moderator")


class TestPreflight(unittest.TestCase):
    def test_missing_model_raises_with_its_name(self):
        config = make_config()
        client = FakeClient(models=["llama3.2:latest"])  # qwen + gemma missing
        with self.assertRaises(ModelNotFoundError) as ctx:
            preflight(client, config)
        # reported in configuration order: the Analyst's model is first
        self.assertEqual(ctx.exception.model, "qwen3.5:9b")

    def test_all_models_present_passes(self):
        config = make_config()
        preflight(FakeClient(), config)  # should not raise

    def test_connection_error_propagates(self):
        config = make_config()
        with self.assertRaises(OllamaError):
            preflight(FakeClient(connection_error=True), config)

    def test_latest_tag_matches_bare_name(self):
        config = make_config()
        client = FakeClient(models=["qwen3.5:9b", "gemma4", "llama3.2:latest"])
        preflight(client, config)  # gemma4:latest configured, gemma4 installed


if __name__ == "__main__":
    unittest.main()
