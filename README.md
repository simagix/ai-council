# AI Council

**Structured LLM Deliberation for Better Decisions — v0.2.0**

Stop asking one AI. Ask three, and let them debate.

`ai-council` convenes three different local Ollama models to independently answer a question, then critique each other's reasoning, and finally synthesize a consensus report. The result: more nuanced answers, preserved disagreements, and transparency into how the conclusion was reached.

**Core question:** Does structured disagreement between different LLMs produce a more reliable conclusion than simply asking one LLM?

**Answer:** Often yes — and you can see exactly why.

### Get Started (1 minute)

```bash
python ai_council.py --server
```

That's it. Opens **http://127.0.0.1:8080** in your browser. Type or paste a question, optionally drag & drop a context file, and watch three models deliberate in real time with live streaming responses. Everything stays on your machine.

---

### Why This Matters

- **Single LLM responses are confident but often wrong.** A model will commit to an answer even when evidence is mixed or assumptions are shaky.
- **Structured disagreement surfaces nuance.** When three different models debate, weak arguments break apart and assumptions get questioned.
- **Consensus without voting.** This isn't democracy (2 YES vs 1 NO = YES wins). The minority position is preserved if significant. Sometimes dissent is the most valuable insight.
- **100% local, zero cloud.** Runs on your Mac or Linux machine with Ollama. No API keys. No data leaves your computer. No agent frameworks or vendor lock-in — just Python standard library.

---

---

## The Council Protocol

