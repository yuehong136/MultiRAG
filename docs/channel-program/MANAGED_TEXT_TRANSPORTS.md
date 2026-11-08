# Managed 文本渠道（CHN-P15）

Discord、Telegram、LINE、WeCom 已有实际 transport 与 registry/spec/descriptor 接线；当前交付是本地代码与受控验收，尚未部署。飞书、钉钉继续使用原 transport。

## 管理与凭据

所有 provider 都通过现有 `/api/v1/chat-channels` 创建、读取、局部更新与删除，表单从 `/chat-channels/providers` 的 manifest 生成。`form.version=1`，字段仅使用既有 text、password、string_list 控件；不发布无 transport 的模板。

| Provider | 公开 credential 字段 | 只写 secret 字段 | 接收方式 |
|---|---|---|---|
| Discord | `bot_id` | `bot_token` | Discord Gateway；需在开发者控制台启用 Message Content intent |
| Telegram | `bot_id` | `bot_token` | Bot API long polling；已有 webhook 时拒绝启动，不自动删除或丢弃 backlog |
| LINE | `channel_id` | `channel_secret`、`access_token` | API 统一签名回调；出站使用 push |
| WeCom | `corp_id`、`agent_id` | `corp_secret`、`callback_token`、`aes_key` | API 统一加密回调；企业应用文本消息 |

每种 provider 都可设置 `allowed_user_ids`。新 transport 只处理文本；Discord/Telegram/LINE 群消息仍受 binding 的 `private_chat_only` 策略控制，默认只接收私聊。WeCom 当前仅私聊。四种 provider 都不声明渐进卡片、交互表单、取消或反馈控件；Discord/Telegram 支持 reply threading。它们尚无独立 `/verify` credential probe，Discord/Telegram 在 worker 启动时核对真实 bot ID。

secret 通过现有行绑定 AES-GCM store 加密，公开 config 与响应不含明文或密文。多 secret 的 PATCH 解密后合并，仅替换本次明确提交的字段；启用前检查每个必填 secret。WeCom 的账号冲突身份同时包含 corp/app，允许同一企业的不同应用。

## 回调 listener 与 worker 所有权

LINE 的 webhook URL：`https://<API-origin>/api/v1/chat-channels/callbacks/line/<binding_id>`。

WeCom 的 callback URL：`https://<API-origin>/api/v1/chat-channels/callbacks/wecom/<binding_id>`，GET 用于 URL 校验，POST 接收加密消息。必须使用管理响应中的 binding ID；绑定须已启用。反向代理/TLS 沿用 API 的部署入口，不新增每个 binding 的监听端口。

1. API listener 拥有所有公网 callback 路由，多 API 实例共用 Redis inbox。worker 不开 HTTP listener。
2. API 从数据库解析启用 binding、tenant、provider、generation，解密该行凭据；外部载荷不能选择执行目标、tenant 或发布版本。
3. LINE 对原始 body 做常量时间 HMAC-SHA256 校验。WeCom 先验 SHA-1 signature 和五分钟 timestamp，再检查 AES-CBC、32 字节 padding、消息长度、corp 与 agent；XML 拒绝实体/DTD。
4. 验证通过后，只保留有界规范化文本消息。Redis key 是 tenant/binding/generation 的哈希；请求 body 上限 1 MiB、每批最多 100 事件、队列上限 256 条、TTL 600 秒。批次原子入队，容量或 Redis 故障返回 503 供平台重试，不 trim 已确认消息。签名/内容非法返回 403，provider/binding/停用或代次不匹配返回 404。
5. worker 使用现有 binding-generation 派生 token 调用私有 GET `/internal/channel-bindings/{binding_id}/callback-inbox`，先 peek 队首，再交给既有 worker admission，结果为 ACCEPTED 或策略 DENIED 时才 POST `.../callback-inbox/ack`（`{"receipt":"<sha256>"}`）。错误 receipt 返回 409；peek 不删除消息。旧代次不能读取或确认新代次队列。停止接收、全局队列满或 follow-up 满返回 RETRY，保留 inbox 队首；这类重试不调用会 claim/终态回复的 queue-busy 分支。Telegram 同样只在 ACCEPTED/DENIED 后推进 offset，handler 异常返回 RETRY，取消继续传播。
6. 执行仍经现有 BindingBridge → 私有 execution service → Dialog/Canvas driver，复用目标授权、generation fence、幂等与独立历史；provider 不访问数据库或创建第二套历史。

inbox 是十分钟的有界接入缓冲。确认边界是 worker 内存接纳，之后进程崩溃恢复、跨实例恢复及 exactly-once 仍属于挂起的 CHN-O14；满队列、停机中的拒收与 provider 重试不能被解释为持久运行队列保证。旧代次队列自然 TTL 过期，不迁移到新目标。Redis 应继续使用受保护的服务网络；该 inbox 包含短期消息正文，不进入日志。

## 生命周期与交付

Discord 登录并等待 ready 后才完成启动；Gateway 终止由通用 worker monitor 判错。Telegram 启动先调用 getMe/getWebhookInfo，轮询失败有限退避后停止；message ID 加 chat 前缀，避免不同聊天的同号消息被去重。LINE/WeCom 启动先验证当前私有 inbox 可读。HTTP/SDK send 失败显式抛分类错误，不当作已交付；文本按平台字符/字节限制分段，Discord 禁止自动提及。

现有私有 runtime DTO 没有新增字段。本批发布顺序为安装锁定依赖 → 新 API（callback/inbox 可用）→ 重启认识新 registry 的 supervisor/worker → 配置平台 callback。Web 使用兼容的 manifest 加法。回滚前停用本批新 provider 的 binding，再回退代码；现有飞书/钉钉无需迁移。该顺序仅为后续部署说明，本批不部署或重启共享服务。

验证入口为 `tests/unit/test_channel_managed_transports.py`、既有 provider/control/runtime/worker/bridge 回归，以及 `tests/integration/test_channel_managed_transports.py`。集成使用 scratch PostgreSQL、真实加密、随机作用域 Redis key 和独占 loopback listener；供应商 origin 与 Web 登录为受控边界。Web 同批记录真实页面保存、刷新和独立 SQL 解密。局部通过不代表真实供应商、桌面全矩阵、生产或远程 `ci-required` 通过。

实现参考冻结上游 `b5a426e6e091867fa27f8c9e1c59618749cf06d4` 的协议行为，并保留本仓既有控制面与历史机制；没有复制上游嵌入 API 的 bootstrap、plaintext CRUD、固定端口共享表或 MD5 会话算法。后续上游具备等价的安全边界时重新比较这些适配层，不建立平行 Channel 执行引擎。
