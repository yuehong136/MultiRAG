<div align="center">
<h1>MultiRAG - Enterprise-Grade RAG Intelligent Backend Engine</h1>
</div>

<p align="center">
  <a href="./README.md"><img alt="简体中文版自述文件" src="https://img.shields.io/badge/简体中文-DBEDFA"></a>
  <a href="./README_EN.md"><img alt="README in English" src="https://img.shields.io/badge/English-DFE0E5"></a>
</p>

<p align="center">
    <a href="https://github.com/yourusername/multirag/releases/latest">
        <img src="https://img.shields.io/github/v/release/yourusername/multirag?color=blue&label=Latest%20Release" alt="Latest Release">
    </a>
    <a href="https://github.com/yourusername/multirag/blob/main/LICENSE">
        <img height="21" src="https://img.shields.io/badge/License-Apache--2.0-ffffff?labelColor=d4eaf7&color=2e6cc4" alt="license">
    </a>
    <a href="https://www.python.org/downloads/release/python-3120/">
        <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
    </a>
    <a href="pyproject.toml">
        <img src="https://img.shields.io/badge/version-0.4.1-green.svg" alt="Version">
    </a>
</p>

<h4 align="center">
  <a href="#-documentation">Documentation</a> |
  <a href="#-roadmap">Roadmap</a> |
  <a href="#-community">Community</a> |
  <a href="#-api-documentation">API Docs</a>
</h4>

#

<details open>
<summary><b>Table of Contents</b></summary>