![AI Council workflow diagram: a user question is sent to three models for independent opinions (Round 1), the models then critique each other's arguments (Round 2), and a moderator synthesizes a consensus report (Round 3)](council-workflow.jpeg)

*Three independent opinions → structured discussion → moderator's consensus report*

```
User question
      ↓
Round 1 — Three independent opinions (models see only the question)
      ↓
Round 2 — Council discussion (each model sees and critiques the others' arguments)
      ↓
Round 3 — Moderator synthesizes the final report
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

### Key Principle: Preserve Minority Arguments

The council deliberately does **not** implement majority voting. `2 YES vs 1 NO → YES wins` is lazy — and often wrong. The moderator is explicitly instructed:

> Do not manufacture consensus. If significant disagreement remains, say so explicitly.

This is why AI Council is different from other multi-agent approaches: dissent is a feature, not a bug.

---

## Council Members (Configurable)

| Model             | Role                | Temperament |
| ----------------- | ------------------- | ----------- |
| `qwen3.5:9b`      | Analyst             | Technically rigorous: facts vs. assumptions, trade-offs, strengths of competing views |
| `gemma4:latest`   | Independent Thinker | Challenges orthodoxy, considers overlooked options and unconventional angles |
| `llama3.2:latest` | Skeptic             | Questions assumptions, hunts flaws in logic, surfaces failure cases and hidden risks |

The models intentionally differ in size and capability. No model is auto-promoted for being larger — all voices are equal. Change any model via environment variables (see [Configuration](#configuration)).

**Live streaming:** Responses stream in real time, both in the terminal and web interface — you see the `[Model — Role]` label as soon as the first token arrives. The first response from each model includes load time (Ollama pulls the model to memory), and reasoning models spend time on hidden thinking before their visible answer.

**Sequential execution** (not parallel) minimizes memory pressure and keeps transcripts clean.

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
```

No installation is required either — run the launcher directly:

```bash
python ai_council.py "Should I buy 256GB or 512GB for my Mac mini?"
python ai_council.py --version        # -> ai-council v0.2.0
```

## Usage

Ask a one-off question:

```bash
python ai_council.py "Should I buy 256GB or 512GB for my Mac mini?"
```

Or run interactively (multi-line questions supported — end with `Ctrl-D`):

```bash
python ai_council.py
```

```
AI Council

Enter your question:
>
```

### Question files (`use_cases/`)

Real questions usually carry a lot of context. Put the question, its
background, and your constraints in a text/Markdown file — the whole file
is sent verbatim to every council member:

```bash
python ai_council.py --file use_cases/mac-mini-storage.md
cat my-question.md | python ai_council.py --file -    # read from stdin
```

See [`use_cases/README.md`](use_cases/README.md) for the recommended file
shape and `use_cases/mac-mini-storage.md` for a worked example.

Every session is saved as a transcript. By default you get a
self-contained HTML page with a timestamped name (e.g.
`out/council-20260901-094709.html`), so repeated runs never overwrite each
other; `--md` saves readable Markdown instead (defaults to `out/` unless
you give a path), and `--html FILE` picks the name. The two flags can
be used together:

```bash
python ai_council.py "Should I buy 256GB or 512GB?"            # out/council-<timestamp>.html
python ai_council.py "Should I buy 256GB or 512GB?" --md council.md
python ai_council.py "Should I buy 256GB or 512GB?" --html council.html
python ai_council.py "Should I buy 256GB or 512GB?" --md council.md --html council.html
```

The HTML page is a single self-contained file — inline CSS only, no
scripts, no external assets — so it can be opened directly in any
browser, emailed, or dropped into a static site. Markdown from the
question file and model outputs is rendered to real HTML (headings,
lists, bold/italic/code, blockquotes), and everything is escaped, so
model output can never inject markup.

### Web Interface (Browser Mode)

**Simplest way to use AI Council:**

```bash
python ai_council.py --server
```

Opens **http://127.0.0.1:8080** automatically. Type or paste your question, optionally drag & drop a context file, and watch three models deliberate with live streaming. When finished, download the **Final Report** (consensus + disagreements + strongest arguments + minority opinion) as Markdown or self-contained HTML.

**Features:**
- **Live streaming** — watch responses arrive token-by-token
- **Session history** — every council deliberation is saved (survives server restarts)
- **Download options** — Council report, full transcript (Markdown or HTML)
- **100% local** — server binds to `127.0.0.1` only, no external network traffic

**Run in the background:**

```bash
python ai_council.py --server --daemonize        # start in background
python ai_council.py --server --stop             # stop the daemon
```

**All server options:**

| Flag | Meaning | Default |
| --- | --- | --- |
| `--host` | Interface to bind | `127.0.0.1` |
| `--port` | Port to listen on | `8080` |
| `--data-dir` | Where session history is stored | `$AI_COUNCIL_DATA_DIR` or `~/.ai-council` |
| `--daemonize` | Fork into the background | off |
| `--stop` | Stop the background daemon | — |
| `--pid-file` | Pid file for daemon mode | `<data-dir>/server.pid` |
| `--log-file` | Log file for daemon mode | `<data-dir>/server.log` |
| `--no-open` | Don't auto-open browser | off |

**API for scripting:**

The server exposes a JSON API: `GET /api/health`, `GET /api/history`, `POST /api/sessions`, `GET/DELETE /api/sessions/<id>`, `GET /api/sessions/<id>/events` (Server-Sent Events), `GET /api/sessions/<id>/download?format=report|md|html`.

---

## Configuration

Model names and settings live in one place (`config.py`) so they can be changed without touching the core logic. Every value can be overridden with environment variables:

| Variable | Meaning | Default |
| --- | --- | --- |
| `AI_COUNCIL_OLLAMA_HOST` | Ollama API base URL | `http://localhost:11434` |
| `AI_COUNCIL_TIMEOUT` | Per-request timeout in seconds | `600` |
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
python ai_council.py "question"
```

---

## Architecture

Deliberately small and modular — the orchestration is built by hand so the council protocol stays transparent and easy to experiment with.

```
ai-council/
├── README.md
├── VERSION               # single source of truth: "0.2.0"
├── ai_council.py         # launcher / main entry: python ai_council.py ... / --version
├── cli.py                # argument parsing, interactive input, transcript display, --md/--html rendering, --server dispatch
├── council.py            # orchestrates Round 1 → Round 2 → Moderator → report
├── ollama.py             # local Ollama API client (stdlib urllib); errors and timeouts
├── prompts.py            # all system prompts and discussion prompts in one place
├── models.py             # CouncilMember(name, model, role, role_key) representations
├── config.py             # model names and basic settings; env-var overrides
├── server.py             # web interface: JSON API + SSE + embedded single-page app; `--server` flag
├── store.py              # persistent session store (one JSON file per session) with event log
└── tests/
```

**Zero external dependencies** — Python standard library only. The local Ollama API is called directly over HTTP. No LangChain, CrewAI, AutoGen, or any vendor-specific agent framework. This keeps the protocol transparent and easy to understand (or fork and modify).

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

## Non-Goals

- Authentication
- Cloud model APIs
- Persistent or vector databases
- RAG
- Autonomous agents, tool calling
- Parallel model execution
- Automatic model selection
- Agent frameworks

(The web UI shipped in v0.2.0 — see [Web interface (server mode)](#web-interface-server-mode).)

## Roadmap (future possibilities)

Deliberately not implemented yet, but the architecture keeps the door open:

- Cloud participants: ChatGPT, Gemini, Claude
- More Ollama models, different council roles
- More debate rounds; user participation mid-discussion
- Consensus scoring
- Evidence/research phase, fact-checker and devil's advocate participants

## Development Philosophy

This is an experiment in multi-model deliberation. The implementation is kept intentionally small so the council protocol can be changed and re-run easily. The first milestone — *question → 3 independent opinions → discussion → moderator → consensus + disagreement + minority opinion* — is the whole of v0.1.
