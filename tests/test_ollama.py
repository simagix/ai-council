"""Tests for the Ollama client helpers."""

import unittest

from ai_council.ollama import normalize_model_name, strip_thinking


class TestStripThinking(unittest.TestCase):
    def test_removes_complete_think_block(self):
        text = "<think>secret reasoning</think>The answer is 512GB."
        self.assertEqual(strip_thinking(text), "The answer is 512GB.")

    def test_removes_think_block_with_surrounding_whitespace(self):
        text = "<think>\nreasoning\n</think>\n\nAnswer here.\n"
        self.assertEqual(strip_thinking(text), "Answer here.")

    def test_drops_unterminated_think_block(self):
        text = "<think>the model never closed its thoughts"
        self.assertEqual(strip_thinking(text), "")

    def test_plain_text_untouched(self):
        self.assertEqual(strip_thinking("Just an answer."), "Just an answer.")


class TestNormalizeModelName(unittest.TestCase):
    def test_strips_latest_suffix(self):
        self.assertEqual(normalize_model_name("llama3.2:latest"), "llama3.2")

    def test_case_and_whitespace(self):
        self.assertEqual(normalize_model_name("  Llama3.2:latest "), "llama3.2")

    def test_explicit_tag_kept(self):
        self.assertEqual(normalize_model_name("qwen3.5:9b"), "qwen3.5:9b")


if __name__ == "__main__":
    unittest.main()
