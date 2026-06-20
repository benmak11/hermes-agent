# Hermes

A multi-agent job discovery and application system, built on the
[ADK](https://adk.dev/). A Coordinator delegates to five specialists:

| Agent | Type | Model | Role |
|-------|------|-------|------|
| Coordinator | LlmAgent | `gemini-flash-latest` | Orchestrates the end-to-end flow |
| Discovery | ParallelAgent | — | Scouts job sources concurrently (job boards + company careers) |
| Matching | LlmAgent | `gemini-3-pro` | Ranks postings against the candidate profile |
| Tailoring | LlmAgent | `gemini-flash-latest` | Tailors resume and cover letter per posting |
| Application | LlmAgent | `gemini-3-pro` | Submits applications via the Computer Use browser tool |
| Tracking | LlmAgent | `gemini-flash-latest` | Records and reports application status |

Scaffolded with `agents-cli` version `0.5.0` (Cloud Run + FastAPI gateway).

## Project Structure

```
hermes/
├── agents/                    # Agent code (one package per agent)
│   ├── _shared.py             # Env setup + model helpers (FLASH/PRO models)
│   ├── coordinator/agent.py   # Root agent; assembles the sub-agents
│   ├── discovery/agent.py     # ParallelAgent fan-out over job sources
│   ├── matching/agent.py      # Gemini 3 Pro matcher
│   ├── tailoring/agent.py     # Gemini Flash tailoring
│   ├── application/agent.py   # Computer Use submission agent
│   │   └── computer.py        # Browser backend (BaseComputer)
│   └── tracking/agent.py      # Gemini Flash tracker
├── api/main.py                # FastAPI gateway (serves the agents)
├── deployment/                # Terraform for Cloud Run + Cloud SQL
├── tests/                     # Unit, integration, and eval tests
├── .env.example               # Copy to .env and fill in
├── Dockerfile
├── CLAUDE.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
```

The gateway uses ADK's agent discovery (`agents_dir="agents"`), so every package
under `agents/` is an independently runnable app — `coordinator` is the primary
one (clients call `/run` with `app_name="coordinator"`), and the specialists can
be exercised in isolation in the playground.

> ⚠️ **Note:** This project uses a custom `agents/` + `api/` layout that differs
> from the default `agents-cli` `app/` convention. The `agents-cli playground` /
> `deploy` / `eval` commands assume the default layout and may need the agent
> directory passed explicitly (or use `uv run adk web agents` and
> `uv run uvicorn api.main:app` directly).

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)


## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        |
| `agents-cli deploy`  | Deploy agent to Cloud Run                                                                   |

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit each agent under `agents/<name>/agent.py`. Run the dev UI over all agents:

```bash
uv run adk web agents
```

Or run the FastAPI gateway directly:

```bash
uv run uvicorn api.main:app --reload --port 8000
```

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```

To add CI/CD and Terraform, run `agents-cli scaffold enhance`.
To set up your production infrastructure, run `agents-cli infra cicd`.

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.
