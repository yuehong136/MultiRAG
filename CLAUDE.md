# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MultiRAG is an enterprise-grade RAG (Retrieval-Augmented Generation) backend engine based on deep document understanding. It provides:
- Python backend (FastAPI-based API server)
- Microservices architecture with Docker deployment
- Multiple data stores (PostgreSQL/MySQL, Milvus/Elasticsearch/Infinity, Redis, MinIO)
- Multi-provider LLM integration and workflow orchestration

## Architecture

### Backend (`/api/`)
- **Main Server**: `api/multirag_server.py` - FastAPI application entry point (port 8123)
- **Apps**: Modular FastAPI routers in `api/apps/` for different functionalities:
  - `kb_app.py` - Knowledge base management
  - `dialog_app.py` - Chat/conversation handling
  - `document_app.py` - Document processing
  - `canvas_app.py` - Agent workflow canvas
  - `file_app.py` - File upload/management
  - `llm_app.py` - LLM model management
  - `workflow_app.py` - Workflow execution
  - `guard_*.py` - Security guard and content filtering
- **Services**: Business logic in `api/db/services/`
- **Models**: Database models in `api/db/db_models.py`

### Core Processing (`/core/`)
- **LLM Integration**: `core/llm/` - Multi-provider model abstractions for chat, embedding, reranking, vision, TTS
- **Document Flow**: `core/flow/` - Chunking, parsing, tokenization, extraction pipelines
- **Task Executor**: `core/svr/task_executor.py` - Distributed task processing with Redis Streams
- **RAG Application**: `core/app/` - Core RAG engine implementation
- **Prompts**: `core/prompts/` - System-level prompt templates

### Agent System (`/agent/`)
- **Components**: Modular workflow components (LLM, retrieval, categorize, etc.)
- **Templates**: Pre-built agent workflows in `agent/templates/`
- **Tools**: External API integrations (Tavily, Wikipedia, SQL execution, etc.)

### Document Understanding (`/deepdoc/`)
- **Parser**: `deepdoc/parser/` - Multi-format document parsing (PDF, DOCX, PPTX, Excel, HTML, Markdown)
- **Vision**: `deepdoc/vision/` - OCR, layout recognition, table structure extraction

### Knowledge Graph (`core/graphrag/`)
- **General**: Full-featured knowledge graph with Leiden clustering in `core/graphrag/general/`
- **Light**: Lightweight implementation in `core/graphrag/light/`
- **Entity Resolution**: Disambiguation and linking

### Data Connectors (`/common/data_source/`)
- Connectors for SharePoint, Dropbox, Jira, Notion, Slack, Gmail, Teams, Discord, Google Drive, Azure Blob

## 验证体系（权威文档：AGENTS.md）

**任何编码任务完成前必须 `make verify` 全绿**（= ruff format/check + mypy + 单元测试）。
完整的验证金字塔、写测试规范、mypy 棘轮策略见 **AGENTS.md**——它是验证流程的唯一权威来源。

```bash
make help          # 列出全部验证目标
make verify        # Tier 0+1+2：编码后标准门禁（必跑）
make fix           # 自动修复 lint/格式（不要手工排版）
make integration   # Tier 3：需要 docker compose base 服务
make smoke         # Tier 4：对运行中的服务器打健康端点
```

修根因，不改门禁：禁止删/跳测试、扩 ruff ignore、扩 mypy exclude 来换绿。

## Common Development Commands

### Package Management
```bash
# Install dependencies (dev group, locked)
make install    # = uv sync --group dev --frozen

# Download model dependencies
uv run python download_deps.py
```

### Running the Application
```bash
# Start API server
uv run python -m api.multirag_server

# Start API server in debug mode
uv run python -m api.multirag_server --debug

# Start task executor
uv run python -m core.svr.task_executor

# Start task executor with worker ID
uv run python -m core.svr.task_executor worker_001

# Start MCP server (self-host mode)
uv run python mcp/server/server.py --host=127.0.0.1 --port=9382 --mode=self-host --api-key=<your-key>
```

