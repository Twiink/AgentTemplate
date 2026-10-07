# Agent Template

A Python FastAPI backend starter for building AI agents. The project uses `uv` for
dependency and virtual-environment management and keeps the application under `src/`.

## Project structure

```text
src/app/
├── __init__.py
├── main.py
├── dependencies.py
├── api/
│   └── health.py
├── core/
│   └── config.py
├── schemas/
│   └── health.py
├── services/
│   └── health.py
└── repositories/
tests/
```

Feature modules should use a consistent path through the layers: API → schema → service →
repository. Keep `main.py` focused on application assembly and router registration.

The example image puts `app/` at the repository root. This project keeps the same internal
modules under `src/app/`: uv defaults packaged projects to a `src/` layout, and the Python
Packaging User Guide describes how that layout avoids accidental imports from the source tree.
FastAPI's multi-file guide independently uses an `app/` package with `main.py`,
`dependencies.py`, and router modules. The repository layer is an additional data-access boundary;
FastAPI leaves that application architecture to the project.

Use `.env.example` as the tracked configuration template and keep local `.env` files untracked.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

## Set up

```bash
uv sync --all-extras --all-groups
cp .env.example .env
uv run fastapi dev
```

The API is available at <http://127.0.0.1:8000>. Interactive API documentation is at
<http://127.0.0.1:8000/docs>, and the liveness endpoint is `/api/v1/health`.

`uv sync` installs the core dependencies and development tools. The `--all-extras`
option also installs the optional `providers`, `worker`, and `auth` dependencies.

## Git hooks

Install the repository's Git hooks once in each clone:

```bash
sh scripts/install-git-hooks.sh
```

The hook validates commit messages in the format `<type>(<scope>): <subject>`. The subject
must contain Chinese, and the message must have one line with no body or trailers. Supported
types are `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, and `chore`.

The hook set also includes:

- pre-commit: checks staged Python files with Ruff and prevents checks against partially staged files.
- pre-push: checks the lockfile, Ruff rules and formatting, and Python types.
- post-merge: refreshes hooks when their files change and reminds you to sync dependencies.
- pre-rebase: blocks rebasing main, master, and release branches; set
  AGENT_TEMPLATE_ALLOW_PROTECTED_REBASE=1 for an intentional override.

core.hooksPath is repository-local. .openviking/config.json is shared with the repository;
its explicit peer ID matches the normalized Git origin. Machine-specific overrides and
generated OpenViking data are ignored.

## Dependency groups

- Core: FastAPI, Pydantic Settings, LangChain, LangGraph, OpenAI integration, PostgreSQL,
  pgvector, Redis, MCP, and structured logging.
- `providers`: Anthropic and Google model integrations.
- `worker`: Celery with Redis support.
- `auth`: JWT and Argon2 password hashing packages.
- `dev`: Ruff and ty.

Add core dependencies with `uv add <package>`. Add a development dependency with
`uv add --dev <package>`. The committed `uv.lock` records the resolved dependency set.
