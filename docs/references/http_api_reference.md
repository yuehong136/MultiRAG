---
sidebar_position: 2
slug: /http_api_reference
---

# HTTP API 参考

本文档提供 MultiRAG HTTP API 的完整参考。

## 基本信息

### 基础 URL

```
http://<your-server>:8123/api/v1
```

### 认证

所有 API 请求都需要在 `Authorization` 头中提供 API 密钥：

```
Authorization: Bearer <your-api-key>
```

### 请求格式

- Content-Type: `application/json`
- 请求体使用 JSON 格式

### 响应格式

所有响应均为 JSON 格式。成功响应结构：

```json
{
  "code": 0,
  "message": "success",
  "data": { ... }
}
```

错误响应结构：

```json
{
  "code": 1001,
  "message": "Error description"
}
```

## 对话 API

### 上传运行时附件

**POST** `/documents/upload`（完整路径 `/api/v1/documents/upload`）。

上传聊天、Agent 或 MCP 输入用的附件，返回运行时文件元数据。
文件存入已认证 owner 的 `<owner>-downloads` 空间；本接口不创建 dataset 文档，
不启动解析或索引。聊天取件时才由附件消费者读取并解析文件。

认证使用 `Authorization: Bearer <token>`，接受 web 会话 JWT 或 SDK API Key。
两种凭据都需要有效用户及个人 owner membership；`created_by` 由服务器确定，
不能通过请求指定 owner、storage key 或 dataset。

| 输入 | 规则 |
|---|---|
| multipart `file` | 一个或多个文件，多个文件重复使用字段名 `file`；每个字段须有文件名 |
| query `url` | 单个 HTTP/HTTPS URL，与 `file` 互斥，必须提供其中一种输入 |

例如上传两个文件：

```bash
curl -X POST 'http://localhost:8123/api/v1/documents/upload' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -F 'file=@notes.txt' -F 'file=@image.png'
```

单文件或单 URL 的 `data` 为对象，多文件为对象数组，文件顺序与输入相同。
成功体为 `{"code":0,"data":...}`，每个对象具有以下字段：

| 字段 | 含义 |
|---|---|
| `id` | 随机存储 location，供附件取件使用；不是 dataset document ID |
| `name`, `extension`, `mime_type` | 文件名、扩展名及内容类型；URL 生成 PDF 时使用 `application/pdf` |
| `size` | 实际存储字节数，PDF 修复后按修复结果计算 |
| `created_by`, `created_at` | 服务器认证 owner ID、Unix 时间秒数 |
| `preview_url` | 当前为 `null` |

缺输入、混合输入、空文件字段、不安全 URL 或失败的 URL 抓取返回 HTTP 200、
`code=101`、`message`，不返回成功 `data`。存储及其他执行失败为 `code=100`。
缺失/无效凭据、失效 owner membership 返回 HTTP 401、`code=401`。
调用方须同时检查 HTTP 状态和业务码。

多个文件中途失败时，service 对本次已写对象执行补偿删除并检查存在状态；
同时删除本批服务器描述登记，不会删除历史对象。描述登记失败也会补偿对应文件。
无法确认清理时仍返回非零业务码，并明确说明 cleanup 未能确认。
取消请求会等待正在进行的写入后补偿；这不是对象存储事务，进程被强制终止、
存储不可达时不能保证补偿完成。

URL 的初始地址、DNS 与每个 HTTP redirect 都校验；浏览器 HTTP 请求通过
DNS 绑定的转发取件，浏览器自己的其他网络流量走拒绝连接的代理。
内网/保留地址、无法验证的目的地及 WebSocket 被阻断；只转发 GET/HEAD，
不转发浏览器 cookie/Authorization，也不使用环境代理替代 DNS 绑定。
URL 模式生成 PDF 或网页 Markdown，仍需可用的 Chromium 运行资源。

