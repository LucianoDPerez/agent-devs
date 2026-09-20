# AgentDevs

> Your AI coding partner, running **100% locally** on your machine.
> No cloud. No API keys. Your code never leaves your computer.

AgentDevs is a development agent that **analyzes, plans, implements, and reviews code** in your repositories using a local LLM (llama.cpp). You tell it what you want in plain language and it explores the project, proposes a plan, writes the changes and — before calling it done — runs lint, tests and build to verify what it delivered actually compiles.

![AgentDevs TUI in action](docs/assets/tui.png)

*Real session: the user asks for the exposed endpoints; `inspect_routes` returns them in one call and the table renders in the TUI.*

![AgentDevs — task completed with verification](docs/assets/agentDevs-CompleteTask.png)

*Real session: the user asks for the "PACIENTES" card to navigate to `/pacientes`; the agent requests approval, applies `onClick`, runs `lint/tests/build` and delivers with `✅ Tarea completada`.*

## Why AgentDevs?

- **Total privacy**: everything runs on your hardware. Ideal for proprietary or sensitive code.
- **Role-based workflow, like a real team**: each task goes through the right role (`analyze`, `plan`, `execute`, `review`) with its own toolset and budgets.
- **Understands architecture, not just files**: integrates with [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) (code knowledge graph) to answer "how is this project structured?" with real clusters, layers and dependencies.
- **Anti-disaster guards**: tool-call budget per turn, per-file edit limits, truncated-write detection, and a verification gate that forces lint/tests/build before closing a change.
- **Large tasks in batches**: asking it to touch 14 files splits the work into persisted batches that survive interruptions and session rotation.
- **Full-screen TUI**: scroll, click-to-edit, selection/copy, and markdown-rendered responses (tables, code, headers).

## Installation

### One-liner (macOS / Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/LucianoDPerez/agent-devs/main/install.sh | bash
```

Clones the project into `~/.agent-devs`, creates the environment, installs the global **`agent-devs`** command in your PATH and runs a full verification. Re-running it updates the checkout (`git pull`).

### Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/LucianoDPerez/agent-devs/main/install.ps1 | iex
```

### From a clone (if you prefer the code at hand)

```bash
git clone https://github.com/LucianoDPerez/agent-devs.git && cd agent-devs
./install.sh        # or .\install.ps1 on Windows
```

The installer handles everything:

1. Creates the virtual environment (`.venv`) and installs dependencies.
2. Installs the package in editable mode → global **`agent-devs`** command from any folder.
3. Installs [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) if missing (macOS/Linux).
4. Runs **`agent-devs doctor`**: checks Python, git, dependencies, MCP and llama-server; installs what it can, and tells you exactly how to fix the rest.

### Updating

When new changes are published:

```bash
agent-devs --update       # git pull + editable reinstall, shows which commits changed
```

Or re-run the install one-liner: it detects the existing install and updates instead of duplicating.

## Getting started

**1. Start the model** (llama-server listening on `http://localhost:8080`, the default):

```bash
llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M --port 8080
```

The doctor (`agent-devs --doctor`) detects your RAM and highlights your tier. Summary (always Q4_K_M, always `--port 8080`):

| Your RAM | Command | Size | Notes |
|---|---|---|---|
| 16GB | `llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M` | ~2.5GB | bench-tested; alt. Spark-X2.5-4B (better agentic, but requires the `XHToken/llama.cpp` fork) |
| 24GB | `llama-server -hf unsloth/Qwen3.5-9B-GGUF:Q4_K_M` | ~5.5GB | 5/5 analysis + 1/2 execution on our bench |
| 32GB | `llama-server -hf unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL` | ~7GB | tested on the small-llm bench |
| 64GB+ | `llama-server -hf unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` | ~17GB | current best on external benchmarks; fully tested here: Qwen3.6-35B-A3B (7/7) |

> We only recommend families that passed our bench with real tool calling. Base Qwen3 is untested here — not recommended.
> Workflow that pays off: plan with a frontier model, execute locally, and judge with a different model than the executor.

**2. Go to any project and talk to it:**

```bash
cd /path/to/your/project
agent-devs .
```

The `.` tells it to work on the current directory (it also accepts an explicit path).

### What you can ask it

| You want… | Try… |
|---|---|
| Understand the project | *"explain this repo's architecture"* |
| Implement something | *"add a REST endpoint for patients with validation and tests"* |
| Plan before coding | *"make a plan to migrate to Postgres"* |
| Code review | *"review the last commit for bugs and code smells"* |
| Large changes | *"rename the entity Consulta to Appointment in all files"* |

When it finishes a change it offers to commit; it never pushes unless you ask.

## High-density tools for small models

AgentDevs doesn't hand an LLM a grep and hope for the best: its exploration tools are **high-density** — a single call returns what would take 5-10 reads by hand. Fewer reasoning steps, fewer tokens, fewer places to fail. Turn budgets are calibrated around exactly this.

