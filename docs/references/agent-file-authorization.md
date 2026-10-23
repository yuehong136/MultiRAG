# Agent 运行文件与数据库连接探测

`POST /api/v1/agents/{canvas_id}/upload` 要求单个 `Authorization: Bearer …`。
Web JWT 和普通 API token 使用现有 Principal 鉴权，并检查路径 Agent 的所有者或
已加入且有效的 TEAM normal/admin 成员权限。文件写入当前已认证调用者的运行空间，
不使用请求中的 `created_by`、`user_id` 或 Agent 所有者来替代调用者。

Share/Widget 在同一上传入口显式传入其 beta token；只有唯一匹配的有效租户才可使用，
且该租户必须拥有路径 Agent。beta 只在这个上传依赖中接受，不成为通用 Principal。
缺失、重复、多段、无效、歧义或跨所有者凭据在抓取网页、写文件和启动运行之前拒绝。
身份与授权所需 SQL 读取仍会执行。

上传复用 `FileService.upload_infos`：文件和 URL 二选一，保留单文件对象与多文件有序列表
响应。成功对象和上传描述符一起持久化到已认证命名空间；中途失败或取消会等待在途写入，
补偿本次已创建的对象，并检查清理结果。不会删除历史对象。

`GET /api/v1/agents/download?id={upload_id}` 只接受 JWT/普通 API token，使用服务端身份
恢复上传描述符，再读取该调用者自己的文件。`created_by` 已从请求合同中移除，传入它不能
选择其他人的空间。beta 不可使用此入口。生成附件仍走独立的
`/api/v1/agentbots/{agent_id}/attachments/{attachment_id}` 合同。

Agent debug 与普通/公开会话运行，在消费消息文件或 Begin/UserFillUp 文件输入之前，
从已认证上传空间恢复描述符，不信任客户端传入的名称、MIME 类型或 `created_by`。
公开运行先校验 Agent 属于凭据租户；恢复 session 先校验其 `dialog_id` 和 `source`。
外部显示用 `user_id` 不能改变文件空间或 session 归属。

`POST /api/v1/agents/test_db_connection` 保留登录权限和原有数据库类型。它先按共享 SSRF
策略解析主机，所有 DNS 结果均须为公网地址，并把校验后的 IP 交给驱动，阻止重绑定。
该用户输入入口没有私网例外。连接只执行固定探测查询；已获取的结果、cursor、连接和
engine 在成功与失败时都释放。清理失败不会覆盖已有主错误，也不会被报告为成功。
驱动错误和连接字符串不向响应或日志回显；SQLAlchemy URL 与原生驱动参数避免凭据
成为连接字符串控制字段。DB2 数据库名不接受 DSN 分隔符。

`tests/integration/test_agent_file_authorization.py` 用注册 HTTP、真实鉴权、scratch
PostgreSQL 和自有 MinIO 对象验证授权矩阵、字节/描述符、合法运行与跨 Agent/session
拒绝，独立检查 SQL、对象和运行副作用。PostgreSQL 正向探测只在测试边界把目标替换为
隔离库；真实私网拒绝仍单独验证，生产策略不放开。其余驱动的故障释放用单元测试覆盖，
不代表已连接真实 MSSQL、DB2 或 Trino 服务。
