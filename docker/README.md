# README

<details open>
<summary><b>📗 目录</b></summary>

- 🐳 [Docker Compose](#-docker-compose)
- 🏗️ [架构设计](#-架构设计)
- 🐬 [Docker 环境变量](#-docker-环境变量)
- 📦 [独立镜像部署](#-独立镜像部署)
- 🧠 [外部模型目录](#-外部模型目录)
- 🌐 [Nginx 配置](#-nginx-配置)
- 🧠 [TEI 服务配置](#-tei-服务配置)
- 🐋 [启动选项](#-启动选项)
- 📋 [使用示例](#-使用示例)

</details>

## 🐳 Docker Compose

- **docker-compose.yml**  
  配置 MultiRAG 主服务，通过 `include` 引用 `docker-compose-base.yml` 获取基础设施服务。
- **docker-compose-base.yml**  
  基础设施服务配置（PostgreSQL、Redis、MinIO、Elasticsearch、Milvus 等），供 `docker-compose.yml` 引用。
- **docker-compose-macos.yml**  
  macOS (Apple Silicon) 专用配置，通过 Rosetta 2 模拟 x86_64 架构。
- **docker-compose-standalone.yml**
  可脱离源码仓库分发的单镜像配置，只依赖外部 `service_conf.yaml` 和模型目录。
- **docker-compose-tei.yml**  
  TEI 服务独立部署配置（已整合到 base 文件中，此文件保留用于向后兼容）。
- **.env**  
  包含 Docker 服务的重要环境变量配置。
- **entrypoint.sh**  
  容器启动脚本，支持灵活的组件启停控制。
- **nginx/**  
  Nginx 反向代理配置目录，包含 HTTP/HTTPS 站点配置和代理规则。

> [!NOTE]
> 本目录结构和配置方式参考了 [ragflow](https://github.com/infiniflow/ragflow) 项目。
> - 基于 ragflow 的 73144e27 提交：使用 Docker Compose profiles 机制支持按需启动不同服务组合
> - 基于 ragflow 的 3fe71ab7 提交：使用数组语法定义 command，避免参数引用问题

## 🏗️ 架构设计

### 文件分层结构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  docker-compose.yml (主配置文件)                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  include:                                                               │ │
│  │    - ./docker-compose-base.yml   ◀─── 引用基础设施服务                    │ │
│  │                                                                         │ │
│  │  services:                                                              │ │
│  │    multirag-cpu:    # CPU 版本主服务                                     │ │
│  │    multirag-gpu:    # GPU 版本主服务                                     │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ▲
                                    │ include
                                    │
┌─────────────────────────────────────────────────────────────────────────────┐
│  docker-compose-base.yml (基础设施配置)                                       │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  services:                                                              │ │
│  │    postgres:         # PostgreSQL 数据库                                 │ │
│  │    redis:            # Redis 缓存                                        │ │
│  │    minio:            # MinIO 对象存储                                    │ │
│  │    es01:             # Elasticsearch 搜索引擎                            │ │
│  │    opensearch01:     # OpenSearch 搜索引擎                               │ │
│  │    milvus-standalone:# Milvus 向量数据库                                 │ │
│  │    infinity:         # Infinity 向量数据库                               │ │
│  │    oceanbase:        # OceanBase 向量数据库                              │ │
│  │    tei-cpu/tei-gpu:  # Text Embeddings Inference                        │ │
│  │    sandbox-executor: # 沙箱执行器                                        │ │
│  │    kibana:           # Elasticsearch 可视化                              │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 设计优势

1. **关注点分离**：基础设施服务与应用服务分开管理
2. **便于维护**：修改基础设施不影响主服务配置
3. **灵活组合**：通过 profiles 按需启用可选服务
4. **复用性强**：base 文件可被多个配置文件引用

### 服务分类

服务分为**必需服务**和**可选服务**两类：

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
| `milvus` | milvus-standalone, milvus-etcd, milvus-minio | Milvus 向量数据库集群 (v2.6.7) |
| `infinity` | infinity | Infinity 向量数据库 |
| `oceanbase` | oceanbase | OceanBase 向量数据库 |
| `tei-cpu` | tei-cpu | TEI 服务（CPU 版本） |
| `tei-gpu` | tei-gpu | TEI 服务（GPU 版本） |
| `sandbox` | sandbox-executor-manager | 沙箱执行器 |
| `kibana` | kibana | Elasticsearch 可视化工具 |

### 快速启动示例

```bash
cd docker

# 最小化部署（自动启动 postgres + redis + minio + multirag）
docker compose --profile cpu up -d

# 加上 Milvus 向量数据库
docker compose --profile cpu --profile milvus up -d

# 加上 Elasticsearch 搜索引擎
docker compose --profile cpu --profile elasticsearch up -d

# 加上 TEI Embedding 服务
docker compose --profile cpu --profile tei-cpu up -d

# 完整部署（Milvus + TEI）
docker compose --profile cpu --profile milvus --profile tei-cpu up -d

# GPU 模式
docker compose --profile gpu --profile milvus --profile tei-gpu up -d
```

### 启动的容器数量

| 命令 | 启动的容器 |
|------|-----------|
| `--profile cpu` | postgres, redis, minio, multirag-cpu (4个) |
| `--profile cpu --profile milvus` | 上述 + milvus-etcd, milvus-minio, milvus-standalone (7个) |
| `--profile cpu --profile milvus --profile tei-cpu` | 上述 + tei-cpu (8个) |
| `--profile cpu --profile elasticsearch` | postgres, redis, minio, multirag-cpu, es01 (5个) |

## 📦 独立镜像部署

`docker-compose-standalone.yml` 用于服务器上只保留 MultiRAG 镜像、一份配置文件和
外部模型的场景。它不依赖仓库的 `configs/`、`entrypoint.sh` 或源码目录。

宿主机与容器的映射契约为：

| 宿主机参数 | 容器内固定路径 | 说明 |
|---|---|---|
| `MULTIRAG_CONFIG_FILE` | `/multirag/configs/service_conf.yaml` | 可指向现有 `service_config.yaml` 或任意文件名 |
| `MULTIRAG_MODEL_DIR` | `/root/.ragdatav` | embedding/rerank 运行时模型根目录 |

示例：

```bash
mkdir -p /opt/multirag /data/multirag/models
cd /opt/multirag

# 放入 docker-compose-standalone.yml 和你现有的 service_config.yaml
MULTIRAG_IMAGE=datav/multirag:latest \
MULTIRAG_CONFIG_FILE=/opt/multirag/service_config.yaml \
MULTIRAG_MODEL_DIR=/data/multirag/models \
DOC_ENGINE=milvus \
STORAGE_IMPL=MINIO \
docker compose -f docker-compose-standalone.yml up -d
```

Compose 通过 `configs` 将配置文件只读挂载，并为容器设置
`SKIP_CONFIG_GENERATE=1`，因此入口脚本不会重新生成或覆盖该文件。容器默认会启动自带
Redis；如果 `service_conf.yaml` 使用外部 Redis，设置 `ENABLE_REDIS=0` 即可关闭容器内 Redis。

> [!IMPORTANT]
> 镜像、`service_conf.yaml` 与数据库迁移必须保持版本兼容。更新镜像时建议同时保存一份对应的
> 配置文件，并先运行 `docker compose config` 检查最终编排。

## 🧠 外部模型目录

主镜像和 build-only 依赖镜像都不再包含 embedding/rerank 模型。宿主机可以使用任意
磁盘路径，但容器内根目录始终是 `/root/.ragdatav`。目录必须按模型 basename 平铺：

```text
${MULTIRAG_MODEL_DIR}/
├── bge-large-zh-v1.5/
├── bge-reranker-v2-m3/
├── bce-embedding-base_v1/
└── bce-reranker-base_v1/
```

只需上传部署实际使用的模型。不要直接把带有 `BAAI/` 和 `maidalun1020/` 中间层的
`huggingface.co/` 根目录挂载进去，因为加载器会直接查找
`/root/.ragdatav/<model-basename>`。

仓库 Compose 和 standalone Compose 都默认使用只读挂载。这可避免 API 和 TaskExecutor
并发下载模型、模型目录无限增长；代价是启动前必须上传完整文件。需要自动下载时，
建议在独立准备机器上运行：

```bash
HF_ENDPOINT=https://hf-mirror.com uv run --script download_deps.py \
  --runtime-models-only --runtime-model-dir /data/multirag/models
```

## 🐬 Docker 环境变量

[.env](./.env) 文件包含 Docker 的重要环境变量配置。

### 基础运行配置

- `WS`  
  TaskExecutor（任务执行器）的数量。默认值为 `1`。
  
- `PYTHON_BIN`  
  Python 解释器路径。默认值为 `python3`。通常无需修改。

- `STRICT_MODE`  
  是否启用严格模式。`1` 表示启用，`0` 表示禁用。启用后脚本遇到错误会立即退出。默认值为 `0`。

### Admin Server 配置

- `ADMIN_SVR_HTTP_PORT`  
  Admin Server 的 HTTP 端口，用于将容器内的服务暴露到宿主机。默认值为 `8130`。

### Redis 配置

- `REDIS_CONF_PATH`  
  Redis 配置文件在容器内的路径。默认值为 `/etc/redis/redis.conf`。

- `REDIS_HOST`  
  如果使用外部 Redis，指定 Redis 主机地址。

- `REDIS_PORT`  
  如果使用外部 Redis，指定 Redis 服务端口。默认值为 `6379`。

- `REDIS_PASSWORD`  
  如果使用外部 Redis，指定 Redis 密码。

### Docling 解析器配置

- `USE_DOCLING`  
  是否启用 Docling PDF 解析器。设置为 `true` 启用，`false` 禁用。默认值为 `false`。  
  启用后，容器启动时会自动检测并安装 docling 依赖。

- `DOCLING_VERSION`  
  可选。指定要安装的 Docling 版本。格式示例：  
  - `==1.0.0`（精确版本）
  - `>=1.0.0,<2.0.0`（版本范围）  
  留空则安装最新版本。

### HuggingFace 镜像站点

- `HF_ENDPOINT`  
  HuggingFace 镜像站点 URL。如果您访问 huggingface.co 受限，可配置此项。默认值为 `https://hf-mirror.com`。

### 时区设置

- `TZ`  
  容器的时区设置。默认值为 `Asia/Shanghai`。

### Nginx / Web Server 配置

- `ENABLE_WEBSERVER`  
  Web Server 开关。`1` 启用 Nginx，`0` 禁用。默认值为 `1`。

- `SVR_WEB_HTTP_PORT`  
  Nginx HTTP 端口（对外暴露）。默认值为 `80`。

- `SVR_WEB_HTTPS_PORT`  
  Nginx HTTPS 端口（对外暴露，需配置 SSL 证书）。默认值为 `443`。

### 直连端口配置（兼容模式）

以下端口用于直连后端服务，主要用于调试或兼容旧版客户端。如需关闭直连，在 `docker-compose.yml` 中注释对应端口映射即可。

- `SVR_HTTP_PORT`  
  主服务 API 直连端口。默认值为 `8123`。

- `ADMIN_SVR_HTTP_PORT`  
  管理后台 API 直连端口。默认值为 `8130`。

## 🌐 Nginx 配置

`nginx/` 目录包含 Nginx 反向代理配置文件，用于统一管理 API 路由和静态资源服务。

### 访问方式

服务启动后，支持两种访问方式（平滑过渡设计）：

#### 方式一：通过 Nginx（推荐）

```bash
# HTTP（端口 80）
curl http://your-server/api/v1/user/login
curl http://your-server/api/v1/admin/users

# HTTPS（端口 443，需配置证书）
curl https://your-server/api/v1/user/login
```

#### 方式二：直连后端（兼容/调试）

```bash
# 主服务 API（端口 8123）
curl http://your-server:8123/api/v1/user/login

# 管理后台 API（端口 8130）
curl http://your-server:8130/api/v1/admin/users
```

> [!TIP]
> 如需关闭直连访问，在 `docker-compose.yml` 中注释以下端口映射即可：
> ```yaml
> # - "${SVR_HTTP_PORT:-8123}:8123"       # 主服务 API 直连
> # - "${ADMIN_SVR_HTTP_PORT:-8130}:8130" # 管理后台 API 直连
> ```

### 目录结构

```
nginx/
├── nginx.conf              # Nginx 主配置文件
├── proxy.conf              # 代理通用配置（请求头、超时、缓冲区）
├── multirag.conf           # HTTP 站点配置（端口 80）
└── multirag.https.conf     # HTTPS 站点配置（端口 443，预留）
```

### 端口映射

根据 `configs/service_conf.yaml` 配置：

| 服务 | 内部端口 | 路由规则 |
|------|---------|---------|
| 主服务 API | 8123 | `/v1/*`, `/api/*` |
| 管理后台 API | 8130 | `/api/v1/admin/*` |
| 前端静态资源 | - | `/`（预留） |

### 配置文件说明

#### nginx.conf

主配置文件，包含：
- Worker 进程配置（自动检测 CPU 核心数）
- 日志格式定义
- 最大上传文件大小（1024M）
- 引入 `multirag.conf` 站点配置

#### proxy.conf

代理通用配置，被其他配置文件引用：
- 请求头转发（Host, X-Forwarded-For, X-Forwarded-Proto）
- 超时配置（3600s，支持长时间运行的请求）
- 缓冲区配置（适合大文件传输）

#### multirag.conf

HTTP 站点配置，监听端口 80：
- API 路由到后端服务
- Gzip 压缩
- 前端静态资源服务（预留）

#### multirag.https.conf

HTTPS 站点配置模板（预留），包含：
- HTTP 到 HTTPS 重定向
- SSL 证书配置占位
- 与 HTTP 版本相同的路由规则

### 启用 HTTPS

#### 方式一：使用 Let's Encrypt 免费证书

1. **安装 Certbot**
   ```bash
   # Ubuntu/Debian
   sudo apt update && sudo apt install certbot
   
   # macOS
   brew install certbot
   ```

2. **获取证书**
   ```bash
   # 确保 80/443 端口未被占用
   sudo certbot certonly --standalone -d your-multirag-domain.com
   ```

3. **证书位置**
   - 证书: `/etc/letsencrypt/live/your-multirag-domain.com/fullchain.pem`
   - 私钥: `/etc/letsencrypt/live/your-multirag-domain.com/privkey.pem`

4. **修改 docker-compose.yml**
   在 `multirag` 服务中添加卷挂载：
   ```yaml
   services:
     multirag:
       # ...existing configuration...
       volumes:
         # SSL 证书
         - /etc/letsencrypt/live/your-multirag-domain.com/fullchain.pem:/etc/nginx/ssl/fullchain.pem:ro
         - /etc/letsencrypt/live/your-multirag-domain.com/privkey.pem:/etc/nginx/ssl/privkey.pem:ro
         # 切换到 HTTPS 配置
         - ./nginx/multirag.https.conf:/etc/nginx/conf.d/multirag.conf
         # ...other existing volumes...
   ```

5. **更新 nginx 配置**
   编辑 `nginx/multirag.https.conf`，将 `your-multirag-domain.com` 替换为实际域名。

6. **重启服务**
   ```bash
   docker compose down
   docker compose up -d
   ```

#### 方式二：使用已有证书

如果您已有其他 CA 签发的证书：

1. 将证书文件放置到 Docker 可访问的目录
2. 修改 `docker-compose.yml` 中的卷挂载路径指向您的证书文件
3. 确保证书文件包含完整的证书链
4. 按照上述步骤 5-6 操作

> [!IMPORTANT]
> - 确保域名的 DNS A 记录指向服务器 IP 地址
> - 使用 `--standalone` 方式获取证书时，需停止占用 80/443 端口的服务

> [!TIP]
> 开发或测试环境可使用自签名证书，但浏览器会显示安全警告。

### 自定义配置

如需修改上传文件大小限制，编辑 `nginx/nginx.conf`：

```nginx
# 修改此值（默认 1024M）
client_max_body_size 2048M;
```

同时确保 `configs/service_conf.yaml` 中的相关配置保持一致。

## 🧠 TEI 服务配置

TEI (Text Embeddings Inference) 是 HuggingFace 提供的高性能 embedding 推理服务。通过 Docker Compose 的 profiles 机制，可以将 TEI 作为独立容器部署，实现微服务架构。

### 架构说明

```
┌────────────────────────────────────┐      HTTP API     ┌──────────────────────────────────────┐
│  Container: multirag               │ ──────────────▶   │  Container: tei-cpu/tei-gpu          │
│  Image: multirag:latest            │                   │  Image: text-embeddings-inference    │
│                                    │   POST /embed     │                                      │
│  轻量级镜像，不含模型权重              │   {inputs: [...]} │  独立 embedding 服务                  │
│  通过 HTTP 调用 TEI 服务             │                   │  加载指定的 embedding 模型             │
│                                    │  ◀─────────────── │                                      │
│                                    │   embeddings      │                                      │
└────────────────────────────────────┘                   └──────────────────────────────────────┘
```

### TEI 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DEVICE` | `cpu` | 设备类型：`cpu` 或 `gpu` |
| `COMPOSE_PROFILES` | `${DEVICE}` | Docker Compose profiles，启用 TEI 需追加 `,tei-cpu` 或 `,tei-gpu` |
| `TEI_IMAGE_CPU` | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.8` | TEI CPU 版本镜像 |
| `TEI_IMAGE_GPU` | `ghcr.io/huggingface/text-embeddings-inference:1.8` | TEI GPU 版本镜像 |
| `TEI_PORT` | `6380` | TEI 服务端口（映射到宿主机） |
| `TEI_MODEL` | `BAAI/bge-small-en-v1.5` | 使用的 embedding 模型 |
| `TEI_MODEL_CACHE` | `~/.cache/huggingface` | 模型缓存目录 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HuggingFace 镜像站点 |

### 推荐模型

| 模型名称 | 最大 Tokens | 语言 | 说明 |
|----------|-------------|------|------|
| `BAAI/bge-small-en-v1.5` | 512 | 英文 | 小型模型，适合快速测试 |
| `BAAI/bge-m3` | 8192 | 多语言 | 支持中英文，推荐生产使用 |
| `Qwen/Qwen3-Embedding-0.6B` | 32768 | 多语言 | 支持超长文本 |

### 启用 TEI 服务

1. **进入 `docker` 目录**：
   ```bash
   cd docker
   ```

2. **编辑 `.env` 文件启用 TEI**：
   ```bash
   # CPU 模式 + TEI
   DEVICE=cpu
   COMPOSE_PROFILES=cpu,tei-cpu
   TEI_MODEL=BAAI/bge-m3
   
   # 或 GPU 模式 + TEI
   # DEVICE=gpu
   # COMPOSE_PROFILES=gpu,tei-gpu
   ```

3. **启动服务**：
   ```bash
   docker compose up -d
   ```

4. **验证 TEI 服务**：
   ```bash
   # 查看容器状态
   docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
   
   # 测试 TEI 服务
   curl -X POST http://localhost:6380/embed \
     -H "Content-Type: application/json" \
     -d '{"inputs": "Hello world"}'
   ```

### Profiles 组合说明

> [!TIP]
> **必需服务**（postgres、redis、minio）会**自动启动**，无需在 COMPOSE_PROFILES 中指定。

| COMPOSE_PROFILES | 启动的容器 | 说明 |
|------------------|-----------|------|
| `cpu` | postgres, redis, minio, multirag-cpu | 最小化部署（4个容器） |
| `gpu` | postgres, redis, minio, multirag-gpu | GPU 模式最小化部署 |
| `cpu,milvus` | 上述 + milvus-etcd, milvus-minio, milvus-standalone | 加上 Milvus（7个容器） |
| `cpu,milvus,tei-cpu` | 上述 + tei-cpu | 完整部署（8个容器） |
| `cpu,elasticsearch` | postgres, redis, minio, multirag-cpu, es01 | 使用 Elasticsearch |
| `gpu,milvus,tei-gpu` | GPU 版本完整部署 | GPU 模式 |

> [!NOTE]
> TEI 服务首次启动时会自动从 HuggingFace 下载模型，可能需要几分钟时间。
> 如果下载缓慢，请确保配置了 `HF_ENDPOINT` 使用国内镜像站点。

### macOS (Apple Silicon) 专用配置

由于 TEI 只有 x86_64 版本，Apple Silicon Mac 需要使用专用配置文件：

```bash
# 使用 macOS 专用配置
docker compose -f docker-compose-macos.yml up -d

# 启用 TEI 服务
docker compose -f docker-compose-macos.yml --profile tei up -d
```

**前提条件**：
1. Docker Desktop 设置中启用 **"Use Rosetta for x86_64/amd64 emulation on Apple Silicon"**
2. 或使用 OrbStack 并选择 x86-64 (emulated) 架构

**性能说明**：
- 通过 Rosetta 2 模拟运行，性能约为原生的 50-70%
- 适合开发测试，生产环境建议使用 Linux x86_64 服务器

## 🐋 启动选项

MultiRAG 支持通过命令行参数灵活控制启动哪些组件。

### 可用的命令行参数

| 参数 | 说明 |
|-----|------|
| `--disable-redis` | 不启动内置 Redis |
| `--disable-server` | 不启动 API Server |
| `--disable-taskexecutor` | 不启动 TaskExecutor |
| `--disable-webserver` | 不启动 Nginx（Web Server） |
| `--enable-adminserver` | 启动 Admin Server（默认不启动） |
| `--workers=<num>` | 指定 TaskExecutor 数量（覆盖 `WS` 环境变量） |
| `--consumer-no-beg=<num>` | 消费者 ID 起始编号（包含） |
| `--consumer-no-end=<num>` | 消费者 ID 结束编号（不包含） |
| `--host-id=<string>` | 手动指定 HOST_ID |
| `-h` 或 `--help` | 显示帮助信息 |

### 在 docker-compose.yml 中配置

通过修改 `docker-compose.yml` 中的 `command` 字段来配置启动参数：

#### 默认启动（Redis + API Server + TaskExecutor）
```yaml
command: []
```

#### 启用 Admin Server
```yaml
command:
  - --enable-adminserver
```

#### 只启动 Admin Server 和 Redis
```yaml
command:
  - --enable-adminserver
  - --disable-server
  - --disable-taskexecutor
```

#### 自定义 TaskExecutor 数量
```yaml
command:
  - --workers=4
  - --enable-adminserver
```

#### 指定消费者 ID 区间
```yaml
command:
  - --consumer-no-beg=0
  - --consumer-no-end=10
```

## 📋 使用示例

### 🚀 快速启动

#### 方式一：使用环境变量（推荐）

```bash
cd docker

# 1. 编辑 .env 配置 COMPOSE_PROFILES
# 例如：COMPOSE_PROFILES=cpu  或  COMPOSE_PROFILES=cpu,milvus,tei-cpu

# 2. 启动服务（自动启动 postgres + redis + minio + 指定的 profile 服务）
docker compose up -d
```

#### 方式二：命令行指定 profile

```bash
cd docker

# 最小化部署（自动启动 postgres + redis + minio + multirag）
docker compose --profile cpu up -d

# 加上 Milvus 向量数据库
docker compose --profile cpu --profile milvus up -d

# 加上 TEI Embedding 服务
docker compose --profile cpu --profile milvus --profile tei-cpu up -d

# GPU 模式
docker compose --profile gpu --profile milvus --profile tei-gpu up -d
```

#### 方式三：从项目根目录运行

```bash
docker compose -f docker/docker-compose.yml --profile cpu up -d
```

> [!NOTE]
> **必需服务**（postgres、redis、minio）会**自动启动**，无需指定 profile。
> 只需要指定主服务（cpu/gpu）和可选的向量数据库、搜索引擎等服务的 profile。

### 📊 查看日志

```bash
cd docker

# 查看主服务日志
docker compose logs -f multirag

# 查看 TEI 服务日志（如果启用）
docker compose logs -f tei-cpu
```

### 🔄 重启服务

```bash
cd docker
docker compose restart
```

### 🛑 停止服务

```bash
cd docker
docker compose down
```

### 🔧 使用外部 Redis

如果您已有 Redis 服务，可以配置 MultiRAG 使用外部 Redis：

1. 在 `.env` 中配置：
   ```bash
   REDIS_HOST=your-redis-host
   REDIS_PORT=6379
   REDIS_PASSWORD=your-password
   ```

2. 在 `docker-compose.yml` 中添加启动参数：
   ```yaml
   command:
     - --disable-redis
   ```

3. 重新启动服务：
   ```bash
   docker compose down
   docker compose up -d
   ```

### 📄 启用 Docling 解析器

Docling 是一个高级的 PDF 解析器，支持复杂文档的解析。

1. 在 `.env` 中配置：
   ```bash
   USE_DOCLING=true
   DOCLING_VERSION==1.0.0  # 可选，指定版本
   ```

2. 启动服务：
   ```bash
   docker compose up -d
   ```

3. 查看安装日志：
   ```bash
   docker compose logs multirag | grep docling
   ```

> [!TIP]
> 首次启用 Docling 时，容器启动可能会稍慢，因为需要下载并安装 docling 及其依赖。

### 🧠 启用 TEI Embedding 服务

TEI 提供独立的高性能 embedding 服务，适合需要大量向量化的场景。

1. 进入 `docker` 目录：
   ```bash
   cd docker
   cp env.template .env
   ```

2. 在 `.env` 中配置：
   ```bash
   # 设备类型
   DEVICE=cpu
   
   # 启用 TEI 服务
   COMPOSE_PROFILES=cpu,tei-cpu
   
   # 选择模型（推荐 bge-m3 支持中英文）
   TEI_MODEL=BAAI/bge-m3
   
   # 国内用户使用镜像站点
   HF_ENDPOINT=https://hf-mirror.com
   ```

3. 启动服务：
   ```bash
   docker compose up -d
   ```

4. 查看容器状态：
   ```bash
   docker ps
   # 应该看到两个容器：multirag 和 tei-cpu
   ```

5. 测试 TEI 服务：
   ```bash
   curl -X POST http://localhost:6380/embed \
     -H "Content-Type: application/json" \
     -d '{"inputs": "这是一段测试文本"}'
   ```

6. 查看 TEI 日志：
   ```bash
   docker compose logs tei-cpu
   ```

> [!TIP]
> - TEI 首次启动需要下载模型，请耐心等待
> - 使用 `docker compose logs -f tei-cpu` 可以实时查看下载进度
> - 模型会缓存到 `TEI_MODEL_CACHE` 目录，下次启动无需重新下载

#### GPU 模式启用 TEI

如果有 NVIDIA GPU，可以使用 GPU 版本获得更好的性能：

```bash
# .env 配置
DEVICE=gpu
COMPOSE_PROFILES=gpu,tei-gpu
TEI_MODEL=BAAI/bge-m3
```

```bash
# 启动
docker compose up -d

# 验证 GPU 使用
docker exec tei-gpu nvidia-smi
```

### 🔨 重新构建镜像

如果修改了代码或 Dockerfile，需要重新构建镜像：

```bash
# 从项目根目录构建
docker build -t multirag:latest .

# 然后重启服务
cd docker
docker-compose down
docker-compose up -d
```

### 🐛 调试模式

查看容器内的环境变量：

```bash
docker exec multirag env | grep -E 'USE_DOCLING|WS|ADMIN_SVR'
```

进入容器调试：

```bash
docker exec -it multirag bash
```

### 📦 多实例部署

使用消费者 ID 区间部署多个 TaskExecutor 实例：

**实例 1 (处理 ID 0-5):**
```yaml
command:
  - --consumer-no-beg=0
  - --consumer-no-end=5
  - --disable-server
```

**实例 2 (处理 ID 5-10):**
```yaml
command:
  - --consumer-no-beg=5
  - --consumer-no-end=10
  - --disable-server
```

> [!IMPORTANT]
> - 确保不同实例的消费者 ID 区间不重叠
> - 只需要一个实例启动 API Server
> - 所有实例应连接到同一个 Redis 和数据库
