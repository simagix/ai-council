"""Tests for the Ollama client helpers."""

import unittest

from ollama import (
    normalize_model_name,
    strip_thinking,
    ThinkingStreamFilter,
)


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


class TestThinkingStreamFilter(unittest.TestCase):
    def feed_all(self, chunks):
        f = ThinkingStreamFilter()
        return "".join(f.feed(c) for c in chunks) + f.feed("")

    def test_plain_text_passes_through(self):
        self.assertEqual(self.feed_all(["Hello, ", "world!"]), "Hello, world!")

    def test_complete_think_block_is_hidden(self):
        out = self.feed_all(["<think>", "secret ", "thoughts", "</think>", "Answer"])
        self.assertEqual(out, "Answer")

    def test_tag_split_across_chunks(self):
        out = self.feed_all(["Hi <thi", "nk>hidden</th", "ink>visible"])
        self.assertEqual(out, "Hi visible")

    def test_open_tag_split_across_chunks(self):
        out = self.feed_all(["A <th", "ink>x</think>B"])
        self.assertEqual(out, "A B")

    def test_unterminated_think_block_suppressed(self):
        out = self.feed_all(["Answer 1<think>never closed"])
        self.assertEqual(out, "Answer 1")

    def test_nothing_leaks_from_inside_think(self):
        f = ThinkingStreamFilter()
        self.assertEqual(f.feed("<think>abc"), "")
        self.assertEqual(f.feed("</th"), "")
        self.assertEqual(f.feed("ink>ok"), "ok")


class TestNormalizeModelName(unittest.TestCase):
    def test_strips_latest_suffix(self):
        self.assertEqual(normalize_model_name("llama3.2:latest"), "llama3.2")

    def test_case_and_whitespace(self):
        self.assertEqual(normalize_model_name("  Llama3.2:latest "), "llama3.2")

    def test_explicit_tag_kept(self):
        self.assertEqual(normalize_model_name("qwen3.5:9b"), "qwen3.5:9b")


if __name__ == "__main__":
    unittest.main()
