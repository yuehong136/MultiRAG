# MultiRAG Project Instructions for GitHub Copilot

This file provides context, build instructions, and coding standards for the MultiRAG project.
It is structured to follow GitHub Copilot's [customization guidelines](https://docs.github.com/en/copilot/concepts/prompting/response-customization).

## 1. Project Overview
MultiRAG is an enterprise-grade RAG (Retrieval-Augmented Generation) backend engine based on deep document understanding. It is a Python backend application with FastAPI.

- **Backend**: Python 3.12+ (FastAPI)
- **Architecture**: Microservices based on Docker.
  - `api/`: Backend API server.
  - `core/`: Core RAG logic (indexing, retrieval, LLM integration).
  - `deepdoc/`: Document parsing and OCR.

## 2. Directory Structure
- `api/`: Backend API server (FastAPI).
  - `apps/`: API routers (Knowledge Base, Chat, Document, etc.).
  - `db/`: Database models and services.
- `core/`: Core processing logic.
  - `llm/`: LLM, Embedding, and Rerank model abstractions.
- `deepdoc/`: Document parsing and OCR modules.
- `agent/`: Agentic reasoning components.
- `common/`: Shared utilities and data source connectors.
- `docker/`: Docker deployment configurations.

## 3. Build Instructions

### Backend (Python)
The project uses **uv** for dependency management.

1. **Setup Environment**:
   ```bash
   uv sync --python 3.12 --all-extras
   uv run python download_deps.py
   ```

2. **Run Server**:
   - **Pre-requisite**: Start dependent services (PostgreSQL/MySQL, ES/Infinity/Milvus, Redis, MinIO).
     ```bash
     docker compose -f docker/docker-compose-base.yml up -d
     ```
   - **Launch**:
     ```bash
     source .venv/bin/activate
     export PYTHONPATH=$(pwd)
     uv run python -m api.multirag_server
     ```

### Docker Deployment
To run the full stack using Docker:
```bash
cd docker
docker compose up -d
```

Start with specific profile:
```bash
docker compose up -d --profile cpu
docker compose up -d --profile gpu
```

## 4. Testing Instructions

### Backend Tests
- **Run All Tests**:
  ```bash
  uv run pytest
  ```
- **Run Specific Test**:
  ```bash
  uv run pytest tests/unit/test_image_filter.py
  ```

## 5. Coding Standards & Guidelines

### Python Version
- **Requires**: Python 3.12+ (strict version bound: >=3.12,<3.13)

### Type Hints (Python 3.12+)
- Use `list[str]` instead of `List[str]`
- Use `dict[str, Any]` instead of `Dict[str, Any]`
- Use `str | None` instead of `Optional[str]`
- Use `str | int` instead of `Union[str, int]`

### FastAPI 0.128+
- Use `Query(..., pattern="pattern")` instead of `regex=`
- Use `Body(..., examples=[{...}])` instead of `example=`
- Use `FastAPI(lifespan=lifespan)` instead of `@app.on_event("startup")`

### Pydantic V2
- Use `model_config = ConfigDict(...)` instead of `class Config:`
- Use `.model_dump()` instead of `.dict()`
- Use `.model_validate()` instead of `.parse_obj()`
- Use `@field_validator` instead of `@validator`

### SQLAlchemy 2.0
- Use `Mapped[type]` + `mapped_column()` instead of `Column()`
- Use `session.get(Model, pk)` for primary key queries (leverages identity map)
- Use `select(M).where(...)` instead of `session.query(M).filter(...)`

### Linting
- **Formatting**: Use `ruff` for linting and formatting.
  ```bash
  ruff check
  ruff format
  ```

## Cursor Cloud specific instructions

### Infrastructure services

The API server requires PostgreSQL, Redis, MinIO, and one vector DB engine (Elasticsearch by default in this environment). These run via Docker Compose:

```bash
# Start required base services (Postgres, Redis, MinIO) + Elasticsearch
docker compose -f docker/docker-compose-base.yml --profile elasticsearch up -d
```

In the Cloud Agent VM (nested Docker), the `mem_limit` directive causes cgroup errors for Elasticsearch. Work around this by running ES directly with `docker run` (no `--memory` flag) on the `docker_multirag` network, mapping port 1200:9200. See the `.env` file in `docker/` for port mappings.

### Local config override

The config system reads `configs/local.service_conf.yaml` which overrides `configs/service_conf.yaml`. For local development, create this file with connection details pointing to `127.0.0.1` for all services (PostgreSQL on 5432, Redis on 6379, MinIO on 9000, ES on 1200). Ensure `/etc/hosts` maps `es01`, `minio`, `redis`, `postgres` to `127.0.0.1`.

### Running the server

```bash
export DOC_ENGINE=elasticsearch
export PYTHONPATH=$(pwd)
uv run python -m api.multirag_server
```

The server starts on port 8123. Database tables and Alembic migrations run automatically on first startup.

### Key gotchas

- `python3.12-dev` system package is required for building native extensions (e.g. `datrie` via `infinity-sdk`).
- The `pyproject.toml` `requires-python` is `>=3.12,<3.15`; the system Python 3.12 works.
- The `download_deps.py` script downloads large ML models from HuggingFace — skip in CI/dev unless you need OCR/embedding features locally.
- The only test file (`api/service/docx2zjform_service/analyzer/test_table_analyzer_util.py`) has a pre-existing relative-import error; use `--ignore=api/service/docx2zjform_service/` with pytest to avoid collection failures.
- `ruff check` reports ~599 pre-existing lint warnings; these are not introduced by your changes.
- Swagger docs are disabled by default (`docs_url=None` in `api/apps/__init__.py`); use `/redoc` or call the API directly.
- Auth flow: POST to `/auth/token` with form data `username=<email>&password=<pw>` to get a JWT Bearer token.
