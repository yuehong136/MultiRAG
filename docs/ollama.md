# 使用 Ollama 运行本地模型

MultiRAG 的 Python 后端支持将 Ollama 配置为 Chat 或 Embedding 提供商。先让后端进程能访问
Ollama，再添加模型并在对话或知识库中选择它。模型下载完成后，本地推理无需调用云端模型；
其他模型、解析器和数据连接器仍按各自配置运行。

## 1. 启动服务并准备模型

安装 [Ollama](https://docs.ollama.com/quickstart) 后，在一个终端启动服务；已有后台服务时直接使用它：

```bash
ollama serve
```

在另一个终端下载适合机器资源的模型并确认名称。以下 `llama3` 仅作示例：

```bash
ollama pull llama3
ollama list
```

默认监听 `127.0.0.1:11434`。同机直接运行 Python 后端时可保持默认；容器或其他机器访问时，
需要在 **Ollama 服务进程**配置可达的监听地址，例如 `OLLAMA_HOST=0.0.0.0:11434 ollama serve`。
`0.0.0.0` 会监听所有网卡，应限制为可信网络可访问。桌面应用或系统服务的环境变量配置方式
见 [Ollama FAQ](https://docs.ollama.com/faq#how-do-i-configure-ollama-server)。

## 2. 从后端所在网络选择 Base URL

这里的“本机”是运行 Python API / worker 的机器或容器。浏览器能打开 Ollama 地址，不代表后端可达。
API 与 worker 分开部署时，两者都要能访问同一个模型服务。

| 部署方式 | 在 MultiRAG 中填写的 Base URL |
| --- | --- |
| Python 后端与 Ollama 直接运行在同一宿主机 | `http://localhost:11434` |
| Docker Desktop 中的后端访问宿主机 Ollama | `http://host.docker.internal:11434` |
| Linux Docker Engine 中的后端访问宿主机 Ollama | `http://host.docker.internal:11434`，前提是容器配置了 host gateway 映射 |
| 后端与 Ollama 在同一 Docker 网络 | Ollama 的服务名和容器端口，例如 `http://ollama:11434`（服务名需实际存在） |
| Ollama 在另一台机器 | `http://<可达的主机地址>:11434`，或已配置的反向代理根地址 |

容器内 `localhost` 指向该容器。Docker Desktop 提供上述特殊域名，详见
[容器访问宿主机](https://docs.docker.com/desktop/features/networking/networking-how-tos/#connect-a-container-to-a-service-on-the-host)。
本仓[默认 Compose](../docker/docker-compose.yml)和[独立部署 Compose](../docker/docker-compose-standalone.yml)
已包含 `host.docker.internal:host-gateway` 映射。自定义 Linux Docker Engine 部署缺少映射时，
可在本地 Compose 覆盖文件中为实际 API / worker service 添加
`extra_hosts: ["host.docker.internal:host-gateway"]`，或直接使用可达的宿主机地址；具体语义见
[Docker host gateway](https://docs.docker.com/reference/cli/docker/container/run/#add-host)。
同时确认 Ollama 的监听地址和防火墙允许该容器访问。

**Base URL 填服务根地址**，例如 `http://host.docker.internal:11434`；使用 Ollama 提供商时，
不要附加 `/api`、`/api/chat`、`/api/embeddings` 或 `/v1`。客户端会选择原生 API 端点：
Chat 使用 `/api/chat`，当前 Python Embedding 使用 `/api/embeddings`。

可从后端相同的网络环境请求 `/api/tags` 检查服务和模型列表。例如后端直接运行在宿主机时：

```bash
curl --fail-with-body http://localhost:11434/api/tags
```

默认 Compose 容器名为 `multirag`，访问宿主机 Ollama 的示例为：

```bash
docker exec multirag python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=10)))"
```

按实际容器名和地址替换；有鉴权代理时需提供对应凭据。模型列表可达只证明服务连通，添加模型时
还会实际调用所选模型。

## 3. 添加并选择模型

1. 在 Web 打开 **设置 → 模型提供商 → Ollama**，添加模型。
2. 模型类型选 **Chat**；名称填写 `ollama list` 显示的完整名称及 tag，例如 `llama3:latest`。
3. 显式填写上表对应的 **Base URL**。本地无鉴权服务可将 **API Key** 留空；反向代理要求
   Bearer token 时填写其 token。
4. 保存成功后，在对话的模型选择器中选择该模型；Agent 则在使用模型的节点中选择它。
   工具调用和视觉开关应与实际模型能力一致。

保存会验证模型可用性，失败时不会保存配置。`Fail to access model` 需结合返回的具体错误排查：
先检查地址、模型名和 tag，再检查代理鉴权、服务日志及首次加载的耗时。

需要 Ollama 向量化时，另行下载支持 embedding 的模型，并在同一提供商中以 **Embedding**
类型添加，随后在知识库中选择它。添加 Chat 模型不会改变现有知识库的嵌入配置。
服务地址也不会靠安装 Ollama 自动写入 MultiRAG；自动配置入口见
[Docker 部署说明](../docker/README.md)。

## 内存、CPU 与向量维度

- Compose 的 `DEVICE=cpu` 默认值用于 MultiRAG 的 DeepDoc 运行组合；外部 Ollama 使用 CPU
  还是 GPU 由 Ollama 自身决定，可用 `ollama ps` 查看。后端的
  [4 核 / 16 GB 起步建议](../README.md#self-hosting)不包含任意 Ollama 模型的内存保证，
  需为模型大小、上下文和并发另留内存或显存。
- 当前 Python Ollama **Embedding** 默认传 `keep_alive=-1`，会让模型保持加载。需要及时释放时，
  可在调用它的 API / worker 进程设置 `OLLAMA_KEEP_ALIVE=0`，代价是后续请求重新加载。
  此处环境变量按整数秒读取；不要填写 `5m` 等时长字符串。Chat 使用的驻留策略由 Ollama
  及请求参数决定，不能据此套用 Embedding 的默认值。
- 向量维度取决于所选嵌入模型的实际输出，不能用 Chat 的 Max Tokens 或随意填写的维度代替。
  即使维度相同，不同模型的向量空间也可能不兼容。更换已有知识库的嵌入模型不会自动重算旧向量；
  稳妥迁移方式是新建目标模型的知识库、重新解析样本并比较检索结果，见
  [知识库使用指南](./knowledge-base-guide.md)。
