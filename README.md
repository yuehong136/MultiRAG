<div align="center">
<h1>MultiRAG</h1>
<p>企业级 RAG / Agent / Workflow / MCP 智能后端引擎</p>
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
  <a href="#-multirag-是什么">项目介绍</a> |
  <a href="#-版本重点">版本重点</a> |
  <a href="#-核心能力">核心能力</a> |
  <a href="#-快速开始">快速开始</a> |
  <a href="#-源码部署开发">源码开发</a> |
  <a href="#-api-与文档">API与文档</a>
</p>

<details open>
<summary><b>📕 目录</b></summary>

- [MultiRAG 是什么？](#-multirag-是什么)
- [版本重点](#-版本重点)
- [核心能力](#-核心能力)
- [系统架构](#-系统架构)
- [项目结构](#-项目结构)
- [快速开始](#-快速开始)
- [Docker Profiles](#-docker-profiles)
- [Docker 镜像构建](#-docker-镜像构建)
- [配置说明](#-配置说明)
- [源码部署开发](#-源码部署开发)
- [API 与文档](#-api-与文档)
- [适用场景](#-适用场景)
- [贡献指南](#-贡献指南)
- [许可证](#-许可证)

</details>

## 📌 MultiRAG 是什么？

MultiRAG 是一个面向企业场景的 AI 后端项目，围绕 RAG、GraphRAG、Workflow、Agent 与 MCP 构建，覆盖文档解析、数据接入、知识入库、检索问答、工具调用、工作流执行和开放 API 等完整链路。

它适合用来搭建：

- 企业知识库问答与检索增强系统
- 多数据源同步与统一检索中台
- Agent / MCP 驱动的业务智能后端
- 文档理解、表格抽取、图像解析与深度检索服务
- OpenAI 兼容接口的能力层平台

## 🔥 版本重点

当前仓库版本为 `v0.9.9`。相较旧版文档，项目边界已经明显扩展：

- MCP client 与 MCP server 双向能力，支持 FastMCP 3 与 Streamable HTTP
- Agent、Workflow、Workflow v2 持续增强，支持更完整的工具调用和多文件输入
- 连接器能力从文件型数据扩展到企业系统与关系数据库
- 检索与存储后端扩展至 Milvus、Infinity、Elasticsearch、OpenSearch、OceanBase、SeekDB
- OpenAI 兼容接口增强，支持更丰富的引用与 metadata 返回
- DeepDoc 解析链路增强，覆盖 OCR、表格旋转纠正、PDF Vision、PaddleOCR 等能力

## ✨ 核心能力

### 检索与知识增强

- 支持标准 RAG、GraphRAG、SQL 检索、混合召回与重排序
- 支持文档级 metadata 入库、过滤、引用回传和条件检索
- 支持 OpenAI 兼容响应格式，便于接入通用客户端与 SDK
- 支持多种文档切分策略、向量检索策略与阈值控制

### 深度文档理解

- 支持 PDF、DOCX、PPT/PPTX、Excel、图片等文档格式
- 内置 DeepDoc，覆盖 OCR、版面分析、图表和表格处理
- 支持 PaddleOCR、PaddleOCR-VL、PDF Vision、Docling 等解析链路
- 支持父子块切分、页码修复、表格方向检测与纠正

### Agent、Workflow 与 MCP

- Agent 组件体系支持工具调用、记忆、文件输入与多步骤执行
- 同时提供 Workflow 与 Workflow v2 编排能力
- MCP 支持 client/server 双向集成
- 支持结构化工具输出、SSE side-channel 日志与超时控制

### 数据源连接与同步

- 覆盖 GitHub、GitLab、Bitbucket
- 覆盖 Jira、Confluence、Notion、Airtable、Asana、Moodle
- 覆盖 Google Drive、SharePoint、Dropbox、Box、Seafile、WebDAV
- 覆盖 Slack、Teams、Discord、Gmail、IMAP、Zendesk
- 支持关系型数据库连接器（MySQL / PostgreSQL）

### 模型与平台适配

- 支持 OpenAI、Anthropic、Gemini、Tongyi、DashScope、Volcengine、Groq、Ollama、Vertex AI 等体系
- 支持 Chat、Embedding、Rerank、Vision、TTS、Sequence2Text
- 支持模型路由、工厂配置、启停控制和 OpenAI 兼容访问

### 工程化与运维

- 基于 FastAPI，适合服务化部署与二次开发
- 提供 Docker Compose 部署、源码开发、Admin API、CLI 与测试体系
- 包含任务执行器、健康检查、系统设置、监控日志和权限能力

## 🔎 系统架构

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

## 🗂️ 项目结构

当前仓库已从单一 RAG 服务演进为综合 AI 后端工程，核心目录如下：

```text
multirag/
├── api/             # FastAPI 入口、路由、DB模型、服务层
├── core/            # RAG/检索/LLM/解析/任务执行核心逻辑
├── deepdoc/         # 文档解析、OCR、版面分析
├── agent/           # Agent 组件、模板、工具、插件
├── workflow/        # Workflow v1
├── workflow_v2/     # Workflow v2
├── common/          # 公共工具、连接器、存储适配
├── mcp/             # MCP client/server
├── memory/          # Memory 相关能力
├── admin/           # Admin server 与 CLI
├── docker/          # Docker Compose、Nginx、部署配置
├── docs/            # 文档与 API 参考
├── tests/           # 单元与集成测试
└── tools/           # Firecrawl、chatgpt-on-wechat 等集成
```

### 主要模块说明

#### API 层

- [`api/apps`](./api/apps) 下包含知识库、文档、会话、LLM、连接器、MCP、Workflow、系统配置、Admin 等接口
- 支持 REST API、SSE 流式输出、OpenAI 兼容接口

#### 核心引擎层

- [`core/llm`](./core/llm) 负责模型适配、Embedding、Rerank、Vision、TTS
- [`core/nlp`](./core/nlp) 负责检索、查询处理、搜索与算法
- [`core/svr`](./core/svr) 负责任务执行、数据同步与后台作业

#### 解析与接入层

- [`deepdoc`](./deepdoc) 负责复杂文档与图像理解
- [`common/data_source`](./common/data_source) 提供企业级连接器与同步逻辑
- [`common/doc_store`](./common/doc_store) 提供向量与检索后端适配

## 🚀 快速开始

### 环境要求

- CPU `>= 4 cores`
- RAM `>= 16 GB`（推荐 32 GB）
- Docker `>= 24`
- Docker Compose `>= v2.26`
- Python `>= 3.12`（源码部署时）

### Docker 启动（推荐）

1. Linux 环境先确认 `vm.max_map_count`（尤其启用 ES/Milvus 时）：

```bash
sysctl vm.max_map_count
sudo sysctl -w vm.max_map_count=262144
```

2. 克隆仓库：

```bash
git clone git@github.com:yuehong136/MultiRAG.git
cd MultiRAG
```

3. 启动服务：

```bash
cd docker

# 最小化部署（postgres + redis + minio + multirag）
docker compose --profile cpu up -d

# 加上 Milvus
docker compose --profile cpu --profile milvus up -d

# 加上 Elasticsearch
docker compose --profile cpu --profile elasticsearch up -d

# 加上 Infinity
docker compose --profile cpu --profile infinity up -d

# 加上 OceanBase
docker compose --profile cpu --profile oceanbase up -d

# 加上 TEI
docker compose --profile cpu --profile milvus --profile tei-cpu up -d

# GPU 模式
docker compose --profile gpu --profile milvus --profile tei-gpu up -d
```

4. 查看日志确认启动完成：

```bash
docker logs -f multirag
```

   _以下输出确认系统成功启动：_

   ```bash
                __  ___      ____  _ ____  ___   ______
               /  |/  /_  __/ / /_(_) __ \/   | / ____/
              / /|_/ / / / / / __/ / /_/ / /| |/ / __   v0.9.9
             / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /
            /_/  /_/\__,_/_/\__/_/_/ |_/_/  |_\____/

INFO:     Uvicorn running on http://0.0.0.0:8123 (Press CTRL+C to quit)
   ```
5. 配置模型和存储：

- 编辑 [`configs/service_conf.yaml`](./configs/service_conf.yaml)
- 或基于 [`docker/service_conf.yaml.template`](./docker/service_conf.yaml.template) 生成配置

6. 默认接口地址：`http://IP_OF_YOUR_MACHINE:8123`

### macOS (Apple Silicon)

```bash
cd docker
docker compose -f docker-compose-macos.yml --profile cpu up -d
```

前提建议：

- Docker Desktop 开启 Rosetta x86_64 仿真
- 或使用 OrbStack 的 x86_64 仿真模式

更多组合见 [`docker/README.md`](./docker/README.md)。

### 外部模型目录与仅镜像部署

MultiRAG 镜像默认不再包含 embedding/rerank 运行时模型。宿主机目录可通过
`MULTIRAG_MODEL_DIR` 任意调整，容器内目标始终是 `/root/.ragdatav`：

```bash
mkdir -p /data/multirag/models
MULTIRAG_MODEL_DIR=/data/multirag/models \
  docker compose -f docker/docker-compose.yml --profile cpu --profile milvus up -d
```

挂载目录按模型 basename 平铺，只需放置实际使用的本地模型：

```text
/data/multirag/models/
├── bge-large-zh-v1.5/
├── bge-reranker-v2-m3/
├── bce-embedding-base_v1/
└── bce-reranker-base_v1/
```

需要准备一份可直接挂载的模型目录时，可单独运行：

```bash
HF_ENDPOINT=https://hf-mirror.com uv run --script download_deps.py \
  --runtime-models-only --runtime-model-dir /data/multirag/models
```

如果服务器上只有 `datav/multirag:latest` 镜像，没有源码仓库，可单独复制
[`docker-compose-standalone.yml`](./docker/docker-compose-standalone.yml) 后启动。该文件不挂载
仓库中的 `configs/`、`entrypoint.sh` 或源码：

```bash
mkdir -p /opt/multirag && cd /opt/multirag
# 将 docker-compose-standalone.yml 放到当前目录
MULTIRAG_IMAGE=datav/multirag:latest \
MULTIRAG_CONFIG_FILE=/data/multirag/service_conf.yaml \
MULTIRAG_MODEL_DIR=/data/multirag/models \
docker compose -f docker-compose-standalone.yml up -d
```

Standalone Compose 默认以只读方式挂载模型。请在启动前上传完整模型；否则代码的
Hugging Face 回退下载也无法写入该目录。`MULTIRAG_CONFIG_FILE` 可以指向任意宿主机
文件名（包括你现有的 `service_config.yaml`），Compose 会将它只读挂载到应用固定读取的
`/multirag/configs/service_conf.yaml`，并设置 `SKIP_CONFIG_GENERATE=1` 防止入口脚本覆盖。可选的
[`docker/.env.standalone.example`](./docker/.env.standalone.example) 可作为启动参数模板。

## 📦 Docker Profiles

### 默认基础服务

- `postgres`
- `redis`
- `minio`

### 可选 profiles

| Profile | 说明 |
|---|---|
| `cpu` | MultiRAG CPU 主服务 |
| `gpu` | MultiRAG GPU 主服务 |
| `milvus` | Milvus 向量数据库 |
| `elasticsearch` | Elasticsearch 搜索引擎 |
| `opensearch` | OpenSearch 搜索引擎 |
| `infinity` | Infinity 向量数据库 |
| `oceanbase` | OceanBase 向量数据库 |
| `tei-cpu` | TEI CPU 服务 |
| `tei-gpu` | TEI GPU 服务 |
| `sandbox` | 沙箱执行器 |
| `kibana` | Kibana |

### 常见组合

| 命令 | 场景 |
|---|---|
| `docker compose --profile cpu up -d` | 最小可用环境 |
| `docker compose --profile cpu --profile milvus up -d` | 常见 RAG 部署 |
| `docker compose --profile cpu --profile infinity up -d` | Infinity 检索部署 |
| `docker compose --profile cpu --profile oceanbase up -d` | OceanBase 部署 |
| `docker compose --profile gpu --profile milvus --profile tei-gpu up -d` | GPU + Milvus + TEI |

## 🔧 Docker 镜像构建

### 构建依赖资源镜像

主镜像不再依赖浮动的 `infiniflow/ragflow_deps:latest`。先下载 Tika、NLTK、Chrome、
DeepDoc 和 uv 0.11.27 等构建资源（不包含运行时 embedding/rerank 模型），再构建
本地依赖镜像：

```bash
HF_ENDPOINT=https://hf-mirror.com uv run --script download_deps.py --china-mirrors
docker build --platform linux/amd64 -f Dockerfile.deps \
  -t multirag_deps:uv0.11.27-tika3.2.3-build-only .
```

团队或 CI 可将该镜像推送到内部仓库，并在构建主镜像时通过
`MULTIRAG_DEPS_IMAGE` 指定完整镜像引用。

### 一键构建主镜像

```bash
NEED_MIRROR=1 ./scripts/build_docker_image.sh datav/multirag:latest
```

该脚本会按顺序下载构建资源、构建 build-only 依赖镜像、构建主镜像，并验证
uv 版本与主镜像中的 `/root/.ragdatav` 为空。手工构建主镜像的等价命令为：

```bash
docker build --platform linux/amd64 --build-arg NEED_MIRROR=1 \
  --build-arg MULTIRAG_DEPS_IMAGE=multirag_deps:uv0.11.27-tika3.2.3-build-only \
  -f Dockerfile -t datav/multirag:latest .
```

使用内部依赖镜像：

```bash
docker build --platform linux/amd64 \
  --build-arg MULTIRAG_DEPS_IMAGE=registry.example.com/multirag_deps:uv0.11.27-tika3.2.3-build-only \
  -f Dockerfile -t multirag:latest .
```

### 重建并重启

```bash
cd docker
docker compose --profile cpu down
docker compose --profile cpu up -d
```

## ⚙️ 配置说明

### 配置文件结构

```text
configs/
├── service_conf.yaml          # 主运行配置
├── llm_factories.json         # 模型工厂与默认模型
└── alembic/                   # DB 迁移

docker/
├── .env                       # Docker 环境变量
├── docker-compose.yml
├── docker-compose-base.yml
├── docker-compose-macos.yml
└── service_conf.yaml.template
```

### 主要配置文件

- [`docker/.env`](./docker/.env) Docker 环境变量（端口、密码、服务开关）
- [`configs/service_conf.yaml`](./configs/service_conf.yaml) 后端服务配置（数据库、LLM、存储、检索）
- [`docker/docker-compose.yml`](./docker/docker-compose.yml) 主 Compose 配置
- [`docker/docker-compose-base.yml`](./docker/docker-compose-base.yml) 基础设施配置

### 常用端口

| 服务 | 默认端口 | 环境变量 |
|---|---|---|
| Nginx HTTP | 80 | `SVR_WEB_HTTP_PORT` |
| Nginx HTTPS | 443 | `SVR_WEB_HTTPS_PORT` |
| 主服务 API | 8123 | `SVR_HTTP_PORT` |
| Admin API | 8130 | `ADMIN_SVR_HTTP_PORT` |
| PostgreSQL | 5432 | `POSTGRES_PORT` |
| Redis | 6379 | `REDIS_PORT` |
| MinIO | 9000/9001 | `MINIO_PORT` / `MINIO_CONSOLE_PORT` |
| Milvus | 19530 | `MILVUS_PORT` |
| Elasticsearch | 9200 | `ES_PORT` |

### 关键配置项示例

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

### 后端切换建议

- 切换检索后端时同步检查 Docker profile 与配置项
- 升级后优先确认 Alembic 迁移状态
- OceanBase / SeekDB / Infinity 建议单独做检索链路回归

## 🔨 源码部署开发

### 1. 安装依赖

```bash
uv sync --python 3.12 --all-extras
uv run --script download_deps.py
```

### 2. 启动依赖服务

```bash
docker compose -f docker/docker-compose-base.yml up -d
```

按需启用额外后端：

```bash
docker compose -f docker/docker-compose-base.yml --profile milvus up -d
docker compose -f docker/docker-compose-base.yml --profile infinity up -d
docker compose -f docker/docker-compose-base.yml --profile oceanbase up -d
```

### 3. 启动主服务

```bash
export PYTHONPATH=$(pwd)
uv run python -m api.multirag_server
```

### 4. 运行测试

```bash
uv run pytest
```

运行单测示例：

```bash
uv run pytest tests/unit/test_image_filter.py
```

### 5. 代码检查

```bash
ruff check
ruff format
```

### 6. 常用开发入口

- 服务入口：[`api/multirag_server.py`](./api/multirag_server.py)
- API 路由：[`api/apps`](./api/apps)
- 核心逻辑：[`core`](./core)
- 文档解析：[`deepdoc`](./deepdoc)
- Agent：[`agent`](./agent)
- MCP：[`mcp`](./mcp)
- Workflow：[`workflow`](./workflow) / [`workflow_v2`](./workflow_v2)

## 📚 API 与文档

### API 能力域

当前版本主要接口域包括：

- Knowledge Base / Dataset / Document / Chunk 管理
- Chat / Conversation / Retrieval / OpenAI-compatible API
- LLM Provider、模型配置与系统设置
- Agent、Canvas、Workflow、Workflow v2
- Connector、MCP server、Admin、用户与租户管理

### 常见接口示例

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/chat/completions` | POST | 对话接口 |
| `/api/v1/datasets` | GET/POST | 知识库管理 |
| `/api/v1/documents` | POST/DELETE | 文档管理 |
| `/api/v1/chunks` | GET | 文档片段查询 |
| `/api/v1/workflows` | GET/POST | 工作流管理 |
| `/api/v1/agents` | POST | Agent 执行 |
| `/api/v1/mcp/*` | GET/POST | MCP 相关接口 |

OpenAPI 文档默认可通过 `/docs` 访问（服务启动后）。

### 文档导航

- 快速开始：[`docs/get_started.md`](./docs/get_started.md)
- 部署说明：[`docs/DEPLOYMENT_GUIDE.md`](./docs/DEPLOYMENT_GUIDE.md)
- 架构说明：[`docs/architecture.md`](./docs/architecture.md)
- HTTP API：[`docs/references/http_api_reference.md`](./docs/references/http_api_reference.md)
- Python API：[`docs/references/python_api_reference.md`](./docs/references/python_api_reference.md)
- 支持模型：[`docs/references/supported_models.md`](./docs/references/supported_models.md)
- Docker 说明：[`docker/README.md`](./docker/README.md)

## 🛠️ 适用场景

- 企业内部知识库问答与智能搜索
- 多数据源同步入库与统一检索
- 面向业务系统的 AI Agent 后端
- 文档理解、表格识别、图像抽取与结构化处理
- OpenAI 兼容生态的中间层平台

## 🤝 贡献指南

欢迎提交 Issue 和 PR。

开始开发前建议先阅读：

- [`docs/architecture.md`](./docs/architecture.md)
- [`docker/README.md`](./docker/README.md)
- [`AGENTS.md`](./AGENTS.md)

提交前建议至少执行：

```bash
uv run pytest
ruff check
ruff format
```

## 📄 许可证

本项目采用 Apache-2.0 License。