MCP 聊天 `POST /v1/llm/enhanced_chat_sse` 的 `files` 保持文件 ID 字符串数组，
例如 `{"files":["上传响应中的id"], ...}`。服务器在当前认证用户的 downloads 空间
恢复上传时登记的描述，读取对象并通过 Canvas/FileService 解析；不能由客户端
`created_by`、自造描述或消息中的 URL 选择其他 owner。文字进入本轮用户消息，
图片通过现有模型 `images` 路径传递。普通/结构化、工具/无工具和非流式使用同一取件逻辑。
每次调用清空旧附件；历史上传没有可信登记时须重新上传，不能凭 ID 猜测所属用户。

附件 ID 不存在、属于其他用户、描述/对象缺失或解析失败时，聊天在发送 SSE 前返回
HTTP 400 和 `detail`。开始回答后发生模型等执行错误，SSE 发送 `retcode=500` 后终止，
不再发送 `retcode=0, data=true` 的成功完成帧；非流式执行失败返回 HTTP 500。
成功的 SSE 帧格式保持原协议。

兼容情况：`POST /v1/document/upload_info` 保留 `retcode/retmsg/data` 响应并标记
deprecated，作为独立 web 的上传迁移验收兼容入口。web `5c7a773` 已把 Explore
与 MCP 调用切换到新 URL；两条前端链路的真实跨端请求验收仍待其任务完成，完成后删除旧入口。
SDK 已有 `POST /api/v1/files/upload_info` 继续使用 multipart 字段 `files` 及
`code/data` 响应；它与新入口复用上传 service，不因路径相似而退役。

### 创建聊天会话

为指定的聊天助手创建一个新的会话。

**请求**

```
POST /chats/{chat_id}/sessions
```

**请求体**

```json
{
  "name": "New session",
  "user_id": "string"
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| chat_id | string | 是 | 聊天助手 ID |
| name | string | 否 | 会话名称，默认 `New session` |
| user_id | string | 否 | 业务侧用户标识，默认空字符串 |

**响应**

```json
{
  "code": 0,
  "data": {
    "id": "session-uuid",
    "name": "New Chat",
    "chat_id": "chat-uuid",
    "messages": [
      {
        "role": "assistant",
        "content": "Hi! I'm your assistant. What can I do for you?"
      }
    ]
  }
}
```

### 列出聊天会话

列出指定聊天助手下的会话。

**请求**

```
GET /chats/{chat_id}/sessions?page=1&page_size=30&orderby=create_time&desc=true&name=&id=&user_id=
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| chat_id | string | 是 | 聊天助手 ID |
| page | integer | 否 | 页码，默认 1 |
| page_size | integer | 否 | 每页数量，默认 30；传 0 时不分页 |
| orderby | string | 否 | 排序字段，默认 `create_time` |
| desc | boolean | 否 | 是否倒序，默认 true |
| name | string | 否 | 按会话名称过滤 |
| id | string | 否 | 按会话 ID 过滤 |
| user_id | string | 否 | 按业务侧用户标识过滤 |

### 获取聊天会话

获取指定聊天助手下的单个会话。

**请求**

```
GET /chats/{chat_id}/sessions/{session_id}
```

### 更新聊天会话

更新指定会话名称。

**请求**

```
PUT /chats/{chat_id}/sessions/{session_id}
```

**请求体**

```json
{
  "name": "Updated session name"
}
```

`messages` 和 `reference` 不允许通过该接口修改。

### 删除聊天会话

批量删除指定聊天助手下的会话。

**请求**

```
DELETE /chats/{chat_id}/sessions
```

**请求体**

```json
{
  "ids": ["session-uuid"],
  "delete_all": false
}
```

### 删除会话消息

删除指定会话中的一条用户消息及其对应助手回复。

**请求**

```
DELETE /chats/{chat_id}/sessions/{session_id}/messages/{msg_id}
```

### 更新消息反馈

更新指定助手消息的点赞或反馈。

**请求**

```
PUT /chats/{chat_id}/sessions/{session_id}/messages/{msg_id}/feedback
```

**请求体**

```json
{
  "thumbup": false,
  "feedback": "The answer is not accurate."
}
```

### 会话补全

基于指定会话继续生成回答。

**请求**

