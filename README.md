<div align="center">
<h1>MultiRAG - 企业级RAG智能后端引擎</h1>
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
  <a href="#-文档">文档</a> |
  <a href="#-roadmap">Roadmap</a> |
  <a href="#-社区">社区</a> |
  <a href="#-api文档">API文档</a>
</h4>

#

<details open>
<summary><b>📕 目录</b></summary>

- 💡 [MultiRAG 是什么？](#-multirag-是什么)
- 🔥 [最新更新](#-最新更新)
- 🌟 [核心功能](#-核心功能)
- 🔎 [系统架构](#-系统架构)
- 🎬 [快速开始](#-快速开始)
- 🔧 [配置说明](#-配置说明)
- 🔧 [Docker镜像构建](#-docker镜像构建)
- 🔨 [源码部署开发](#-源码部署开发)
- 📚 [API文档](#-api文档)
- 📚 [文档](#-文档)
- 📜 [Roadmap](#-roadmap)
- 🏄 [社区](#-社区)
- 🙌 [贡献指南](#-贡献指南)

</details>

## 💡 MultiRAG 是什么？

MultiRAG 是一款专为企业级应用设计的智能RAG（Retrieval-Augmented Generation）后端引擎。项目集成了RAG检索增强、GraphRAG知识图谱、Workflow工作流编排、Agent智能体等核心技术，为企业提供完整的AI应用后端解决方案。

### 项目特色

- 🔥 **模块化架构** - 核心功能模块独立，支持灵活组合
- ⚡ **高性能处理** - 支持分布式任务执行和并发处理
- 🔌 **多引擎支持** - 兼容Milvus、Elasticsearch、Infinity等向量数据库
- 🌐 **多模型适配** - 支持OpenAI、智谱、通义千问等主流LLM厂商
- 📊 **企业级特性** - 完整的权限管理、监控日志、错误处理机制

## 🔥 最新更新

- 2025-10-29 支持MCP (Model Context Protocol) 协议标准化接口
- 2025-10-20 新增Admin管理服务容器化支持，支持Docker部署
- 2025-10-15 添加敏感词库管理系统，支持Milvus向量检索
- 2025-10-10 优化文档解析性能，支持DOCX表格智能分析
- 2025-10-05 升级FastAPI框架，改进健康检查机制
- 2025-09-28 支持多种向量数据库切换（Milvus/Elasticsearch/Infinity）
- 2025-09-20 新增GraphRAG知识图谱增强检索功能
- 2025-09-15 集成Workflow工作流引擎，支持可视化编排

## 🎉 保持关注

⭐️ Star我们的仓库，随时了解激动人心的新功能和改进！获取新版本的即时通知！🌟

[//]: # (<div align="center" style="margin-top:20px;margin-bottom:20px;">)

[//]: # (<img src="https://github.com/user-attachments/assets/18c9707e-b8aa-4caf-a154-037089c105ba" width="1000"/>)

[//]: # (</div>)

## 🌟 核心功能

### 🍭 **RAG检索增强系统**
- **多模态文档处理** - 支持PDF、Word、PPT、Excel、图片等格式
- **智能文档解析** - 基于DeepDoc深度文档理解技术
- **向量检索优化** - 支持多种embedding模型和重排序算法
- **知识库管理** - 文档上传、索引构建、检索优化一体化

### 🌱 **GraphRAG知识图谱**
- **实体关系抽取** - 自动构建知识图谱
- **图谱推理查询** - 支持复杂关系推理
- **实体消歧与链接** - 智能实体识别和关联

### 🍔 **Workflow工作流引擎**
- **可视化编排** - 支持复杂业务流程设计
- **组件化开发** - 丰富的内置组件库
- **并行执行** - 支持工作流并行和条件分支
- **API集成** - 灵活的第三方系统集成

### 🛀 **Agent智能体框架**
- **工具调用** - 支持Python代码执行、外部API调用
- **角色定义** - 可配置的Agent角色和能力
- **记忆管理** - 会话历史和知识记忆机制
- **多Agent协作** - 支持多智能体协同工作

### 🎨 **MCP协议支持**
- **标准化接口** - 基于Model Context Protocol
- **多模式部署** - 支持self-host和host模式
- **安全认证** - 完整的API Key认证机制

## 🔎 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                          MultiRAG 系统架构                      │
├─────────────────────────────────────────────────────────────────┤
│  API服务层        │    核心引擎层        │      数据存储层        │
│                  │                     │                       │
│  ┌─────────────┐ │  ┌─────────────────┐ │  ┌─────────────────────┐ │
│  │ FastAPI     │ │  │ RAG Engine      │ │  │ 向量数据库           │ │
│  │ RESTful API │ │  │ GraphRAG Engine │ │  │ • Milvus           │ │
│  │ MCP Server  │ │  │ Workflow Engine │ │  │ • Elasticsearch    │ │
│  │ SSE Stream  │ │  │ Agent Framework │ │  │ • Infinity         │ │
│  └─────────────┘ │  │ DeepDoc Parser  │ │  │ • OpenSearch       │ │
│                  │  │ Task Executor   │ │  └─────────────────────┘ │
│  ┌─────────────┐ │  └─────────────────┘ │                       │
│  │ Auth & ACL  │ │                     │  ┌─────────────────────┐ │
│  │ Rate Limit  │ │  ┌─────────────────┐ │  │ 关系数据库           │ │
│  │ Monitoring  │ │  │ LLM Adapters    │ │  │ • PostgreSQL       │ │
│  └─────────────┘ │  │ • OpenAI        │ │  │ • MySQL            │ │
│                  │  │ • ZHIPU-AI      │ │  └─────────────────────┘ │
│                  │  │ • Tongyi-Qianwen│ │                       │
│                  │  │ • Ollama        │ │  ┌─────────────────────┐ │
│                  │  └─────────────────┘ │  │ 对象存储             │ │
│                  │                     │  │ • MinIO            │ │
│                  │  ┌─────────────────┐ │  │ • AWS S3           │ │
│                  │  │ Message Queue   │ │  │ • Azure Blob       │ │
│                  │  │ Redis Streams   │ │  │ • Alibaba OSS      │ │
│                  │  └─────────────────┘ │  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## 🎬 快速开始

### 📝 前置要求

- CPU >= 4核
- 内存 >= 16 GB（推荐32GB以获得更好性能）
- 磁盘 >= 50 GB
- Docker >= 24.0.0 & Docker Compose >= v2.26.1
- Python >= 3.12（源码部署时需要）

> [!TIP]
> 如果您的本地机器（Windows、Mac或Linux）上尚未安装Docker，请参阅 [安装Docker引擎](https://docs.docker.com/engine/install/)。

### 🚀 启动服务

1. 确保 `vm.max_map_count` >= 262144（Linux系统）：

   > 检查 `vm.max_map_count` 的值：
   >
   > ```bash
   > $ sysctl vm.max_map_count
   > ```
   >
   > 如果值小于262144，重置 `vm.max_map_count` 为至少262144：
   >
   > ```bash
   > # 本例中我们设置为262144：
   > $ sudo sysctl -w vm.max_map_count=262144
   > ```
   >
   > 此更改将在系统重启后重置。为确保更改永久生效，请在 **/etc/sysctl.conf** 中相应地添加或更新 `vm.max_map_count` 值：
   >
   > ```bash
   > vm.max_map_count=262144
   > ```

2. 克隆仓库：

   ```bash
   $ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
   $ cd multrag
   ```

3. 使用Docker Compose启动服务：

   ```bash
   $ cd docker

   # 最小化部署（自动启动 postgres + redis + minio + multirag）
   $ docker compose --profile cpu up -d

   # 加上 Milvus 向量数据库
   $ docker compose --profile cpu --profile milvus up -d

   # 加上 Elasticsearch 搜索引擎
   $ docker compose --profile cpu --profile elasticsearch up -d

   # 加上 TEI Embedding 服务
   $ docker compose --profile cpu --profile tei-cpu up -d

   # 完整部署（Milvus + TEI）
   $ docker compose --profile cpu --profile milvus --profile tei-cpu up -d

   # GPU 模式
   $ docker compose --profile gpu --profile milvus --profile tei-gpu up -d
   ```

4. 服务启动后检查服务器状态：

   ```bash
   $ docker logs -f multirag
   ```

   _以下输出确认系统成功启动：_

   ```bash
                __  ___      ____  _ ____  ___   ______
               /  |/  /_  __/ / /_(_) __ \/   | / ____/
              / /|_/ / / / / / __/ / /_/ / /| |/ / __
             / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /
            /_/  /_/\__,_/_/\__/_/_/ |_/_/  |_\____/

   * Running on all addresses (0.0.0.0)
   ```

   > 如果跳过此确认步骤并直接访问MultiRAG，您的浏览器可能会提示 `network abnormal` 错误，因为此时您的MultiRAG可能尚未完全初始化。

5. 在Web浏览器中，输入服务器的IP地址并访问MultiRAG。
   > 使用默认设置时，您只需输入 `http://IP_OF_YOUR_MACHINE:8123`（默认HTTP服务端口为 `8123`）。

6. 在 [service_conf.yaml](./configs/service_conf.yaml) 中，选择所需的LLM工厂并使用相应的API密钥更新 `API_KEY` 字段。

   _开始使用！_

### 🍎 macOS (Apple Silicon) 部署

由于部分服务（如 TEI）只有 x86_64 版本，Apple Silicon Mac 需要使用专用配置文件：

```bash
$ cd docker

# 最小化部署（通过 Rosetta 2 模拟 x86_64）
$ docker compose -f docker-compose-macos.yml --profile cpu up -d

# 加上 Milvus 向量数据库
$ docker compose -f docker-compose-macos.yml --profile cpu --profile milvus up -d

# 加上 TEI Embedding 服务
$ docker compose -f docker-compose-macos.yml --profile cpu --profile tei-cpu up -d
```

**前提条件**：
- Docker Desktop 设置中启用 **"Use Rosetta for x86_64/amd64 emulation on Apple Silicon"**
- 或使用 OrbStack 并选择 x86-64 (emulated) 架构

**性能说明**：
- 通过 Rosetta 2 模拟运行，性能约为原生的 50-70%
- 适合开发测试，生产环境建议使用 Linux x86_64 服务器

### 📦 服务 Profiles 说明

MultiRAG 使用 Docker Compose profiles 机制按需启动服务：

#### 必需服务（默认启动，无需指定 profile）

| 服务 | 容器名 | 端口 | 说明 |
|------|--------|------|------|
| postgres | multirag-postgres | 5432 | PostgreSQL 数据库 |
| redis | multirag-redis | 6379 | Redis 缓存服务 |
| minio | multirag-minio | 9000, 9001 | MinIO 对象存储 |

#### 可选服务（需要指定 profile 启动）

| Profile | 服务 | 说明 |
|---------|------|------|
| `cpu` | multirag-cpu | MultiRAG 主服务（CPU 版本） |
| `gpu` | multirag-gpu | MultiRAG 主服务（GPU 版本） |
| `elasticsearch` | es01 | Elasticsearch 搜索引擎 |
| `opensearch` | opensearch01 | OpenSearch 搜索引擎 |
| `milvus` | milvus-standalone, milvus-etcd, milvus-minio | Milvus 向量数据库集群 |
| `infinity` | infinity | Infinity 向量数据库 |
| `oceanbase` | oceanbase | OceanBase 向量数据库 |
| `tei-cpu` | tei-cpu | TEI Embedding 服务（CPU 版本） |
| `tei-gpu` | tei-gpu | TEI Embedding 服务（GPU 版本） |
| `sandbox` | sandbox-executor-manager | 沙箱执行器 |
| `kibana` | kibana | Elasticsearch 可视化工具 |

#### 启动容器数量参考

| 命令 | 启动的容器 |
|------|-----------|
| `--profile cpu` | postgres, redis, minio, multirag-cpu (4个) |
| `--profile cpu --profile milvus` | 上述 + milvus-etcd, milvus-minio, milvus-standalone (7个) |
| `--profile cpu --profile milvus --profile tei-cpu` | 上述 + tei-cpu (8个) |
| `--profile cpu --profile elasticsearch` | postgres, redis, minio, multirag-cpu, es01 (5个) |

## 🔧 配置说明

当涉及系统配置时，您需要管理以下文件：

### 配置文件结构

```
docker/
├── .env                        # 环境变量配置（端口、密码等）
├── docker-compose.yml          # 主配置文件（引用 base 文件）
├── docker-compose-base.yml     # 基础设施服务配置（PostgreSQL、Redis、MinIO 等）
├── docker-compose-macos.yml    # macOS 专用配置
├── service_conf.yaml.template  # 服务配置模板
└── nginx/                      # Nginx 反向代理配置
    ├── nginx.conf
    ├── proxy.conf
    └── multirag.conf
```

### 主要配置文件

- **[.env](./docker/.env)**: Docker 环境变量配置，包括端口映射、数据库密码、服务开关等
- **[service_conf.yaml](./configs/service_conf.yaml)**: 后端服务配置，包括数据库连接、LLM 配置、存储设置等
- **[docker-compose.yml](./docker/docker-compose.yml)**: 主 Docker Compose 配置，通过 `include` 引用基础设施服务
- **[docker-compose-base.yml](./docker/docker-compose-base.yml)**: 基础设施服务定义（PostgreSQL、Redis、MinIO、Milvus 等）

> [!TIP]
> 详细的 Docker 配置说明请参阅 [docker/README.md](./docker/README.md)

### 端口配置

| 服务 | 默认端口 | 环境变量 |
|------|---------|---------|
| Nginx HTTP | 80 | `SVR_WEB_HTTP_PORT` |
| Nginx HTTPS | 443 | `SVR_WEB_HTTPS_PORT` |
| 主服务 API | 8123 | `SVR_HTTP_PORT` |
| 管理后台 API | 8130 | `ADMIN_SVR_HTTP_PORT` |
| PostgreSQL | 5432 | `POSTGRES_PORT` |
| Redis | 6379 | `REDIS_PORT` |
| MinIO | 9000, 9001 | `MINIO_PORT`, `MINIO_CONSOLE_PORT` |
| Milvus | 19530 | `MILVUS_PORT` |
| Elasticsearch | 9200 | `ES_PORT` |

对配置的更新需要重新启动容器才能生效：

```bash
$ cd docker
$ docker compose --profile cpu down
$ docker compose --profile cpu up -d
```

### 切换向量数据库引擎

MultiRAG 支持多种向量数据库，通过 profiles 机制按需启用：

#### 使用 Milvus（推荐）

```bash
$ docker compose --profile cpu --profile milvus up -d
```

#### 使用 Elasticsearch

```bash
$ docker compose --profile cpu --profile elasticsearch up -d
```

#### 使用 Infinity

```bash
$ docker compose --profile cpu --profile infinity up -d
```

> [!WARNING]
> 切换向量数据库时，如需清除现有数据，请使用 `docker compose down -v`，`-v` 参数会删除数据卷。

### 主要配置选项

#### 环境变量配置

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DOC_ENGINE` | 文档引擎类型 (milvus/elasticsearch/infinity) | `milvus` |
| `STORAGE_IMPL` | 存储实现类型 (MINIO/S3/AZURE/OSS) | `MINIO` |
| `LIGHTEN` | 轻量级模式（不包含embedding模型） | `0` |
| `MAX_CONTENT_LENGTH` | 最大文档大小 | `1GB` |
| `REGISTER_ENABLED` | 用户注册开关 | `1` |

#### 向量数据库配置

MultiRAG支持多种向量数据库：

```yaml
# Milvus配置（推荐）
milvus:
  host: localhost
  port: 19530
  user: root
  password: Milvus

# Elasticsearch配置
elasticsearch:
  hosts: ["http://localhost:9200"]

# Infinity配置
infinity:
  uri: "infinity:23817"
```

#### LLM模型配置

支持主流LLM厂商：

```yaml
user_default_llm:
  factory: "ZHIPU-AI"  # OpenAI, ZHIPU-AI, Tongyi-Qianwen, Ollama等
  api_key: "your-api-key"
  base_url: "https://open.bigmodel.cn/api/paas/v4/"
  default_models:
    chat_model: "glm-4-plus"
    embedding_model: "embedding-2"
    rerank_model: "bge-reranker-v2-m3"
```

## 🔧 Docker镜像构建

### 构建轻量级镜像（不包含embedding模型）

此镜像大小约为2 GB，依赖外部LLM和embedding服务（如 TEI）。

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ docker build --platform linux/amd64 --build-arg LIGHTEN=1 -f Dockerfile -t multirag:slim .
```

### 构建完整镜像（包含embedding模型）

此镜像大小约为9 GB。由于它包含embedding模型，因此仅依赖外部LLM服务。

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ docker build --platform linux/amd64 -f Dockerfile -t multirag:latest .
```

### 重新构建并更新服务

```bash
# 构建镜像
$ docker build -t multirag:latest .

# 重启服务
$ cd docker
$ docker compose --profile cpu down
$ docker compose --profile cpu up -d
```

## 🔨 源码部署开发

### 1. 安装开发工具

```bash
# 安装 uv 和 pre-commit（如已安装则跳过）
$ pipx install uv pre-commit
```

### 2. 克隆源代码并安装依赖

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ uv sync --python 3.12 --all-extras  # 安装MultiRAG依赖的Python模块
$ uv run download_deps.py
$ pre-commit install
```

### 3. 启动基础设施服务

使用 Docker Compose 启动依赖服务（PostgreSQL、Redis、MinIO 等）：

```bash
# 只启动基础设施服务（不启动 multirag 主服务）
$ cd docker
$ docker compose up -d postgres redis minio

# 如需 Milvus 向量数据库
$ docker compose --profile milvus up -d
```

将以下行添加到 `/etc/hosts` 以解析服务主机名：

```
127.0.0.1       postgres redis minio milvus es01 infinity
```

### 4. 配置环境变量

```bash
# HuggingFace 镜像站点（国内用户推荐）
$ export HF_ENDPOINT=https://hf-mirror.com
```

### 5. 安装系统依赖（可选）

如果您的操作系统没有 jemalloc，请按如下方式安装：

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

### 6. 启动后端服务

```bash
$ source .venv/bin/activate
$ export PYTHONPATH=$(pwd)
$ bash docker/launch_backend_service.sh
```

_以下输出确认系统成功启动：_

```bash
             __  ___      ____  _ ____  ___   ______
            /  |/  /_  __/ / /_(_) __ \/   | / ____/
           / /|_/ / / / / / __/ / /_/ / /| |/ / __
          / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /
         /_/  /_/\__,_/_/\__/_/_/ |_/_/  |_\____/

* Running on all addresses (0.0.0.0)
```

### 7. 停止服务

```bash
$ pkill -f "multirag_server.py|task_executor.py"
```

## 📚 API文档

### 核心接口

| 接口路径 | 方法 | 功能描述 |
|----------|------|----------|
| `/api/v1/chat/completions` | POST | 对话聊天接口 |
| `/api/v1/datasets` | GET/POST | 知识库管理 |
| `/api/v1/documents` | POST/DELETE | 文档管理 |
| `/api/v1/chunks` | GET | 文档片段查询 |
| `/api/v1/workflows` | GET/POST | 工作流管理 |
| `/api/v1/agents` | POST | Agent执行 |

### 使用示例

```python
import requests

# 对话接口
response = requests.post(
    "http://localhost:8000/api/v1/chat/completions",
    headers={"Authorization": "Bearer your-api-key"},
    json={
        "model": "glm-4-plus",
        "messages": [
            {"role": "user", "content": "你好，请介绍MultiRAG的功能"}
        ],
        "dataset_ids": ["kb_001"]
    }
)

# 知识库创建
response = requests.post(
    "http://localhost:8000/api/v1/datasets",
    headers={"Authorization": "Bearer your-api-key"},
    json={
        "name": "技术文档库",
        "description": "存储技术相关文档",
        "parser": "manual"
    }
)
```

详细API文档: [http://localhost:8000/docs](http://localhost:8000/docs)

## 🛠️ 开发指南

### 项目结构

```
multirag/
├── api/                        # API 服务层
│   ├── admin/                  # 管理后台 API
│   ├── apps/                   # FastAPI 应用模块
│   │   ├── sdk/               # SDK 接口（兼容 OpenAI API）
│   │   ├── auth/              # OAuth/OIDC 认证
│   │   ├── dataset_app.py     # 知识库管理
│   │   ├── document_app.py    # 文档管理
│   │   ├── conversation_app.py # 对话管理
│   │   ├── workflow_app.py    # 工作流 API
│   │   └── ...                # 其他业务 API
│   ├── db/                    # 数据库层
│   │   ├── models/           # SQLAlchemy 模型
│   │   └── services/         # 数据库服务
│   ├── service/               # 业务逻辑服务
│   ├── middleware/            # 中间件（认证、限流等）
│   └── utils/                 # API 工具函数
│
├── core/                       # 核心引擎
│   ├── app/                   # 文档解析器（按文档类型）
│   │   ├── naive.py          # 通用解析器
│   │   ├── paper.py          # 论文解析器
│   │   ├── resume.py         # 简历解析器
│   │   └── ...               # 其他解析器
│   ├── flow/                  # 文档处理流水线
│   │   ├── parser/           # 解析器
│   │   ├── splitter/         # 分块器
│   │   ├── extractor/        # 信息提取器
│   │   └── tokenizer/        # 分词器
│   ├── llm/                   # LLM 适配器
│   │   ├── chat_model.py     # 对话模型
│   │   ├── embedding_model.py # 向量模型
│   │   ├── rerank_model.py   # 重排序模型
│   │   └── ...               # 各厂商适配器
│   ├── nlp/                   # NLP 处理模块
│   ├── prompts/               # Prompt 模板
│   ├── svr/                   # 服务器组件
│   │   └── task_executor.py  # 任务执行器
│   └── utils/                 # 存储连接器
│       ├── milvus_conn.py    # Milvus 连接
│       ├── es_conn.py        # Elasticsearch 连接
│       └── ...               # 其他连接器
│
├── agent/                      # Agent 智能体框架
│   ├── component/             # Agent 组件
│   │   ├── base.py           # 基础组件
│   │   ├── llm.py            # LLM 组件
│   │   ├── iteration.py      # 循环组件
│   │   └── ...               # 其他组件
│   ├── tools/                 # 工具集
│   │   ├── retrieval.py      # 知识库检索
│   │   ├── code_exec.py      # 代码执行
│   │   ├── crawler.py        # 网页爬取
│   │   ├── duckduckgo.py     # DuckDuckGo 搜索
│   │   └── ...               # 其他工具
│   └── templates/             # Agent 模板
│
├── agentic_reasoning/          # 智能推理模块
│   └── deep_research.py       # 深度研究
│
├── deepdoc/                    # 深度文档处理
│   ├── parser/                # 文档解析器
│   │   ├── pdf_parser.py     # PDF 解析
│   │   ├── docx_parser.py    # Word 解析
│   │   └── ...               # 其他格式
│   └── vision/                # 视觉处理
│
├── graphrag/                   # GraphRAG 知识图谱
│   ├── entity_extractor.py    # 实体抽取
│   ├── graph_builder.py       # 图谱构建
│   └── graph_search.py        # 图谱检索
│
├── workflow/                   # 工作流引擎 v1
├── workflow_v2/                # 工作流引擎 v2
│   ├── component/             # 工作流组件
│   │   ├── llm_component.py  # LLM 组件
│   │   ├── code_component.py # 代码组件
│   │   └── ...               # 其他组件
│   └── workflow.py            # 工作流执行器
│
├── mcp/                        # MCP 协议支持
├── plugin/                     # 插件系统
├── sandbox/                    # 沙箱执行器
├── server/                     # 服务器模块
├── admin/                      # 管理后台服务
├── intergrations/              # 第三方集成
│
├── common/                     # 公共模块
├── configs/                    # 配置文件
├── errors/                     # 错误定义
├── scripts/                    # 脚本工具
└── docker/                     # Docker 部署配置
```

### 核心组件

| 组件 | 位置 | 说明 |
|------|------|------|
| **TaskExecutor** | `core/svr/task_executor.py` | 分布式任务执行器，处理文档解析、向量化等异步任务 |
| **RAG Engine** | `core/flow/` | 检索增强生成引擎，包含解析、分块、向量化流水线 |
| **LLM Adapters** | `core/llm/` | LLM 适配器，支持 OpenAI、智谱、通义等多厂商 |
| **GraphRAG** | `graphrag/` | 知识图谱增强检索，实体抽取和图谱推理 |
| **Workflow Engine** | `workflow_v2/` | 工作流编排引擎，可视化流程设计 |
| **Agent Framework** | `agent/` | 智能体框架，工具调用和多 Agent 协作 |
| **DeepDoc Parser** | `deepdoc/` | 深度文档解析器，支持复杂文档结构识别 |
| **MCP Server** | `mcp/` | Model Context Protocol 服务器 |

### 扩展开发

#### 添加自定义文档解析器

```python
# core/app/custom.py
from core.app.naive import Naive

class CustomParser(Naive):
    """自定义文档解析器"""
    
    def __call__(self, filename, binary=None, from_page=0, to_page=100000, **kwargs):
        # 实现自定义解析逻辑
        sections = []
        # ... 解析逻辑
        return sections

# 在 FACTORY 中注册
FACTORY["custom"] = CustomParser
```

#### 添加自定义 Agent 工具

```python
# agent/tools/custom_tool.py
from agent.tools.base import BaseTool

class CustomTool(BaseTool):
    """自定义工具"""
    name = "custom_tool"
    description = "工具描述"
    
    def run(self, query: str, **kwargs):
        # 实现工具逻辑
        return result
```

#### 添加自定义工作流组件

```python
# workflow_v2/component/custom_component.py
from workflow_v2.component.base_component import BaseComponent

class CustomComponent(BaseComponent):
    """自定义工作流组件"""
    component_type = "custom"
    
    def execute(self, inputs: dict) -> dict:
        # 实现组件逻辑
        return outputs
```

## 🏄 贡献指南

我们欢迎社区贡献！请遵循以下流程：

1. **Fork项目** - 创建你的分支
2. **本地开发** - 在功能分支上开发
3. **代码规范** - 运行 `pre-commit` 检查
4. **测试验证** - 确保测试通过
5. **提交PR** - 详细描述变更内容

### 提交规范

```bash
feat: 新增用户认证模块
fix: 修复文档解析内存泄漏
docs: 更新API文档
style: 代码格式调整
refactor: 重构RAG检索逻辑
perf: 优化向量检索性能
test: 添加单元测试
chore: 更新依赖版本
```

### 开发环境

```bash
# 安装开发工具
uv add --dev pytest black flake8 mypy
pre-commit install

# 运行测试
pytest tests/

# 代码格式化
black . --line-length 120
```

## 📄 许可证

本项目采用 [Apache-2.0](LICENSE) 许可证开源。

---

<p align="center">
  <strong>MultiRAG - 让企业AI应用开发更简单</strong><br>
  如果这个项目对您有帮助，请给我们一个 ⭐️ Star！
</p>