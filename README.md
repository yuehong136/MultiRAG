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

1. 确保 `vm.max_map_count` >= 262144：

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

3. 使用预构建的Docker镜像启动服务：

   ```bash
   $ cd docker
   # 使用CPU进行embedding和文档处理任务：
   $ docker compose -f docker-compose.yml up -d

   # 使用GPU加速embedding和文档处理任务：
   # docker compose -f docker-compose-gpu.yml up -d
   ```

4. 服务启动后检查服务器状态：

   ```bash
   $ docker logs -f multirag-server
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

## 🔧 配置说明

当涉及系统配置时，您需要管理以下文件：

- [.env](./docker/.env): 保存系统的基本设置，例如 `SVR_HTTP_PORT`、`MYSQL_PASSWORD` 和 `MINIO_PASSWORD`。
- [service_conf.yaml](./configs/service_conf.yaml): 配置后端服务。此文件中的环境变量将在Docker容器启动时自动填充。
- [docker-compose.yml](./docker/docker-compose.yml): 系统依赖 [docker-compose.yml](./docker/docker-compose.yml) 启动。

> [docker/README.md](./docker/README.md) 文件提供了环境设置和服务配置的详细说明，这些配置可以在 [service_conf.yaml](./configs/service_conf.yaml) 文件中用作 `${ENV_VARS}`。

要更新默认HTTP服务端口（8000），请转到 [docker-compose.yml](./docker/docker-compose.yml) 并将 `8000:8000` 更改为 `<YOUR_SERVING_PORT>:8000`。

对上述配置的更新需要重新启动所有容器才能生效：

> ```bash
> $ docker compose -f docker-compose.yml up -d
> ```

### 从milvus切换到Infinity文档引擎

MultiRAG默认使用milvus存储全文和向量。要切换到 [Infinity](https://github.com/infiniflow/infinity/)，请按照以下步骤操作：

1. 停止所有运行的容器：

   ```bash
   $ docker compose -f docker/docker-compose.yml down -v
   ```

> [!WARNING]
> `-v` 将删除docker容器卷，现有数据将被清除。

2. 在 **docker/.env** 中将 `DOC_ENGINE` 设置为 `infinity`。

3. 启动容器：

   ```bash
   $ docker compose -f docker-compose.yml up -d
   ```

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

## 🔧 构建不包含embedding模型的Docker镜像

此镜像大小约为2 GB，依赖外部LLM和embedding服务。

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ docker build --platform linux/amd64 --build-arg LIGHTEN=1 -f Dockerfile -t multirag:slim .
```

## 🔧 构建包含embedding模型的Docker镜像

此镜像大小约为9 GB。由于它包含embedding模型，因此仅依赖外部LLM服务。

```bash
$ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
$ cd multrag/
$ docker build --platform linux/amd64 -f Dockerfile -t multirag:latest .
```

## 🔨 从源代码启动服务进行开发

1. 安装 `uv` 和 `pre-commit`，如果已安装则跳过此步骤：

   ```bash
   $ pipx install uv pre-commit
   ```

2. 克隆源代码并安装Python依赖：

   ```bash
   $ git clone http://122.112.170.159:8888/scdf/ai/multrag.git
   $ cd multrag/
   $ uv sync --python 3.12 --all-extras  # 安装MultiRAG依赖的Python模块
   $ uv run download_deps.py
   $ pre-commit install
   ```

3. 使用Docker Compose启动依赖服务（MinIO、Elasticsearch、Redis和MySQL）：

   ```bash
   $ docker compose -f docker/docker-compose-base.yml up -d
   ```

   将以下行添加到 `/etc/hosts` 以将 **docker/.env** 中指定的所有主机解析为 `127.0.0.1`：

   ```
   127.0.0.1       es01 infinity mysql minio redis
   ```

4. 如果无法访问HuggingFace，请设置 `HF_ENDPOINT` 环境变量以使用镜像站点：

   ```bash
   $ export HF_ENDPOINT=https://hf-mirror.com
   ```

5. 如果您的操作系统没有jemalloc，请按如下方式安装：

   ```bash
   # Ubuntu
   $ sudo apt-get install libjemalloc-dev
   # CentOS
   $ sudo yum install jemalloc
   # OpenSUSE
   $ sudo zypper install jemalloc
   # macOS
   $ sudo brew install jemalloc
   ```

6. 启动后端服务：

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

7. 开发完成后停止MultiRAG前端和后端服务：

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
├── api/                    # API服务层
│   ├── apps/              # FastAPI应用
│   ├── db/                # 数据库模型和服务
│   └── service/           # 业务逻辑服务
├── core/                   # 核心引擎
│   ├── app/               # 核心应用逻辑
│   ├── llm/               # LLM适配器
│   ├── nlp/               # NLP处理模块
│   ├── svr/               # 服务器组件
│   └── utils/             # 工具函数
├── agent/                  # Agent智能体
├── deepdoc/               # 深度文档处理
├── graphrag/              # 图RAG引擎
├── workflow/              # 工作流引擎
├── mcp/                   # MCP协议支持
├── configs/               # 配置文件
└── docker/                # Docker部署配置
```

### 核心组件

1. **TaskExecutor** - 分布式任务执行器
2. **RAG Engine** - 检索增强生成引擎
3. **GraphRAG** - 知识图谱增强检索
4. **Workflow Engine** - 工作流编排引擎
5. **Agent Framework** - 智能体框架
6. **DeepDoc Parser** - 深度文档解析器

### 扩展开发

添加自定义组件：

```python
# 自定义文档解析器
from deepdoc.parser import ParserBase

class CustomParser(ParserBase):
    def __call__(self, filename, binary=None, from_page=0, to_page=100000, **kwargs):
        # 实现自定义解析逻辑
        pass

# 注册解析器
FACTORY["custom"] = CustomParser
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