```
POST /chats/{chat_id}/sessions/{session_id}/completions
```

**请求体**

```json
{
  "messages": [
    {
      "role": "user",
      "content": "Hello"
    }
  ],
  "stream": true
}
```

### 对话补全

与助手进行对话（流式或非流式）。

**请求**

```
POST /chats/{chat_id}/completions
```

**请求体**

```json
{
  "question": "Hello, how are you?",
  "session_id": "string",
  "stream": true,
  "metadata_condition": {
    "logic": "and",
    "conditions": []
  }
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| chat_id | string | 是 | 聊天助手 ID |
| question | string | 否 | 用户问题 |
| session_id | string | 否 | 对话会话 ID |
| stream | boolean | 否 | 是否流式输出，默认 true |
| metadata_condition | object | 否 | 元数据过滤条件 |

**非流式响应**

```json
{
  "code": 0,
  "data": {
    "id": "completion-uuid",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "I'm doing well, thank you!"
        },
        "finish_reason": "stop"
      }
    ],
    "references": [
      {
        "chunk_id": "chunk-uuid",
        "content": "Referenced content...",
        "document_name": "document.pdf",
        "score": 0.85
      }
    ]
  }
}
```

**OpenAI 兼容补全**

使用已有聊天助手的 ID、API Key 和 OpenAI 风格的 `messages` 请求：

```
POST /openai/{chat_id}/chat/completions
```

`model` 必填；传 `"model"` 使用聊天助手已配置的模型，传具体模型名（例如 `glm-4-flash@ZHIPU-AI`）时必须是当前租户可用的聊天模型。`messages` 最后一条必须为用户消息；文本数组内容支持 `type: "text"`，图片等非文本内容会返回参数错误。省略 `stream` 时新路径返回非流式响应；设为 `true` 则返回 SSE。旧路径 `/chats_openai/{chat_id}/chat/completions` 暂保留，省略 `stream` 时仍默认流式，已标记 deprecated。

可在 `extra_body` 中设置 `reference: true`、`reference_metadata: {"include": true, "fields": ["author"]}` 和 `metadata_condition`。非流式引用位于 `choices[0].message.reference`；流式引用及完整最终正文位于收尾帧的 `choices[0].delta.reference` 和 `final_content`。引用切片的 `document_metadata` 只包含请求的字段。

Python OpenAI 客户端的 `base_url` 应设为 `http://<your-server>:8123/api/v1/openai/{chat_id}`，再调用 `client.chat.completions.create(...)`；客户端会自行追加 `/chat/completions`。客户端还会把 `extra_body` 中的 `reference` 等参数合并到请求 JSON 顶层；直接发送 HTTP 时也可将这些参数放在 JSON 的 `extra_body` 对象中，无需双重嵌套。

**流式响应 (SSE)**

```
data: {"id":"chatcmpl-chat-id","choices":[{"delta":{"content":"I'm"}}]}

data: {"id":"chatcmpl-chat-id","choices":[{"delta":{"content":" doing"}}]}

data: {"id":"chatcmpl-chat-id","choices":[{"delta":{"content":" well"}}]}

data: {"id":"chatcmpl-chat-id","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}

data: [DONE]
```

## 知识库 API

### 列出知识库

获取当前用户的所有知识库。

**请求**

```
GET /datasets
```

**查询参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| page | integer | 页码，默认 1 |
| page_size | integer | 每页数量，默认 20 |
| name | string | 按名称搜索 |

**响应**

```json
{
  "code": 0,
  "data": {
    "total": 10,
    "items": [
      {
        "id": "dataset-uuid",
        "name": "My Knowledge Base",
        "description": "Description",
        "document_count": 5,
        "chunk_count": 100,
        "embedding_model": "BAAI/bge-m3",
        "created_at": "2024-01-01T00:00:00Z"
      }
    ]
  }
}
```

### 创建知识库

创建一个新的知识库。

**请求**

```
POST /datasets
```

**请求体**

