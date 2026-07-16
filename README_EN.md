<div align="center">
<h1>MultiRAG</h1>
<p>Enterprise-grade backend engine for RAG, Agents, Workflows, and MCP</p>
</div>

<p align="center">
  <a href="./README.md"><img alt="简体中文" src="https://img.shields.io/badge/简体中文-DBEDFA"></a>
  <a href="./README_EN.md"><img alt="English" src="https://img.shields.io/badge/English-DFE0E5"></a>
</p>

<p align="center">
  <a href="https://github.com/yuehong136/MultiRAG/releases/latest">
    <img src="https://img.shields.io/github/v/release/yuehong136/MultiRAG?color=2e6cc4&label=release" alt="Latest Release">
  </a>
  <a href="https://github.com/yuehong136/MultiRAG/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-Apache--2.0-4c7ed8" alt="License">
  </a>
  <a href="https://www.python.org/downloads/release/python-3120/">
    <img src="https://img.shields.io/badge/python-3.12%2B-3776ab" alt="Python 3.12+">
  </a>
  <a href="./pyproject.toml">
    <img src="https://img.shields.io/badge/version-0.9.9-2f9e44" alt="Version 0.9.9">
  </a>
</p>

<p align="center">
  <a href="#-what-is-multirag">Overview</a> |
  <a href="#-release-focus">Release Focus</a> |
  <a href="#-core-capabilities">Capabilities</a> |
  <a href="#-quick-start">Quick Start</a> |
  <a href="#-development-from-source">Development</a> |
  <a href="#-api-and-documentation">API & Docs</a>
</p>

<details open>
<summary><b>Table of Contents</b></summary>

