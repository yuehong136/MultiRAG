# README

<details open>
<summary><b>📗 目录</b></summary>

- 🐳 [Docker Compose](#-docker-compose)
- 🐬 [Docker 环境变量](#-docker-环境变量)
- 🧠 [TEI 服务配置](#-tei-服务配置)
- 🐋 [启动选项](#-启动选项)
- 📋 [使用示例](#-使用示例)

</details>

## 🐳 Docker Compose

- **docker-compose.yml**  
  配置 MultiRAG 服务及其运行环境，支持 CPU/GPU 模式和 TEI 服务。
- **docker-compose-macos.yml**  
  macOS (Apple Silicon) 专用配置，通过 Rosetta 2 模拟 x86_64 架构。
- **docker-compose-tei.yml**  
  TEI 服务独立部署配置。
- **.env**  
  包含 Docker 服务的重要环境变量配置。
- **entrypoint.sh**  
  容器启动脚本，支持灵活的组件启停控制。

> [!NOTE]
> 本目录结构和配置方式参考了 [ragflow](https://github.com/infiniflow/ragflow) 项目的 73144e27 提交。
> 使用 Docker Compose profiles 机制支持按需启动不同服务组合。

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

| COMPOSE_PROFILES | 启动的容器 | 说明 |
|------------------|-----------|------|
| `cpu` | multirag-cpu | 仅启动主服务（CPU） |
| `gpu` | multirag-gpu | 仅启动主服务（GPU） |
| `cpu,tei-cpu` | multirag-cpu, tei-cpu | 主服务 + TEI（CPU） |
| `gpu,tei-gpu` | multirag-gpu, tei-gpu | 主服务 + TEI（GPU） |
| `tei-cpu` | tei-cpu | 仅启动 TEI 服务（CPU） |
| `tei-gpu` | tei-gpu | 仅启动 TEI 服务（GPU） |

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
# 例如：COMPOSE_PROFILES=cpu  或  COMPOSE_PROFILES=cpu,tei-cpu

# 2. 启动服务
docker compose up -d
```

#### 方式二：命令行指定 profile

```bash
cd docker

# 仅启动主服务（CPU 模式）
docker compose --profile cpu up -d

# 启动主服务 + TEI 服务
docker compose --profile cpu --profile tei-cpu up -d

# GPU 模式
docker compose --profile gpu --profile tei-gpu up -d
```

#### 方式三：从项目根目录运行

```bash
docker compose -f docker/docker-compose.yml --profile cpu up -d
```

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