- [What is MultiRAG?](#-what-is-multirag)
- [Latest Updates](#-latest-updates)
- [Core Features](#-core-features)
- [System Architecture](#-system-architecture)
- [Quick Start](#-quick-start)
- [Configuration](#-configuration)
- [Docker Image Build](#-docker-image-build)
- [Development from Source](#-development-from-source)
- [API Documentation](#-api-documentation)
- [Documentation](#-documentation)
- [Roadmap](#-roadmap)
- [Community](#-community)
- [Contributing](#-contributing)

</details>

## 💡 What is MultiRAG?

MultiRAG is an intelligent RAG (Retrieval-Augmented Generation) backend engine designed for enterprise applications. The project integrates RAG retrieval enhancement, GraphRAG knowledge graphs, Workflow orchestration, and Agent frameworks to provide a complete AI application backend solution for enterprises.

### Project Highlights

- 🔥 **Modular Architecture** - Independent core modules supporting flexible combinations
- ⚡ **High Performance** - Distributed task execution and concurrent processing
- 🔌 **Multi-Engine Support** - Compatible with Milvus, Elasticsearch, Infinity, and other vector databases
- 🌐 **Multi-Model Adaptation** - Supports OpenAI, ZHIPU-AI, Tongyi-Qianwen, and other major LLM providers
- 📊 **Enterprise Features** - Complete permission management, monitoring, logging, and error handling

## 🔥 Latest Updates

- 2025-10-29 Support for MCP (Model Context Protocol) standardized interface
- 2025-10-20 Added Admin service containerization support with Docker deployment
- 2025-10-15 Added sensitive word library management system with Milvus vector search
- 2025-10-10 Optimized document parsing performance with DOCX table intelligent analysis
- 2025-10-05 Upgraded FastAPI framework with improved health check mechanism
- 2025-09-28 Support for multiple vector database switching (Milvus/Elasticsearch/Infinity)
- 2025-09-20 Added GraphRAG knowledge graph enhanced retrieval
- 2025-09-15 Integrated Workflow engine with visual orchestration support

## 🎉 Stay Updated

⭐️ Star our repository to stay informed about exciting new features and improvements! Get instant notifications for new releases! 🌟

## 🌟 Core Features

### 🍭 **RAG Retrieval Enhancement System**
- **Multi-modal Document Processing** - Support for PDF, Word, PPT, Excel, images, and more
- **Intelligent Document Parsing** - Based on DeepDoc deep document understanding technology
- **Vector Search Optimization** - Multiple embedding models and reranking algorithms
- **Knowledge Base Management** - Unified document upload, index building, and retrieval optimization

### 🌱 **GraphRAG Knowledge Graph**
- **Entity Relationship Extraction** - Automatic knowledge graph construction
- **Graph Reasoning Query** - Complex relationship inference support
- **Entity Disambiguation & Linking** - Intelligent entity recognition and association

### 🍔 **Workflow Engine**
- **Visual Orchestration** - Complex business process design support
- **Component Development** - Rich built-in component library
- **Parallel Execution** - Workflow parallelism and conditional branching
- **API Integration** - Flexible third-party system integration

### 🛀 **Agent Framework**
- **Tool Invocation** - Python code execution, external API calls
- **Role Definition** - Configurable Agent roles and capabilities
- **Memory Management** - Session history and knowledge memory mechanisms
- **Multi-Agent Collaboration** - Multi-agent cooperative work support

### 🎨 **MCP Protocol Support**
- **Standardized Interface** - Based on Model Context Protocol
- **Multi-Mode Deployment** - Self-host and host mode support
- **Secure Authentication** - Complete API Key authentication mechanism

## 🔎 System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MultiRAG System Architecture                  │
├─────────────────────────────────────────────────────────────────┤
│  API Layer          │    Core Engine Layer   │   Data Storage    │
│                     │                        │                   │
│  ┌───────────────┐  │  ┌──────────────────┐  │  ┌─────────────┐  │
│  │ FastAPI       │  │  │ RAG Engine       │  │  │ Vector DB   │  │
│  │ RESTful API   │  │  │ GraphRAG Engine  │  │  │ • Milvus    │  │
│  │ MCP Server    │  │  │ Workflow Engine  │  │  │ • ES        │  │
│  │ SSE Stream    │  │  │ Agent Framework  │  │  │ • Infinity  │  │
│  └───────────────┘  │  │ DeepDoc Parser   │  │  │ • OpenSearch│  │
│                     │  │ Task Executor    │  │  └─────────────┘  │
│  ┌───────────────┐  │  └──────────────────┘  │                   │
│  │ Auth & ACL    │  │                        │  ┌─────────────┐  │
│  │ Rate Limit    │  │  ┌──────────────────┐  │  │ RDBMS       │  │
│  │ Monitoring    │  │  │ LLM Adapters     │  │  │ • PostgreSQL│  │
│  └───────────────┘  │  │ • OpenAI         │  │  │ • MySQL     │  │
│                     │  │ • ZHIPU-AI       │  │  └─────────────┘  │
│                     │  │ • Tongyi-Qianwen │  │                   │
│                     │  │ • Ollama         │  │  ┌─────────────┐  │
│                     │  └──────────────────┘  │  │ Object Store│  │
│                     │                        │  │ • MinIO     │  │
│                     │  ┌──────────────────┐  │  │ • AWS S3    │  │
│                     │  │ Message Queue    │  │  │ • Azure Blob│  │
│                     │  │ Redis Streams    │  │  │ • Alibaba OSS│ │
│                     │  └──────────────────┘  │  └─────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## 🎬 Quick Start

### 📝 Prerequisites

- CPU >= 4 cores
- RAM >= 16 GB (32GB recommended for better performance)
- Disk >= 50 GB
- Docker >= 24.0.0 & Docker Compose >= v2.26.1
- Python >= 3.12 (for source deployment)

> [!TIP]
> If Docker is not installed on your local machine (Windows, Mac, or Linux), see [Install Docker Engine](https://docs.docker.com/engine/install/).

### 🚀 Start Services

1. Ensure `vm.max_map_count` >= 262144 (Linux systems):

   > Check the `vm.max_map_count` value:
   >
   > ```bash
   > $ sysctl vm.max_map_count
   > ```
   >
   > If the value is less than 262144, reset it to at least 262144:
   >
   > ```bash
   > # In this example, we set it to 262144:
   > $ sudo sysctl -w vm.max_map_count=262144
   > ```
   >
   > This change will reset after a system reboot. To make it permanent, add or update the `vm.max_map_count` value in **/etc/sysctl.conf**:
   >
   > ```bash
   > vm.max_map_count=262144
   > ```

2. Clone the repository:

   ```bash
   $ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
   $ cd multrag
   ```

3. Start services using Docker Compose:

   ```bash
   $ cd docker

   # Minimal deployment (auto-starts postgres + redis + minio + multirag)
   $ docker compose --profile cpu up -d

   # With Milvus vector database
   $ docker compose --profile cpu --profile milvus up -d

   # With Elasticsearch search engine
   $ docker compose --profile cpu --profile elasticsearch up -d

   # With TEI Embedding service
   $ docker compose --profile cpu --profile tei-cpu up -d

   # Full deployment (Milvus + TEI)
   $ docker compose --profile cpu --profile milvus --profile tei-cpu up -d

   # GPU mode
   $ docker compose --profile gpu --profile milvus --profile tei-gpu up -d
   ```

4. Check server status after startup:

   ```bash
   $ docker logs -f multirag
   ```

   _The following output confirms successful system startup:_

   ```bash
                __  ___      ____  _ ____  ___   ______
               /  |/  /_  __/ / /_(_) __ \/   | / ____/
              / /|_/ / / / / / __/ / /_/ / /| |/ / __
             / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /
            /_/  /_/\__,_/_/\__/_/_/ |_/_/  |_\____/

   * Running on all addresses (0.0.0.0)
   ```

   > If you skip this confirmation step and access MultiRAG directly, your browser may show a `network abnormal` error because MultiRAG may not be fully initialized yet.

5. In your web browser, enter the server's IP address to access MultiRAG.
   > With default settings, simply enter `http://IP_OF_YOUR_MACHINE:8123` (default HTTP port is `8123`).

6. In [service_conf.yaml](./configs/service_conf.yaml), select the desired LLM factory and update the `API_KEY` field with your API key.

   _Get started!_

### 🍎 macOS (Apple Silicon) Deployment

Since some services (like TEI) only have x86_64 versions, Apple Silicon Macs need to use a dedicated configuration file:

```bash
$ cd docker

# Minimal deployment (via Rosetta 2 x86_64 emulation)
$ docker compose -f docker-compose-macos.yml --profile cpu up -d

# With Milvus vector database
$ docker compose -f docker-compose-macos.yml --profile cpu --profile milvus up -d

# With TEI Embedding service
$ docker compose -f docker-compose-macos.yml --profile cpu --profile tei-cpu up -d
```

**Prerequisites**:
- Enable **"Use Rosetta for x86_64/amd64 emulation on Apple Silicon"** in Docker Desktop settings
- Or use OrbStack and select x86-64 (emulated) architecture

**Performance Notes**:
- Running via Rosetta 2 emulation, performance is approximately 50-70% of native
- Suitable for development and testing; production environments should use Linux x86_64 servers

### 📦 Service Profiles

MultiRAG uses Docker Compose profiles to start services on demand:

#### Required Services (auto-start, no profile needed)

| Service | Container Name | Port | Description |
|---------|---------------|------|-------------|
| postgres | multirag-postgres | 5432 | PostgreSQL database |
| redis | multirag-redis | 6379 | Redis cache service |
| minio | multirag-minio | 9000, 9001 | MinIO object storage |

#### Optional Services (require profile specification)

| Profile | Service | Description |
|---------|---------|-------------|
| `cpu` | multirag-cpu | MultiRAG main service (CPU version) |
| `gpu` | multirag-gpu | MultiRAG main service (GPU version) |
| `elasticsearch` | es01 | Elasticsearch search engine |
| `opensearch` | opensearch01 | OpenSearch search engine |
| `milvus` | milvus-standalone, milvus-etcd, milvus-minio | Milvus vector database cluster |
| `infinity` | infinity | Infinity vector database |
| `oceanbase` | oceanbase | OceanBase vector database |
| `tei-cpu` | tei-cpu | TEI Embedding service (CPU version) |
| `tei-gpu` | tei-gpu | TEI Embedding service (GPU version) |
| `sandbox` | sandbox-executor-manager | Sandbox executor |
| `kibana` | kibana | Elasticsearch visualization tool |

#### Container Count Reference

| Command | Containers Started |
|---------|-------------------|
| `--profile cpu` | postgres, redis, minio, multirag-cpu (4) |
| `--profile cpu --profile milvus` | above + milvus-etcd, milvus-minio, milvus-standalone (7) |
| `--profile cpu --profile milvus --profile tei-cpu` | above + tei-cpu (8) |
| `--profile cpu --profile elasticsearch` | postgres, redis, minio, multirag-cpu, es01 (5) |

## 🔧 Configuration

When configuring the system, you need to manage the following files:

### Configuration File Structure

```
docker/
├── .env                        # Environment variables (ports, passwords, etc.)
├── docker-compose.yml          # Main config file (includes base file)
├── docker-compose-base.yml     # Infrastructure services (PostgreSQL, Redis, MinIO, etc.)
├── docker-compose-macos.yml    # macOS-specific config
├── service_conf.yaml.template  # Service configuration template
└── nginx/                      # Nginx reverse proxy config
    ├── nginx.conf
    ├── proxy.conf
    └── multirag.conf
```

### Main Configuration Files

- **[.env](./docker/.env)**: Docker environment variables including port mappings, database passwords, service switches
- **[service_conf.yaml](./configs/service_conf.yaml)**: Backend service configuration including database connections, LLM settings, storage settings
- **[docker-compose.yml](./docker/docker-compose.yml)**: Main Docker Compose config, includes infrastructure services via `include`
- **[docker-compose-base.yml](./docker/docker-compose-base.yml)**: Infrastructure service definitions (PostgreSQL, Redis, MinIO, Milvus, etc.)

> [!TIP]
> For detailed Docker configuration documentation, see [docker/README.md](./docker/README.md)

### Port Configuration

| Service | Default Port | Environment Variable |
|---------|-------------|---------------------|
| Nginx HTTP | 80 | `SVR_WEB_HTTP_PORT` |
| Nginx HTTPS | 443 | `SVR_WEB_HTTPS_PORT` |
| Main API | 8123 | `SVR_HTTP_PORT` |
| Admin API | 8130 | `ADMIN_SVR_HTTP_PORT` |
| PostgreSQL | 5432 | `POSTGRES_PORT` |
| Redis | 6379 | `REDIS_PORT` |
| MinIO | 9000, 9001 | `MINIO_PORT`, `MINIO_CONSOLE_PORT` |
| Milvus | 19530 | `MILVUS_PORT` |
| Elasticsearch | 9200 | `ES_PORT` |

Configuration updates require restarting containers:

```bash
$ cd docker
$ docker compose --profile cpu down
$ docker compose --profile cpu up -d
```

### Switching Vector Database Engines

MultiRAG supports multiple vector databases, enabled via profiles:

#### Using Milvus (Recommended)

```bash
$ docker compose --profile cpu --profile milvus up -d
```

#### Using Elasticsearch

```bash
$ docker compose --profile cpu --profile elasticsearch up -d
```

#### Using Infinity

```bash
$ docker compose --profile cpu --profile infinity up -d
```

> [!WARNING]
> When switching vector databases, use `docker compose down -v` to clear existing data. The `-v` flag removes data volumes.

### Main Configuration Options

#### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DOC_ENGINE` | Document engine type (milvus/elasticsearch/infinity) | `milvus` |
| `STORAGE_IMPL` | Storage implementation (MINIO/S3/AZURE/OSS) | `MINIO` |
| `LIGHTEN` | Lightweight mode (without embedding models) | `0` |
| `MAX_CONTENT_LENGTH` | Maximum document size | `1GB` |
| `REGISTER_ENABLED` | User registration switch | `1` |

#### Vector Database Configuration

MultiRAG supports multiple vector databases:

```yaml
# Milvus configuration (recommended)
milvus:
  host: localhost
  port: 19530
  user: root
  password: Milvus

# Elasticsearch configuration
elasticsearch:
  hosts: ["http://localhost:9200"]

# Infinity configuration
infinity:
  uri: "infinity:23817"
```

#### LLM Model Configuration

Supports major LLM providers:

```yaml
user_default_llm:
  factory: "ZHIPU-AI"  # OpenAI, ZHIPU-AI, Tongyi-Qianwen, Ollama, etc.
  api_key: "your-api-key"
  base_url: "https://open.bigmodel.cn/api/paas/v4/"
  default_models:
    chat_model: "glm-4-plus"
    embedding_model: "embedding-2"
    rerank_model: "bge-reranker-v2-m3"
```

## 🔧 Docker Image Build

### Build Lightweight Image (without embedding models)

This image is approximately 2 GB and relies on external LLM and embedding services (like TEI).

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ docker build --platform linux/amd64 --build-arg LIGHTEN=1 -f Dockerfile -t multirag:slim .
```

### Build Full Image (with embedding models)

This image is approximately 9 GB. It includes embedding models and only requires external LLM services.

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ docker build --platform linux/amd64 -f Dockerfile -t multirag:latest .
```

### Rebuild and Update Service

```bash
# Build image
$ docker build -t multirag:latest .

# Restart service
$ cd docker
$ docker compose --profile cpu down
$ docker compose --profile cpu up -d
```

## 🔨 Development from Source

### 1. Install Development Tools

```bash
# Install uv and pre-commit (skip if already installed)
$ pipx install uv pre-commit
```

### 2. Clone Source Code and Install Dependencies

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ uv sync --python 3.12 --all-extras  # Install MultiRAG Python dependencies
$ uv run download_deps.py
$ pre-commit install
```

### 3. Start Infrastructure Services

Use Docker Compose to start dependency services (PostgreSQL, Redis, MinIO, etc.):

```bash
# Start only infrastructure services (not the multirag main service)
$ cd docker
$ docker compose up -d postgres redis minio

# If Milvus vector database is needed
$ docker compose --profile milvus up -d
```

Add the following lines to `/etc/hosts` to resolve service hostnames:

```
127.0.0.1       postgres redis minio milvus es01 infinity
```

### 4. Configure Environment Variables

```bash
# HuggingFace mirror site (recommended for users in China)
$ export HF_ENDPOINT=https://hf-mirror.com
```

### 5. Install System Dependencies (Optional)

If your operating system doesn't have jemalloc, install it as follows:

```bash
# Ubuntu/Debian
$ sudo apt-get install libjemalloc-dev
# CentOS/RHEL
$ sudo yum install jemalloc
# OpenSUSE
$ sudo zypper install jemalloc
# macOS
$ brew install jemalloc
```

### 6. Start Backend Services

```bash
$ source .venv/bin/activate
$ export PYTHONPATH=$(pwd)
$ bash docker/launch_backend_service.sh
```

_The following output confirms successful system startup:_

```bash
             __  ___      ____  _ ____  ___   ______
            /  |/  /_  __/ / /_(_) __ \/   | / ____/
           / /|_/ / / / / / __/ / /_/ / /| |/ / __
          / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /
         /_/  /_/\__,_/_/\__/_/_/ |_/_/  |_\____/

* Running on all addresses (0.0.0.0)
```

### 7. Stop Services

```bash
$ pkill -f "multirag_server.py|task_executor.py"
```

## 📚 API Documentation

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/chat/completions` | POST | Chat conversation interface |
| `/api/v1/datasets` | GET/POST | Knowledge base management |
| `/api/v1/documents` | POST/DELETE | Document management |
| `/api/v1/chunks` | GET | Document chunk query |
| `/api/v1/workflows` | GET/POST | Workflow management |
| `/api/v1/agents` | POST | Agent execution |

### Usage Examples

```python
import requests

# Chat interface
response = requests.post(
    "http://localhost:8000/api/v1/chat/completions",
    headers={"Authorization": "Bearer your-api-key"},
    json={
        "model": "glm-4-plus",
        "messages": [
            {"role": "user", "content": "Hello, please introduce MultiRAG features"}
        ],
        "dataset_ids": ["kb_001"]
    }
)

# Knowledge base creation
response = requests.post(
    "http://localhost:8000/api/v1/datasets",
    headers={"Authorization": "Bearer your-api-key"},
    json={
        "name": "Technical Documentation",
        "description": "Store technical documents",
        "parser": "manual"
    }
)
```

Detailed API documentation: [http://localhost:8000/docs](http://localhost:8000/docs)

## 🛠️ Development Guide

### Project Structure

```
multirag/
├── api/                        # API Service Layer
│   ├── admin/                  # Admin API
│   ├── apps/                   # FastAPI Application Modules
│   │   ├── sdk/               # SDK Interface (OpenAI API compatible)
│   │   ├── auth/              # OAuth/OIDC Authentication
│   │   ├── dataset_app.py     # Knowledge Base Management
│   │   ├── document_app.py    # Document Management
│   │   ├── conversation_app.py # Conversation Management
│   │   ├── workflow_app.py    # Workflow API
│   │   └── ...                # Other Business APIs
│   ├── db/                    # Database Layer
│   │   ├── models/           # SQLAlchemy Models
│   │   └── services/         # Database Services
│   ├── service/               # Business Logic Services
│   ├── middleware/            # Middleware (Auth, Rate Limiting, etc.)
│   └── utils/                 # API Utilities
│
├── core/                       # Core Engine
│   ├── app/                   # Document Parsers (by type)
│   │   ├── naive.py          # General Parser
│   │   ├── paper.py          # Paper Parser
│   │   ├── resume.py         # Resume Parser
│   │   └── ...               # Other Parsers
│   ├── flow/                  # Document Processing Pipeline
│   │   ├── parser/           # Parsers
│   │   ├── splitter/         # Chunkers
│   │   ├── extractor/        # Information Extractors
│   │   └── tokenizer/        # Tokenizers
│   ├── llm/                   # LLM Adapters
│   │   ├── chat_model.py     # Chat Model
│   │   ├── embedding_model.py # Embedding Model
│   │   ├── rerank_model.py   # Rerank Model
│   │   └── ...               # Vendor Adapters
│   ├── nlp/                   # NLP Processing Modules
│   ├── prompts/               # Prompt Templates
│   ├── svr/                   # Server Components
│   │   └── task_executor.py  # Task Executor
│   └── utils/                 # Storage Connectors
│       ├── milvus_conn.py    # Milvus Connection
│       ├── es_conn.py        # Elasticsearch Connection
│       └── ...               # Other Connectors
│
├── agent/                      # Agent Framework
│   ├── component/             # Agent Components
│   │   ├── base.py           # Base Component
│   │   ├── llm.py            # LLM Component
│   │   ├── iteration.py      # Loop Component
│   │   └── ...               # Other Components
│   ├── tools/                 # Tool Collection
│   │   ├── retrieval.py      # Knowledge Base Retrieval
│   │   ├── code_exec.py      # Code Execution
│   │   ├── crawler.py        # Web Crawler
│   │   ├── duckduckgo.py     # DuckDuckGo Search
│   │   └── ...               # Other Tools
│   └── templates/             # Agent Templates
│
├── agentic_reasoning/          # Agentic Reasoning Module
│   └── deep_research.py       # Deep Research
│
├── deepdoc/                    # Deep Document Processing
│   ├── parser/                # Document Parsers
│   │   ├── pdf_parser.py     # PDF Parser
│   │   ├── docx_parser.py    # Word Parser
│   │   └── ...               # Other Formats
│   └── vision/                # Vision Processing
│
├── graphrag/                   # GraphRAG Knowledge Graph
│   ├── entity_extractor.py    # Entity Extraction
│   ├── graph_builder.py       # Graph Building
│   └── graph_search.py        # Graph Search
│
├── workflow/                   # Workflow Engine v1
├── workflow_v2/                # Workflow Engine v2
│   ├── component/             # Workflow Components
│   │   ├── llm_component.py  # LLM Component
│   │   ├── code_component.py # Code Component
│   │   └── ...               # Other Components
│   └── workflow.py            # Workflow Executor
│
├── mcp/                        # MCP Protocol Support
├── plugin/                     # Plugin System
├── sandbox/                    # Sandbox Executor
├── server/                     # Server Modules
├── admin/                      # Admin Service
├── intergrations/              # Third-party Integrations
│
├── common/                     # Common Modules
├── configs/                    # Configuration Files
├── errors/                     # Error Definitions
├── scripts/                    # Script Tools
└── docker/                     # Docker Deployment Config
```

### Core Components

| Component | Location | Description |
|-----------|----------|-------------|
| **TaskExecutor** | `core/svr/task_executor.py` | Distributed task executor for document parsing, vectorization, and async tasks |
| **RAG Engine** | `core/flow/` | Retrieval-augmented generation engine with parsing, chunking, and vectorization pipeline |
| **LLM Adapters** | `core/llm/` | LLM adapters supporting OpenAI, ZHIPU-AI, Tongyi-Qianwen, and more |
| **GraphRAG** | `graphrag/` | Knowledge graph enhanced retrieval with entity extraction and graph reasoning |
| **Workflow Engine** | `workflow_v2/` | Workflow orchestration engine with visual process design |
| **Agent Framework** | `agent/` | Intelligent agent framework with tool invocation and multi-agent collaboration |
| **DeepDoc Parser** | `deepdoc/` | Deep document parser supporting complex document structure recognition |
| **MCP Server** | `mcp/` | Model Context Protocol server |

### Extension Development

#### Adding Custom Document Parser

```python
# core/app/custom.py
from core.app.naive import Naive

class CustomParser(Naive):
    """Custom document parser"""
    
    def __call__(self, filename, binary=None, from_page=0, to_page=100000, **kwargs):
        # Implement custom parsing logic
        sections = []
        # ... parsing logic
        return sections

# Register in FACTORY
FACTORY["custom"] = CustomParser
```

#### Adding Custom Agent Tool

```python
# agent/tools/custom_tool.py
from agent.tools.base import BaseTool

class CustomTool(BaseTool):
    """Custom tool"""
    name = "custom_tool"
    description = "Tool description"
    
    def run(self, query: str, **kwargs):
        # Implement tool logic
        return result
```

#### Adding Custom Workflow Component

```python
# workflow_v2/component/custom_component.py
from workflow_v2.component.base_component import BaseComponent

class CustomComponent(BaseComponent):
    """Custom workflow component"""
    component_type = "custom"
    
    def execute(self, inputs: dict) -> dict:
        # Implement component logic
        return outputs
```

## 🏄 Contributing

We welcome community contributions! Please follow this process:

1. **Fork the project** - Create your branch
2. **Local development** - Develop on a feature branch
3. **Code standards** - Run `pre-commit` checks
4. **Test verification** - Ensure tests pass
5. **Submit PR** - Describe changes in detail

### Commit Convention

```bash
feat: Add user authentication module
fix: Fix document parsing memory leak
docs: Update API documentation
style: Code formatting adjustments
refactor: Refactor RAG retrieval logic
perf: Optimize vector search performance
test: Add unit tests
chore: Update dependency versions
```

### Development Environment

```bash
# Install development tools
uv add --dev pytest black flake8 mypy
pre-commit install

# Run tests
pytest tests/

# Code formatting
black . --line-length 120
```

## 📄 License

This project is open-sourced under the [Apache-2.0](LICENSE) license.

---

<p align="center">
  <strong>MultiRAG - Making Enterprise AI Development Easier</strong><br>
  If this project helps you, please give us a ⭐️ Star!
</p>
