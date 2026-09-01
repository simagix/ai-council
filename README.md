# AI Council

**A multi-model deliberation experiment — v0.1**

`ai-council` convenes three different local Ollama models to discuss a question, challenge each other's reasoning, and produce a consensus report. It is an experimental prototype built to answer one question:

> Does structured disagreement between different LLMs produce a more reliable conclusion than simply asking one LLM?

No web UI. No cloud services. No agent frameworks. Just three local models, a transparent council protocol, and a readable transcript.

---

## How It Works

```
User question
      ↓
Round 1 — Three independent opinions (models see only the question)
      ↓
Round 2 — Council discussion (each model sees and critiques the others' arguments)
      ↓
Moderator — One model synthesizes the final report
      ↓
Consensus + Disagreements + Strongest Arguments + Minority Opinion
```

### Round 1 — Independent Opinions

The exact same question is sent to all three models **independently**. No model sees another model's response. Each model answers from its role-specific perspective:

1. Recommendation / position
2. Reasoning
3. Important assumptions
4. Confidence level
5. What information could change its conclusion

### Round 2 — Council Discussion

Each model is then shown the other participants' Round 1 responses and asked to critique the discussion: points of agreement and disagreement, factual disputes, logical weaknesses, assumptions worth examining, and whether its own position was changed, modified, or maintained.

### Round 3 — Moderator / Consensus Report

One model (the Analyst) acts as moderator after completing its participant role. It receives the original question plus all Round 1 and Round 2 responses and produces the final report:

- **Consensus** — what all or most participants agree on
- **Disagreement** — important unresolved differences
- **Strongest Arguments** — which arguments appear strongest and why
- **Minority Opinion** — a meaningful minority position, preserved if one exists
- **Final Recommendation** — the moderator's best overall conclusion
- **Confidence** — Low / Medium / High, with an explanation

### The Core Principle: Evaluate Reasoning, Not Votes

The council deliberately does **not** implement majority voting. `2 YES vs 1 NO → YES wins` is not sufficient — a minority argument may be correct. The moderator is explicitly instructed:

> Do not manufacture consensus. If significant disagreement remains, say so explicitly.

---

## Council Members (defaults)

| Model             | Role                | Temperament |
| ----------------- | ------------------- | ----------- |
| `qwen3.5:9b`      | Analyst             | Technically/logically strongest answer: facts vs. assumptions, trade-offs |
| `gemma4:latest`   | Independent Thinker | Avoids conventional wisdom, considers overlooked options |
| `llama3.2:latest` | Skeptic             | Hunts hidden assumptions, challenges unsupported claims, considers failure cases |

The models intentionally differ in size and capability. The largest model is not assumed to be automatically correct. All model names are configurable — see [Configuration](#configuration).

Models run **sequentially, not concurrently**, to minimize memory pressure on a 32 GB Apple M1.

---

## Requirements

- macOS (developed on Apple M1 / 32 GB RAM, but any machine with enough RAM works)
- Python 3.10+
- [Ollama](https://ollama.com) installed and running locally

Pull the required models:

```bash
ollama pull qwen3.5:9b
ollama pull gemma4:latest
ollama pull llama3.2:latest
```

## Installation

```bash
git clone https://github.com/simagix/ai-council.git
cd ai-council
pip install -e .
```

## Usage

Ask a one-off question:

```bash
ai-council "Should I buy 256GB or 512GB for my Mac mini?"
```

No installation is required either — run the launcher directly:

```bash
python ai_council.py "Should I buy 256GB or 512GB for my Mac mini?"
python ai_council.py --version        # -> ai-council v0.1.0
```

Or run interactively (multi-line questions supported — end with `Ctrl-D`):

```bash
ai-council
```

```
AI Council

Enter your question:
>
```

Save the full session as a Markdown transcript:

```bash
ai-council "Should I buy 256GB or 512GB?" --save council.md
```

---

## Configuration

Model names and settings live in one place (`src` defaults in `ai_council/config.py`) so they can be changed without touching the core logic. Every value can be overridden with environment variables:

| Variable | Meaning | Default |
| --- | --- | --- |
| `AI_COUNCIL_OLLAMA_HOST` | Ollama API base URL | `http://localhost:11434` |
| `AI_COUNCIL_TIMEOUT` | Per-request timeout in seconds | `300` |
| `AI_COUNCIL_MODEL_ANALYST` | Model for the Analyst role | `qwen3.5:9b` |
| `AI_COUNCIL_MODEL_THINKER` | Model for the Independent Thinker role | `gemma4:latest` |
| `AI_COUNCIL_MODEL_SKEPTIC` | Model for the Skeptic role | `llama3.2:latest` |
| `AI_COUNCIL_MODERATOR` | Model acting as Round 3 moderator | `qwen3.5:9b` |

For example, to run a quick, low-memory council using only Llama 3.2:

```bash
AI_COUNCIL_MODEL_ANALYST=llama3.2:latest \
AI_COUNCIL_MODEL_THINKER=llama3.2:latest \
AI_COUNCIL_MODEL_SKEPTIC=llama3.2:latest \
AI_COUNCIL_MODERATOR=llama3.2:latest \
ai-council "question"
```

---

## Architecture

Deliberately small and modular — the orchestration is built by hand so the council protocol stays transparent and easy to experiment with.

```
ai-council/
├── README.md
├── VERSION               # single source of truth: "0.1.0"
├── pyproject.toml
├── ai_council.py         # launcher: python ai_council.py ... / --version
├── ai_council/
│   ├── __init__.py
│   ├── cli.py            # argument parsing, interactive input, transcript display, --save
│   ├── council.py        # orchestrates Round 1 → Round 2 → Moderator → report
│   ├── ollama.py         # local Ollama API client (stdlib urllib); errors and timeouts
│   ├── prompts.py        # all system prompts and discussion prompts in one place
│   ├── models.py         # CouncilMember(name, model, role, role_key) representations
│   └── config.py         # model names and basic settings; env-var overrides
└── tests/
```

**Dependencies:** zero at runtime — Python's standard library only; the local Ollama API is accessed directly over HTTP. No LangChain, CrewAI, AutoGen, or any agent framework.

## Development

Run the test suite (uses a fake Ollama client — no Ollama needed):

```bash
python3 -m unittest discover -s tests
```

---

## Error Handling

Failures are reported clearly — the council never silently substitutes a model or pretends a member participated.

- **Ollama not running:**
  ```
  Cannot connect to Ollama.

  Make sure Ollama is running and try again.
  ```
- **Model not installed:**
  ```
  Model not found: qwen3.5:9b

  Install it with:

  ollama pull qwen3.5:9b
  ```
- **Member failure mid-session:** the failure is called out in the transcript rather than treated as a contribution.

---

## Non-Goals (v0.1)

- Web UI
- Authentication
- Cloud model APIs
- Persistent or vector databases
- RAG
- Autonomous agents, tool calling
- Parallel model execution
- Automatic model selection
- Agent frameworks

## Roadmap (future possibilities)

Deliberately not implemented yet, but the architecture keeps the door open:

- Cloud participants: ChatGPT, Gemini, Claude
- More Ollama models, different council roles
- More debate rounds; user participation mid-discussion
- Web interface; session history; consensus scoring
- Evidence/research phase, fact-checker and devil's advocate participants

## Development Philosophy

This is an experiment in multi-model deliberation. The implementation is kept intentionally small so the council protocol can be changed and re-run easily. The first milestone — *question → 3 independent opinions → discussion → moderator → consensus + disagreement + minority opinion* — is the whole of v0.1.
