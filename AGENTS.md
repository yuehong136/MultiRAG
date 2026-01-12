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

### Linting
- **Formatting**: Use `ruff` for linting and formatting.
  ```bash
  ruff check
  ruff format
  ```
