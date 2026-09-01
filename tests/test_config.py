"""Tests for configuration defaults and env overrides."""

import unittest

from config import load_config


class TestLoadConfig(unittest.TestCase):
    def test_defaults(self):
        config = load_config(environ={})
        self.assertEqual(config.ollama_host, "http://localhost:11434")
        # must be generous: reasoning models think for minutes
        self.assertEqual(config.timeout_seconds, 600)
        self.assertEqual(config.moderator_model, "qwen3.5:9b")
        self.assertEqual(len(config.members), 3)

    def test_env_overrides(self):
        config = load_config(
            environ={
                "AI_COUNCIL_OLLAMA_HOST": "http://host:1234",
                "AI_COUNCIL_TIMEOUT": "1200",
                "AI_COUNCIL_MODEL_ANALYST": "llama3.2:latest",
                "AI_COUNCIL_MODERATOR": "llama3.2:latest",
            }
        )
        self.assertEqual(config.ollama_host, "http://host:1234")
        self.assertEqual(config.timeout_seconds, 1200)
        self.assertEqual(config.members[0].model, "llama3.2:latest")
        self.assertEqual(config.moderator_model, "llama3.2:latest")
        # untouched roles keep their defaults
        self.assertEqual(config.members[1].model, "gemma4:latest")


if __name__ == "__main__":
    unittest.main()
