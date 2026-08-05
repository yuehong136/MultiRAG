# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠ 权威指针（最重要的一条）

**验证流程、写测试规范、mypy 棘轮、配置与资源访问规范、编码规范速查——一切以根目录
[AGENTS.md](AGENTS.md) 为准**，它是唯一权威来源，本文件不再重复其内容。
最低要求：任何编码任务完成前 `make verify` 必须全绿（`make help` 列出全部验证目标）；
修根因，不改门禁。

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
  - `restful_apis/` - RESTful `/api/v1` 端点（新代码优先落这里）
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

### 停滞的并行实现（不在门禁范围，活代码禁止依赖）
- `server/`（旧 Python 实现）与 Go 移植（`cmd/` + Go 侧 `internal/`）

## Key Configuration Files

- `configs/service_conf.yaml` - Main service configuration (databases, LLM, storage, authentication)
- `configs/local.service_conf.yaml` - 本地覆盖（gitignored，按顶层 section 整体替换）
- `configs/llm_factories.json` - LLM provider factory configurations
- `configs/es_mapping.json` / `configs/infinity_mapping.json` - Vector DB index mappings
- `pyproject.toml` - Python dependencies and project metadata（含 ruff/mypy/coverage/import-linter 门禁配置）
- `docker/.env` - Docker environment variables
- `alembic.ini` + `configs/alembic/` - Database migration configuration

## Database Engines

MultiRAG supports multiple vector database backends:

- **Vector**: Milvus（默认）/ Elasticsearch / Infinity / OpenSearch——`configs/service_conf.yaml` 对应 section
- **Relational**: PostgreSQL（默认）/ MySQL / VastBase
- **Storage**: MinIO（默认）/ AWS S3 / Azure Blob / Aliyun OSS

## Docker Operations（AGENTS.md 未覆盖的部分）

```bash
# Full Docker deployment（profile 由 docker/.env 的 COMPOSE_PROFILES 决定）
cd docker && docker compose up -d

# 临时指定 profile：--profile 必须在子命令之前，放在 up 之后会 unknown flag
docker compose --profile cpu up -d
docker compose --profile gpu up -d

# Build production image
docker build -t multirag:latest .

# Build lightweight image
docker build --build-arg LIGHTEN=1 -t multirag:slim .

# 看日志：compose 用服务名（multirag-cpu），docker 用容器名（multirag）
docker compose logs -f multirag-cpu
docker logs -f multirag --tail 100
```

主机重启后所有服务自动拉起（全部 `restart: unless-stopped`）；若主服务早于依赖启动而停在
unhealthy，`docker compose restart multirag-cpu` 即可。开机自启 systemd 单元与排查方式见
[docker/README.md](docker/README.md) 的「主机重启与自动恢复」。

其他入口（基础服务、API server、task executor 的启动命令）见 AGENTS.md「服务与运行」。
MCP server（self-host）：`uv run python mcp/server/server.py --host=127.0.0.1 --port=9382 --mode=self-host --api-key=<your-key>`；
模型依赖下载：`uv run python download_deps.py`。

## Claude Code 特有事项

- **PostToolUse 钩子**（`.claude/settings.json` → `scripts/hooks/post_edit_ruff.py`）：每次编辑 .py
  单文件即时 ruff 自动修复 + mypy 纳管范围内 dmypy 增量类型检查，残留问题通过 exit 2
  回灌给 Claude 当场修复；`server/`、`internal/` 等停滞/笔记目录自动跳过。
- **项目 skill**：`port-ragflow-commit`——跟进 ragflow 上游提交时必用。
- `internal/*.md` 是用户本地笔记：可读可编辑，**绝不 git add**。Channel 审计的完整版
  （含未修复问题的复现细节、上游对照）在 `internal/channel-audit-2026-08.md`；
  收敛后的方案、账本与前后端契约在 `docs/channel-program/`（**已入库**，冷启动读它的
  [README](docs/channel-program/README.md)）。分层原则：入库讲我们自己的代码，本地讲别人的。