- [What is MultiRAG?](#-what-is-multirag)
- [Release Focus](#-release-focus)
- [Core Capabilities](#-core-capabilities)
- [System Architecture](#-system-architecture)
- [Project Structure](#-project-structure)
- [Quick Start](#-quick-start)
- [Docker Profiles](#-docker-profiles)
- [Docker Image Build](#-docker-image-build)
- [Configuration](#-configuration)
- [Development from Source](#-development-from-source)
- [API and Documentation](#-api-and-documentation)
- [Typical Use Cases](#-typical-use-cases)
- [Contributing](#-contributing)
- [License](#-license)

</details>

## 📌 What is MultiRAG?

MultiRAG is an enterprise-oriented AI backend project built around RAG, GraphRAG, Workflow orchestration, Agents, and MCP. It provides an end-to-end service layer for document understanding, ingestion, retrieval, QA, tool invocation, workflow execution, and open APIs.

It is suitable for building:

- enterprise knowledge base QA and retrieval-augmented systems
- multi-source ingestion and unified retrieval platforms
- Agent and MCP-powered backend services
- document understanding, table extraction, and multimodal retrieval services
- OpenAI-compatible capability layers for downstream products

## 🔥 Release Focus

Current repository version is `v0.9.9`. Compared with older docs, the project scope is significantly broader:

- MCP client/server support with FastMCP 3 and Streamable HTTP
- stronger Agent, Workflow, and Workflow v2 execution chains
- broader enterprise connectors and relational DB integrations
- backend support across Milvus, Infinity, Elasticsearch, OpenSearch, OceanBase, and SeekDB
- richer OpenAI-compatible responses, including references and metadata-related enhancements
- deeper document understanding with OCR, table rotation correction, PDF Vision, and PaddleOCR flows

## ✨ Core Capabilities

### Retrieval and knowledge augmentation

- Supports standard RAG, GraphRAG, SQL retrieval, hybrid recall, and reranking
- Supports document metadata storage, filtering, references, and conditional retrieval
- Exposes OpenAI-compatible response formats for easier SDK and client integration
- Supports multiple chunking strategies, retrieval thresholds, and ranking controls

### Deep document understanding

- Supports PDF, DOCX, PPT/PPTX, Excel, images, and other common formats
- Built on DeepDoc for OCR, layout analysis, tables, and figures
- Includes PaddleOCR, PaddleOCR-VL, PDF Vision, and Docling-based flows
- Handles parent-child chunking, page fixes, table orientation detection, and correction

### Agents, workflows, and MCP

- Agent component system with tools, memory, file inputs, and multi-step execution
- Workflow and Workflow v2 orchestration modules
- MCP in both client and server directions
- Structured tool outputs, SSE side-channel logs, and timeout control

### Data source connectivity

- Covers GitHub, GitLab, Bitbucket
- Covers Jira, Confluence, Notion, Airtable, Asana, Moodle
- Covers Google Drive, SharePoint, Dropbox, Box, Seafile, WebDAV
- Covers Slack, Teams, Discord, Gmail, IMAP, Zendesk
- Supports relational data connectors (MySQL / PostgreSQL)

### Model and platform integration

- Integrates with OpenAI, Anthropic, Gemini, Tongyi, DashScope, Volcengine, Groq, Ollama, Vertex AI, and more
- Supports Chat, Embedding, Rerank, Vision, TTS, and Sequence2Text
- Supports provider routing, factory configuration, enable/disable controls, and OpenAI-compatible access

### Operations and engineering

- Built on FastAPI for service deployment and extension
- Includes Docker Compose deployment, source-based development, Admin APIs, CLI, and tests
- Ships with task executors, health checks, system settings, logging, and permission controls

## 🔎 System Architecture

```text
┌─────────────────────────────────────────────────────────────────────┐
│                              MultiRAG                               │
├─────────────────────────────────────────────────────────────────────┤
│ API / Protocol Layer                                                │
│  - FastAPI REST APIs                                                │
│  - OpenAI-compatible APIs                                           │
│  - MCP Server / SSE Stream                                          │
│  - Admin APIs / CLI                                                 │
├─────────────────────────────────────────────────────────────────────┤
│ Core Engine Layer                                                   │
│  - RAG / GraphRAG / SQL Retrieval                                   │
│  - Agent / Workflow / Workflow v2                                   │
│  - LLM / Embedding / Rerank / Vision / TTS                          │
│  - Task Executor / Sync / Background Jobs                           │
├─────────────────────────────────────────────────────────────────────┤
│ Parsing and Integration Layer                                       │
│  - DeepDoc / OCR / Table / PDF Vision                               │
│  - Enterprise Data Source Connectors                                │
│  - MCP Client / Tool Invocation                                     │
├─────────────────────────────────────────────────────────────────────┤
│ Storage Layer                                                       │
│  - Vector/Search: Milvus / Infinity / ES / OpenSearch               │
│  - Database: PostgreSQL / MySQL / OceanBase / SeekDB                │
│  - Object Storage: MinIO / S3 / OSS / Azure Blob                    │
└─────────────────────────────────────────────────────────────────────┘
```

## 🗂️ Project Structure

The repository has evolved from a single RAG service into a broader AI backend platform:

```text
multirag/
├── api/             # FastAPI entrypoint, routers, DB models, service layer
├── core/            # RAG/retrieval/LLM/parsing/execution core logic
├── deepdoc/         # Document parsing, OCR, layout analysis
├── agent/           # Agent components, tools, templates, plugins
├── workflow/        # Workflow v1
├── workflow_v2/     # Workflow v2
├── common/          # Shared utilities, connectors, storage adapters
├── mcp/             # MCP client/server
├── memory/          # Memory-related capabilities
├── admin/           # Admin server and CLI
├── docker/          # Docker Compose, Nginx, deployment configs
├── docs/            # Documentation and API references
├── tests/           # Unit and integration tests
└── tools/           # Firecrawl, chatgpt-on-wechat, and integrations
```

### Module overview

#### API layer

- [`api/apps`](./api/apps) contains routes for KB, documents, conversations, LLMs, connectors, MCP, workflows, system settings, and admin features
- Supports REST APIs, SSE streaming, and OpenAI-compatible endpoints

#### Core engine layer

- [`core/llm`](./core/llm) handles model adapters, embeddings, rerankers, vision, and TTS
- [`core/nlp`](./core/nlp) handles retrieval, search, query processing, and algorithms
- [`core/svr`](./core/svr) handles task execution, synchronization, and background jobs

#### Parsing and integration layer

- [`deepdoc`](./deepdoc) handles advanced document and image understanding
- [`common/data_source`](./common/data_source) contains enterprise connectors and sync logic
- [`common/doc_store`](./common/doc_store) contains vector/search backend adapters

## 🚀 Quick Start

### Requirements

- CPU `>= 4 cores`
- RAM `>= 16 GB` (32 GB recommended)
- Docker `>= 24`
- Docker Compose `>= v2.26`
- Python `>= 3.12` (for source deployment)

### Docker startup (recommended)

1. On Linux, check `vm.max_map_count` (especially for ES/Milvus):

```bash
sysctl vm.max_map_count
sudo sysctl -w vm.max_map_count=262144
```

2. Clone the repository:

```bash
git clone git@github.com:yuehong136/MultiRAG.git
cd MultiRAG
```

3. Start services:

```bash
cd docker

# minimal deployment (postgres + redis + minio + multirag)
docker compose --profile cpu up -d

# add Milvus
docker compose --profile cpu --profile milvus up -d

# add Elasticsearch
docker compose --profile cpu --profile elasticsearch up -d

# add Infinity
docker compose --profile cpu --profile infinity up -d

# add OceanBase
docker compose --profile cpu --profile oceanbase up -d

# add TEI
docker compose --profile cpu --profile milvus --profile tei-cpu up -d

# GPU mode
docker compose --profile gpu --profile milvus --profile tei-gpu up -d
```

4. Verify startup:

```bash
docker logs -f multirag
```

   _The following output confirms that the system has successfully started：_

   ```bash
                __  ___      ____  _ ____  ___   ______
               /  |/  /_  __/ / /_(_) __ \/   | / ____/
              / /|_/ / / / / / __/ / /_/ / /| |/ / __   v0.9.9
             / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /
            /_/  /_/\__,_/_/\__/_/_/ |_/_/  |_\____/

INFO:     Uvicorn running on http://0.0.0.0:8123 (Press CTRL+C to quit)
   ```
5. Configure model and storage settings:

- edit [`configs/service_conf.yaml`](./configs/service_conf.yaml)
- or use [`docker/service_conf.yaml.template`](./docker/service_conf.yaml.template)

6. Default API endpoint: `http://IP_OF_YOUR_MACHINE:8123`

### macOS (Apple Silicon)

```bash
cd docker
docker compose -f docker-compose-macos.yml --profile cpu up -d
```

Recommendations:

- enable Rosetta x86_64 emulation in Docker Desktop
- or use OrbStack with x86_64 emulation

For more combinations, see [`docker/README.md`](./docker/README.md).

### External model directory and image-only deployment

The MultiRAG image no longer bundles runtime embedding or rerank models. Set
`MULTIRAG_MODEL_DIR` to any host directory; its in-container target always
remains `/root/.ragdatav`:

```bash
mkdir -p /data/multirag/models
MULTIRAG_MODEL_DIR=/data/multirag/models \
  docker compose -f docker/docker-compose.yml --profile cpu --profile milvus up -d
```

Keep the model basenames flat under the mounted directory, and only provide the
local models your deployment uses:

```text
/data/multirag/models/
├── bge-large-zh-v1.5/
├── bge-reranker-v2-m3/
├── bce-embedding-base_v1/
└── bce-reranker-base_v1/
```

To prepare a mount-ready model directory separately:

```bash
HF_ENDPOINT=https://hf-mirror.com uv run --script download_deps.py \
  --runtime-models-only --runtime-model-dir /data/multirag/models
```

If a server has only the `datav/multirag:latest` image and no source checkout,
copy [`docker-compose-standalone.yml`](./docker/docker-compose-standalone.yml)
to it. This Compose file does not mount repository `configs/`, `entrypoint.sh`,
or source files:

```bash
mkdir -p /opt/multirag && cd /opt/multirag
# Place docker-compose-standalone.yml in the current directory.
MULTIRAG_IMAGE=datav/multirag:latest \
MULTIRAG_CONFIG_FILE=/data/multirag/service_conf.yaml \
MULTIRAG_MODEL_DIR=/data/multirag/models \
docker compose -f docker-compose-standalone.yml up -d
```

The standalone Compose file mounts models read-only. Upload complete model
directories before startup; otherwise Hugging Face fallback downloads cannot
write to the mount. `MULTIRAG_CONFIG_FILE` may point to any host filename,
including an existing `service_config.yaml`; Compose mounts it read-only at the
application's fixed `/multirag/configs/service_conf.yaml` path and sets
`SKIP_CONFIG_GENERATE=1` so the entrypoint cannot overwrite it. The optional
[`docker/.env.standalone.example`](./docker/.env.standalone.example) is a
starting template for launch parameters.

## 📦 Docker Profiles

### Default baseline services

- `postgres`
- `redis`
- `minio`

### Optional profiles

| Profile | Description |
|---|---|
| `cpu` | MultiRAG CPU main service |
| `gpu` | MultiRAG GPU main service |
| `milvus` | Milvus vector DB |
| `elasticsearch` | Elasticsearch |
| `opensearch` | OpenSearch |
| `infinity` | Infinity vector DB |
| `oceanbase` | OceanBase vector DB |
| `tei-cpu` | TEI CPU service |
| `tei-gpu` | TEI GPU service |
| `sandbox` | Sandbox executor |
| `kibana` | Kibana |

### Common combinations

| Command | Scenario |
|---|---|
| `docker compose --profile cpu up -d` | Minimal setup |
| `docker compose --profile cpu --profile milvus up -d` | Typical RAG deployment |
| `docker compose --profile cpu --profile infinity up -d` | Infinity-based retrieval |
| `docker compose --profile cpu --profile oceanbase up -d` | OceanBase-based setup |
| `docker compose --profile gpu --profile milvus --profile tei-gpu up -d` | GPU + Milvus + TEI |

## 🔧 Docker Image Build

### Build the dependency resource image

The application image no longer relies on the floating
`infiniflow/ragflow_deps:latest` image. Download build resources such as Tika,
NLTK, Chrome, DeepDoc, and uv 0.11.27 (excluding runtime embedding/rerank
models), then build the local dependency resource image:

```bash
HF_ENDPOINT=https://hf-mirror.com uv run --script download_deps.py --china-mirrors
docker build --platform linux/amd64 -f Dockerfile.deps \
  -t multirag_deps:uv0.11.27-tika3.3.0-build-only .
```

Teams and CI can push this image to an internal registry and select it with
`MULTIRAG_DEPS_IMAGE` when building the application image.

### Build the application image

```bash
NEED_MIRROR=1 ./scripts/build_docker_image.sh datav/multirag:latest
```

The script downloads build resources, builds the build-only dependency image,
builds the application image, and verifies both uv and an empty
`/root/.ragdatav`. The equivalent manual application build is:

```bash
docker build --platform linux/amd64 --build-arg NEED_MIRROR=1 \
  --build-arg MULTIRAG_DEPS_IMAGE=multirag_deps:uv0.11.27-tika3.3.0-build-only \
  -f Dockerfile -t datav/multirag:latest .
```

To use a dependency image from an internal registry:

```bash
docker build --platform linux/amd64 \
  --build-arg MULTIRAG_DEPS_IMAGE=registry.example.com/multirag_deps:uv0.11.27-tika3.3.0-build-only \
  -f Dockerfile -t multirag:latest .
```

### Rebuild and restart

```bash
cd docker
docker compose --profile cpu down
docker compose --profile cpu up -d
```

## ⚙️ Configuration

### Configuration layout

```text
configs/
├── service_conf.yaml
├── llm_factories.json
└── alembic/

docker/
├── .env
├── docker-compose.yml
├── docker-compose-base.yml
├── docker-compose-macos.yml
└── service_conf.yaml.template
```

### Main config files

- [`docker/.env`](./docker/.env) Docker variables (ports, passwords, service switches)
- [`configs/service_conf.yaml`](./configs/service_conf.yaml) backend runtime settings (DB, LLM, storage, retrieval)
- [`docker/docker-compose.yml`](./docker/docker-compose.yml) main compose file
- [`docker/docker-compose-base.yml`](./docker/docker-compose-base.yml) infrastructure compose file

### Common ports

| Service | Default Port | Env Variable |
|---|---|---|
| Nginx HTTP | 80 | `SVR_WEB_HTTP_PORT` |
| Nginx HTTPS | 443 | `SVR_WEB_HTTPS_PORT` |
| Main API | 8123 | `SVR_HTTP_PORT` |
| Admin API | 8130 | `ADMIN_SVR_HTTP_PORT` |
| PostgreSQL | 5432 | `POSTGRES_PORT` |
| Redis | 6379 | `REDIS_PORT` |
| MinIO | 9000/9001 | `MINIO_PORT` / `MINIO_CONSOLE_PORT` |
| Milvus | 19530 | `MILVUS_PORT` |
| Elasticsearch | 9200 | `ES_PORT` |

### Key config example

```yaml
# configs/service_conf.yaml
user_default_llm:
  factory: "OpenAI"
  api_key: "your-api-key"
  base_url: "https://api.openai.com/v1"
  default_models:
    chat_model: "gpt-4o-mini"
    embedding_model: "text-embedding-3-large"

retrieval:
  doc_engine: "milvus"  # milvus / infinity / elasticsearch / opensearch
```

### Backend switching checklist

- ensure matching Docker profile and config are aligned
- verify Alembic migrations after upgrades
- run retrieval/write regression checks for OceanBase, SeekDB, and Infinity

## 🔨 Development from Source

### 1. Install dependencies

```bash
uv sync --python 3.12 --all-extras
uv run --script download_deps.py
```

### 2. Start infrastructure services

```bash
docker compose -f docker/docker-compose-base.yml up -d
```

Enable extra backends as needed:

```bash
docker compose -f docker/docker-compose-base.yml --profile milvus up -d
docker compose -f docker/docker-compose-base.yml --profile infinity up -d
docker compose -f docker/docker-compose-base.yml --profile oceanbase up -d
```

### 3. Start the backend service

```bash
export PYTHONPATH=$(pwd)
uv run python -m api.multirag_server
```

### 4. Run tests

```bash
uv run pytest
```

Single-test example:

```bash
uv run pytest tests/unit/test_image_filter.py
```

### 5. Run lint and formatting

```bash
ruff check
ruff format
```

### 6. Typical dev entry points

- service entry: [`api/multirag_server.py`](./api/multirag_server.py)
- API routes: [`api/apps`](./api/apps)
- core logic: [`core`](./core)
- document parsing: [`deepdoc`](./deepdoc)
- agents: [`agent`](./agent)
- MCP: [`mcp`](./mcp)
- workflows: [`workflow`](./workflow) / [`workflow_v2`](./workflow_v2)

## 📚 API and Documentation

### API domains

Main API domains include:

- knowledge base, dataset, document, and chunk management
- chat, conversation, retrieval, and OpenAI-compatible APIs
- LLM providers, model settings, and system configuration
- agent, canvas, workflow, and workflow v2 execution
- connectors, MCP server, admin, user, and tenant management

### Common endpoint examples

| Path | Method | Description |
|---|---|---|
| `/api/v1/chat/completions` | POST | Chat endpoint |
| `/api/v1/datasets` | GET/POST | Knowledge base management |
| `/api/v1/documents` | POST/DELETE | Document management |
| `/api/v1/chunks` | GET | Chunk query |
| `/api/v1/workflows` | GET/POST | Workflow management |
| `/api/v1/agents` | POST | Agent execution |
| `/api/v1/mcp/*` | GET/POST | MCP-related APIs |

OpenAPI docs are available at `/docs` after service startup.

### Documentation index

- Getting started: [`docs/get_started.md`](./docs/get_started.md)
- Deployment guide: [`docs/DEPLOYMENT_GUIDE.md`](./docs/DEPLOYMENT_GUIDE.md)
- Architecture: [`docs/architecture.md`](./docs/architecture.md)
- HTTP API reference: [`docs/references/http_api_reference.md`](./docs/references/http_api_reference.md)
- Python API reference: [`docs/references/python_api_reference.md`](./docs/references/python_api_reference.md)
- Supported models: [`docs/references/supported_models.md`](./docs/references/supported_models.md)
- Docker guide: [`docker/README.md`](./docker/README.md)

## 🛠️ Typical Use Cases

- enterprise internal QA and intelligent search
- multi-source ingestion and unified retrieval
- Agent backends for business systems
- document understanding and structured extraction
- OpenAI-compatible platform services

## 🤝 Contributing

Issues and pull requests are welcome.

Before contributing, review:

- [`docs/architecture.md`](./docs/architecture.md)
- [`docker/README.md`](./docker/README.md)
- [`AGENTS.md`](./AGENTS.md)

Before submitting changes, at least run:

```bash
uv run pytest
ruff check
ruff format
```

## 📄 License

This project is licensed under Apache-2.0.