```json
{
  "name": "string",
  "description": "string",
  "embedding_model": "string",
  "chunk_method": "string",
  "parser_config": {
    "chunk_token_num": 512,
    "layout_recognize": true,
    "parent_child": {
      "use_parent_child": true,
      "children_delimiter": "\n"
    }
  }
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| name | string | 是 | 知识库名称 |
| description | string | 否 | 描述 |
| embedding_model | string | 否 | Embedding 模型 |
| chunk_method | string | 否 | 分块方法：naive, manual, qa, etc. |
| parser_config | object | 否 | 解析配置 |

`parser_config.parent_child` 用于启用 parent-child 分块。启用后，系统会先按普通配置生成父分块，再用 `children_delimiter` 将父分块拆成更小的子分块用于向量匹配；检索命中子分块时，会把父分块全文作为上下文返回给大模型。

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| parser_config.parent_child.use_parent_child | boolean | 否 | 是否启用 parent-child 分块，默认 `false` |
| parser_config.parent_child.children_delimiter | string | 否 | 子分块分隔符，默认 `"\n"`，仅在 `use_parent_child=true` 时生效 |

**响应**

```json
{
  "code": 0,
  "data": {
    "id": "dataset-uuid",
    "name": "My Knowledge Base",
    "created_at": "2024-01-01T00:00:00Z"
  }
}
```

### 更新知识库

更新指定知识库的基础信息、分块方法或解析配置。

**请求**

```
PUT /datasets/{dataset_id}
```

**请求体**

```json
{
  "name": "string",
  "description": "string",
  "embedding_model": "string",
  "chunk_method": "naive",
  "parser_config": {
    "parent_child": {
      "use_parent_child": true,
      "children_delimiter": "\n"
    }
  }
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| dataset_id | string | 是 | 知识库 ID |
| name | string | 否 | 知识库名称 |
| description | string | 否 | 描述 |
| embedding_model | string | 否 | Embedding 模型；已有分块时不能切换 |
| chunk_method | string | 否 | 分块方法：naive, manual, qa, etc. |
| parser_config | object | 否 | 解析配置；支持 `parent_child` 嵌套配置 |

`parser_config.parent_child` 的字段含义与创建知识库相同。设置 `use_parent_child=false` 时，会清空执行层使用的 `children_delimiter`。

**响应**

```json
{
  "code": 0,
  "data": {
    "id": "dataset-uuid",
    "name": "My Knowledge Base",
    "updated_at": "2024-01-01T00:00:00Z"
  }
}
```

### 删除知识库

删除指定的知识库。

**请求**

```
DELETE /datasets/{dataset_id}
```

**响应**

```json
{
  "code": 0,
  "message": "success"
}
```

## 文档 API

### 下载代码沙箱产物

CodeExec 生成的 Markdown 附件链接指向以下 REST 路径：

```
GET /documents/artifact/{filename}
```

完整地址为 `/api/v1/documents/artifact/{filename}`；旧地址
`/v1/document/artifact/{filename}` 暂作 deprecated 兼容入口。两者均需登录令牌或
个人 API Key。`filename` 为生成时的 32 位小写十六进制 ID，加
`.png`、`.jpg`、`.jpeg`、`.svg`、`.pdf`、`.csv`、`.json` 或 `.html` 扩展名。
CodeExec 生成的 URL 带 `run_id`；有持久化 Agent 会话时另带 `session_id`，例如
`/api/v1/documents/artifact/<filename>?run_id=<run_id>&session_id=<session_id>`。
服务端在上传时登记文件名与请求用户、运行 ID、可选会话 ID 的精确绑定；下载时检查该登记，
会话运行还要求会话存在且 Canvas 可访问。只提供一个属于自己的 `session_id`，或让助手消息
引用其他人的文件 URL，均不能取得该对象。流式链接在登记完成后即可下载，无需等待助手消息落库。
历史上未登记的裸链接无法证明归属，不提供无鉴权兜底。成功时响应为原始文件字节，
`Content-Type` 按扩展名设置，HTML 与 SVG 强制作为附件下载。
无权访问、文件不存在或文件名无效时检查非零业务 `retcode`，不能只看 HTTP 200。

### 创建文档

上传本地文件、抓取网页为 PDF，或创建空白虚拟文档。创建后不会自动开始解析。

**请求**

```
POST /datasets/{dataset_id}/documents?type=local|web|empty
```

`type` 可省略，默认为 `local`。

| type | 请求体 | 必需字段 | 成功时的 `data` |
|------|--------|----------|-----------------|
| local | multipart/form-data | `file` 或兼容字段 `files`，可上传多个 | 文档数组 |
| web | multipart/form-data | `name`、`url` | 单个文档 |
| empty | application/json | `{"name": "blank.txt"}` | 单个文档 |

`local` 可使用可选表单字段 `parent_path`，并支持 `return_raw_files=true` 查询参数返回原始文档字段。
网页 URL 会经过出网地址校验；各模式均检查数据集访问权限。失败时检查响应中的非零业务 `code`。

**请求示例**

```bash
curl -X POST 'http://{address}/api/v1/datasets/{dataset_id}/documents?type=web' \
  -H 'Authorization: Bearer <API_KEY>' \
  -F 'name=example-page' -F 'url=https://example.com'

curl -X POST 'http://{address}/api/v1/datasets/{dataset_id}/documents?type=empty' \
  -H 'Authorization: Bearer <API_KEY>' -H 'Content-Type: application/json' \
  -d '{"name":"blank.txt"}'
```

**成功响应示例**（`web`、`empty`；`local` 的 `data` 为文档数组）

```json
{
  "code": 0,
  "data": {
    "id": "document-uuid",
    "name": "example-page.pdf",
    "dataset_id": "dataset-uuid",
    "run": "UNSTART"
  }
}
```

### 列出文档

获取知识库中的文档列表。

**请求**

```
GET /datasets/{dataset_id}/documents
```

**响应**

```json
{
  "code": 0,
  "data": {
    "total": 5,
    "items": [
      {
        "id": "document-uuid",
        "name": "document.pdf",
        "size": 1024000,
        "status": "done",
        "chunk_count": 20,
        "progress": 1.0,
        "created_at": "2024-01-01T00:00:00Z"
      }
    ]
  }
}
```

### 解析文档

开始解析文档。

**请求**

```
POST /datasets/{dataset_id}/documents/{document_id}/run
```

**响应**

```json
{
  "code": 0,
  "message": "success"
}
```

### 删除文档

删除指定的文档。

**请求**

```
DELETE /datasets/{dataset_id}/documents/{document_id}
```

## 分块 API

### 列出分块

获取文档的分块列表。

**请求**

```
GET /datasets/{dataset_id}/documents/{document_id}/chunks
```

**响应**

```json
{
  "code": 0,
  "data": {
    "total": 20,
    "items": [
      {
        "id": "chunk-uuid",
        "content": "Chunk content...",
        "important_keywords": ["keyword1", "keyword2"],
        "tag_kwd": ["tag1", "tag2"],
        "position": 1
      }
    ]
  }
}
```

### 添加分块

向指定文档添加新的分块，可附加 Base64 编码图片。

**请求**

```
POST /datasets/{dataset_id}/documents/{document_id}/chunks
```

**请求体**

```json
{
  "content": "Chunk content...",
  "important_keywords": ["keyword1"],
  "tag_kwd": ["tag1", "tag2"],
  "image_base64": "<base64-encoded-image>"
}
```

**响应**

```json
{
  "code": 0,
  "data": {
    "chunk": {
      "id": "chunk-uuid",
      "content": "Chunk content...",
      "important_keywords": ["keyword1"],
      "tag_kwd": ["tag1", "tag2"],
      "image_id": "dataset-uuid-chunk-uuid"
    }
  }
}
```

### 更新分块

更新分块内容或关键词。

**请求**

```
PUT /datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}
```

**请求体**

```json
{
  "content": "Updated content...",
  "important_keywords": ["new", "keywords"],
  "tag_kwd": ["tag3"]
}
```

## 检索 API

### 检索测试

在知识库中进行检索测试。

**请求**

```
POST /retrieval
```

**请求体**

```json
{
  "dataset_ids": ["dataset-uuid"],
  "question": "What is RAG?",
  "top_k": 5,
  "similarity_threshold": 0.2,
  "vector_similarity_weight": 0.3
}
```

**响应**

```json
{
  "code": 0,
  "data": {
    "chunks": [
      {
        "id": "chunk-uuid",
        "content": "RAG (Retrieval-Augmented Generation)...",
        "tag_kwd": ["tag1", "tag2"],
        "score": 0.85,
        "document_name": "rag_intro.pdf"
      }
    ]
  }
}
```

## 助手 API

### 列出助手

获取所有对话助手。

**请求**

```
GET /chats
```

**查询参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| id | string | 按聊天助手 ID 精确过滤 |
| name | string | 按名称精确过滤 |
| keywords | string | 按关键词搜索 |
| page | integer | 页码；为 0 时不分页 |
| page_size | integer | 每页数量；为 0 时不分页 |
| orderby | string | 排序字段，默认 `create_time` |
| desc | boolean | 是否降序，默认 true |

### 创建助手

创建一个新的对话助手。

**请求**

```
POST /chats
```

**请求体**

```json
{
  "name": "My Assistant",
  "dataset_ids": ["dataset-uuid"],
  "llm_id": "glm-4-plus@ZHIPU-AI",
  "llm_setting": {
    "model_type": "chat",
    "temperature": 0.1
  },
  "prompt_config": {
    "system": "You are a helpful assistant.",
    "prologue": "Hi! I'm your assistant. What can I do for you?",
    "parameters": [{"key": "knowledge", "optional": false}],
    "empty_response": "Sorry! No relevant content was found in the knowledge base!",
    "quote": true
  },
  "similarity_threshold": 0.2,
  "vector_similarity_weight": 0.3,
  "top_n": 6,
  "top_k": 1024,
  "rerank_id": ""
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| name | string | 是 | 聊天助手名称 |
| dataset_ids | array | 否 | 关联的知识库 ID；省略或为空数组时创建空助手，可稍后绑定知识库 |
| llm_id | string | 否 | 聊天模型 ID；未指定时使用租户默认聊天模型 |
| llm_setting | object | 否 | 模型参数配置，例如 `model_type`、`temperature`、`top_p`、`presence_penalty`、`frequency_penalty` |
| prompt_config | object | 否 | 提示词配置，例如 `system`、`prologue`、`parameters`、`empty_response`、`quote`、`tts`、`refine_multiturn` |
| similarity_threshold | number | 否 | 相似度阈值 |
| vector_similarity_weight | number | 否 | 向量相似度权重 |
| top_n | integer | 否 | 送入回答生成的分块数量 |
| top_k | integer | 否 | 召回候选数量 |
| rerank_id | string | 否 | Rerank 模型 ID |

### 获取助手

获取指定聊天助手配置。

**请求**

```
GET /chats/{chat_id}
```

### 全量更新助手

覆盖指定聊天助手的配置。

**请求**

```
PUT /chats/{chat_id}
```

`PUT` 适用于提交完整配置。请求体中省略的字段不会进行嵌套合并，可能被服务端默认值或空值覆盖；只改少数字段时应使用 `PATCH`。

### 部分更新助手

只更新指定字段。

**请求**

```
PATCH /chats/{chat_id}
```

`PATCH` 会保留未提供的字段，并对 `llm_setting`、`prompt_config` 这类嵌套对象做浅层合并，适合重命名助手或只调整部分模型/提示词参数。

### 删除助手

删除单个聊天助手。

**请求**

```
DELETE /chats/{chat_id}
```

### 批量删除助手

按 ID 批量删除聊天助手，或在 `delete_all` 为 true 时删除当前用户的全部助手。

**请求**

```
DELETE /chats
```

**请求体**

```json
{
  "ids": ["chat-uuid-1", "chat-uuid-2"],
  "delete_all": false
}
```

## Agent API

### 执行 Agent

执行 Agent 工作流。

**请求**

```
POST /agents/{agent_id}/run
```

**请求体**

```json
{
  "inputs": {
    "query": "User input..."
  },
  "stream": true
}
```

## 系统 API

系统 API 使用本文档的基础 URL，即 `/api/v1`。旧版 `/v1/system/*` 路由仍可用于兼容历史客户端，并已在 OpenAPI 中标记为 deprecated；新集成应优先使用本节 RESTful 路径。

### 连通测试

检查 MultiRAG 服务是否可访问。

**请求**

```
GET /system/ping
```

**响应**

```
pong
```

### 获取系统版本

获取当前服务版本。

**请求**

```
GET /system/version
```

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": "0.9.9"
}
```

### 检查系统健康状态

检查数据库、Redis、文档引擎、对象存储和聊天服务等关键依赖的健康状态。该接口不需要 API Key。

**请求**

```
GET /system/healthz
```

**状态码**

| 状态码 | 说明 |
|--------|------|
| 200 | 所有关键依赖正常 |
| 500 | 至少一个关键依赖异常 |

**响应示例**

```json
{
  "database": {
    "status": "green",
    "elapsed": "3.2"
  },
  "redis": {
    "status": "green",
    "elapsed": "1.1"
  },
  "storage": {
    "status": "green",
    "elapsed": "5.6"
  }
}
```

### 列出 API Tokens

列出当前登录用户所属 owner 租户下的 API Tokens。

**请求**

```
GET /system/tokens
```

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": [
    {
      "tenant_id": "tenant-uuid",
      "name": "API Token",
      "description": null,
      "token": "multirag-...",
      "beta": "abcdef1234567890abcdef1234567890"
    }
  ]
}
```

### 创建 API Token

为当前登录用户所属 owner 租户创建新的 API Token。`name` 可通过 JSON body 或 query 参数传入；未传时默认使用 `API Token`。

**请求**

```
POST /system/tokens
```

**请求体**

```json
{
  "name": "My Token",
  "description": "Used by automation"
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| name | string | 否 | Token 名称，最长 20 个字符 |
| description | string | 否 | Token 描述 |

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "tenant_id": "tenant-uuid",
    "name": "My Token",
    "description": "Used by automation",
    "token": "multirag-...",
    "beta": "abcdef1234567890abcdef1234567890"
  }
}
```

### 删除 API Token

删除当前登录用户所属租户下的指定 API Token。

**请求**

```
DELETE /system/tokens/{token}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| token | string | 是 | 要删除的 API Token |

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": true
}
```

### 获取日志级别

获取当前运行时日志级别配置。

**请求**

```
GET /config/log
```

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "root": "INFO",
    "sqlalchemy": "WARNING"
  }
}
```

### 设置日志级别

运行时调整指定包的日志级别。

**请求**

```
PUT /config/log
```

**请求体**

```json
{
  "pkg_name": "core.utils.redis_conn",
  "level": "DEBUG"
}
```

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| pkg_name | string | 是 | 包名或 `root` |
| level | string | 是 | 日志级别，例如 `DEBUG`、`INFO`、`WARNING`、`ERROR` |

**响应**

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "pkg_name": "core.utils.redis_conn",
    "level": "DEBUG"
  }
}
```

## 健康检查

### 健康状态

检查服务健康状态。

**请求**

```
GET /health
```

**响应**

```json
{
  "status": "ok"
}
```

## 错误码

| 错误码 | 说明 |
|--------|------|
| 0 | 成功 |
| 1001 | 参数错误 |
| 1002 | 认证失败 |
| 1003 | 权限不足 |
| 1004 | 资源不存在 |
| 1005 | 资源已存在 |
| 2001 | 服务内部错误 |
| 2002 | 服务不可用 |

## OpenAPI 文档

完整的 OpenAPI 规范可通过以下地址访问：

```
http://<your-server>:8123/docs
http://<your-server>:8123/redoc
```

---

有关 Python SDK 的使用，请参阅 [Python API 参考](./python_api_reference.md)。
