# 嵌入式 Chatbot 授权

`GET /api/v1/chatbots/{dialog_id}/info` 与
`POST /api/v1/chatbots/{dialog_id}/completions` 使用 `Authorization: Bearer <beta>`。
Beta token 必须唯一匹配 `APIToken.beta`，并绑定有效 tenant。
Dialog 必须属于该 tenant 且 `status=1`；同租户的其他 dialog、失效和不存在的
资源不能代替目标 dialog。

补全请求可传 `session_id` 续聊。会话必须同时满足
`dialog_id == 路径 dialog_id` 和 `user_id == token tenant_id`。
新会话的 `user_id` 由鉴权结果写入；请求中的 `user_id`、`tenant_id` 不参与授权。
历史空 owner 或其他 owner 的会话不会自动认领，需要重新建会话。
这条策略仅针对嵌入式 chatbot，不改变 Agent 会话中的外部用户标识合同。

授权在构造 SSE 响应前完成，同步与异步补全 service 在执行前重复校验。
无效 token 按统一 SDK 鉴权返回 HTTP 401、`retcode=109`；无权访问的 dialog/session
返回 JSON `code=109`，不创建 SSE、会话或调用模型。拒绝日志仅包含
鉴权 tenant 与资源 ID，不记录 token、问题或请求声称的用户身份。

新会话保留现行 prologue 帧格式；非流式新会话的 `data` 仍为 prologue SSE 字符串。
续聊在提交前复制 JSON 消息和引用列表，保证 SQLAlchemy 能持久化本轮内容。

验证入口：

- `tests/unit/test_chatbot_access.py`：两个 service 的拒绝、执行前重验。
- `tests/integration/test_chatbot_authorization.py`：真实注册 HTTP 路由、SQL token
  校验、跨租户及会话拒绝、独立连接读回。模型回答使用受控边界，无真实供应商调用。