| Tool | What it returns in one call |
|---|---|
| `inspect_routes` | All HTTP endpoints in the project: method, route and purpose. Scans Next.js, Express, FastAPI, Flask, Go (chi/gin), Spring, PHP and .NET |
| `inspect_models` | All data models/tables: Prisma, SQLAlchemy, Django ORM, TypeORM, Mongoose, Rails — with relations and file |
| `inspect_env` | The environment variables the project needs (reads only `.env.example`, never the real `.env`) |
| `trace_component` | A full component: its source + who uses it + the page that renders it |
| `search_code` | Semantic search over the code knowledge graph (MCP) |
| `probe_http` / `capture_dev_server` | Runtime evidence: probe a local URL or capture dev-server startup logs |
| `run_install / run_lint / run_tests / run_build` | Real verification for the detected stack (npm, uv, gradle, maven…) |
| `stage_files / create_commit / git_restore` | Safe, scoped git — no raw commands |

Each role receives **only the subset it needs**: `analyze` cannot write, and the `execute` retry loses even search to force delivery.

## Commands

```text
agent-devs .              # open the agent on the current repo
agent-devs /other/repo    # open on a specific path
agent-devs --doctor       # verify the environment and install what's missing
agent-devs --update       # update this installation (git pull + reinstall)
agent-devs --list         # list already-analyzed repos (cache)
agent-devs --analyze REPO # pre-analyze a repo without opening a session
```

Inside the session: **ESC** cancels the running turn · **Ctrl+C ×2** quits · `/new` new session (clears the panel) · `/compact` summarizes the history to free context · `/history` last turns (with id) · `/resume <id>` resumes a previous session · `/autoapprove [on|off]` approves writes without asking (commit stays manual; `/new` turns it off) · `/verify` runs lint/tests/build now · `/help` help. Typing `/` opens the command menu (Enter accepts, Esc closes, Tab completes).

The context limit is **detected from the server** (`/props` of llama.cpp): when the session reaches ~80%, AgentDevs warns you and offers to compact; at 90% it compacts automatically.

## Requirements

| Component | Requirement | Auto-installed? |
|---|---|---|
| Python | 3.10+ | — |
| git | modern | — |
| llama.cpp (`llama-server`) | recent build (b5000+; b6200+ for MTP/spec-draft) listening on `:8080` | detects and guides per OS |
| codebase-memory-mcp | 0.8+ | yes (macOS/Linux); Windows: manual |

> **Windows note**: `install.ps1` is provided and tested on real Windows
> hardware (execution policy, spaced paths, UTF-8). If you hit a problem,
> open an issue.

## Known limitations (read before using)

AgentDevs is designed for small models and works well on bounded tasks, but it has measured limits (see `benchmarks/ANALISIS.en.md`):

- **Small models (4B/9B)**: complete ~86% of the benchmark (5/5 analysis + 1/2 execution). The task that escapes them is usually fine-grained implementation.
- **Deep debugging without a stack trace**: if you don't paste the exact error (log, console, stack), no model converges reliably — paste the error evidence in the prompt.
- **PLAN does not explore**: the planner role answers from the cached analysis (0 tools across the 3 models tested) — a known design gap.
- **PATH FIX codemod**: when EXECUTE starts, the harness automatically fixes frontend↔backend path mismatches (announced in console). Disable with `PATH_FIX_ENABLED = False` in `config.py`.
- **Time > capability**: on local hardware, long executes can take tens of minutes; timeouts are tuned in `config.py` (`LLM_TIMEOUT`, `TURN_IDLE_TIMEOUT`).

## Troubleshooting

- **Agent not responding / hangs on start** → almost certainly the model is missing: run `agent-devs doctor`; if it says "nobody answers on http://localhost:8080", start llama-server.
- **`cm__*` tools missing** → codebase-memory-mcp is missing; the doctor installs it on macOS/Linux.
- **Want another port/model** → edit `LLM_BASE_URL` and `LLM_MODEL_NAME` in `config.py`.

## Contact

Want to contribute, report something, or talk about the project?

- **GitHub**: [LucianoDPerez](https://github.com/LucianoDPerez)
- **Email**: [lucianoperezvic84@gmail.com](mailto:lucianoperezvic84@gmail.com)
- **LinkedIn**: [Luciano David Perez](https://www.linkedin.com/in/luciano-david-perez-6172a11a0/)

For large contributions, email first to coordinate scope.

## How it works (the short version)

Every message goes through a **router** that picks the right role. Each role has a bounded tool subset: `analyze` only reads, `execute` writes but is subject to a **verification gate** (lint/tests/build) before closing, and `review` compares against the diff. The MCP knowledge graph gives architectural vision; the SQLite cache gives memory across sessions; tool-call budgets prevent infinite loops.

Want the long version with diagrams, budgets and design decisions?

📘 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

---

*[Español](README.es.md) · English*