### Testing
```bash
# 单元测试（无需外部服务；tests/unit 平铺，纯 monkeypatch 风格）
make test

# 单元 + 集成（集成测试在服务缺失时自动跳过）
make test-all

# 覆盖率报告
make coverage

# 跑单个测试文件
uv run pytest tests/unit/test_image_filter.py -q
```

### Development Environment Setup
```bash
# Set environment variables for development
export HF_ENDPOINT=https://hf-mirror.com
export PYTHONPATH=$(pwd)

# Configure hosts for local development (add to /etc/hosts)
# 127.0.0.1 es01 infinity mysql minio redis
```

### Docker Operations
```bash
# Start base services only (databases, Redis, MinIO)
docker compose -f docker/docker-compose-base.yml up -d

# Full Docker deployment
cd docker && docker compose up -d

# Start with specific profile
docker compose up -d --profile cpu
docker compose up -d --profile gpu

# Build production image
docker build -t multirag:latest .

# Build lightweight image
docker build --build-arg LIGHTEN=1 -t multirag:slim .

# Check server logs
docker logs -f multirag-server
```

## Key Configuration Files

- `configs/service_conf.yaml` - Main service configuration (databases, LLM, storage, authentication)
- `configs/llm_factories.json` - LLM provider factory configurations
- `configs/mapping.json` - General mapping configurations
- `configs/es_mapping.json` - Elasticsearch index mapping
- `configs/infinity_mapping.json` - Infinity vector DB mapping
- `pyproject.toml` - Python dependencies and project metadata
- `docker/.env` - Docker environment variables
- `alembic.ini` - Database migration configuration

## Database Engines

MultiRAG supports multiple vector database backends:

### Vector Databases
- **Milvus** (default): Set in `configs/service_conf.yaml` under `milvus` section
- **Elasticsearch**: Configure under `elasticsearch` section
- **Infinity**: Lightweight option, configure under `infinity` section
- **OpenSearch**: Enterprise option, configure under `opensearch` section

### Relational Databases
- **PostgreSQL** (default): Configure under `postgresql` section
- **MySQL**: Alternative relational backend
- **VastBase**: Vector-enhanced database option

### Storage
- **MinIO** (default): S3-compatible object storage
- **AWS S3**: Cloud storage option
- **Azure Blob**: Azure storage integration
- **Alibaba OSS**: Aliyun storage option

## Development Environment Requirements

- Python 3.12+ (version bound: >=3.12,<3.15)
- uv package manager
- Docker & Docker Compose
- 16GB+ RAM, 50GB+ disk space

## Code Style Guidelines

### Python 3.12+

| ❌ 避免 | ✅ 推荐 |
|--------|--------|
| `List[str]`, `Dict[str, Any]` | `list[str]`, `dict[str, Any]` |
| `Optional[str]`, `Union[str, int]` | `str \| None`, `str \| int` |

### FastAPI 0.128+

| ❌ 弃用 | ✅ 推荐 |
|--------|--------|
| `Query(..., regex="pattern")` | `Query(..., pattern="pattern")` |
| `Body(..., example={...})` | `Body(..., examples=[{...}])` |
| `@app.on_event("startup")` | `FastAPI(lifespan=lifespan)` |

### Pydantic V2

| ❌ 弃用 | ✅ 推荐 |
|--------|--------|
| `class Config:` 内部类 | `model_config = ConfigDict(...)` |
| `.dict()` | `.model_dump()` |
| `.parse_obj()` | `.model_validate()` |
| `@validator` | `@field_validator` |

### SQLAlchemy 2.0

| ❌ 弃用 | ✅ 推荐 |
|--------|--------|
| `Column(String)` | `name: Mapped[str] = mapped_column(String)` |
| `session.query(M).filter(...)` | `select(M).where(...)` |
| `select(M).where(M.id == pk)` | `session.get(Model, pk)` — 主键查询，利用identity map |