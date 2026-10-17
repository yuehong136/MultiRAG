# Python / Go 主 API 鉴权

Python 是当前生产后端。本项对齐并行 Go 服务的主 API 登录协议，不启用混合部署或改变
Vite 的目标地址。两端使用同一启动密钥签发 HS256 JWT，`sub` 为邮件地址，`exp` 为
一天；每次请求从当前 SQL 用户状态解析身份。Go 拒绝缺少 subject、expiry、错误算法、
错误签名、停用账户和已注销账户。重新登录恢复账户的正常登录状态。

Go 使用 `multirag.secret_key`（或 Go `server.secret_key`）与 Python 相同的有效固定密钥，
支持 `MULTIRAG_SECRET_KEY` 和优先级更高的 `MULTIRAG_MULTIRAG__SECRET_KEY`；至少
32 字符。未配置时使用相同 Redis key，启动失败不能回退到本地候选或硬编码密钥。
固定配置不被 Redis watcher 覆盖。密钥不随 system/configs 或用户 profile 返回。

`authentication.enable_register` 默认 true，`disable_password_login` 默认 false。
`REGISTER_ENABLED` / `DISABLE_PASSWORD_LOGIN` 可显式覆盖为 true/false、1/0、yes/no、
on/off；无效值报错。禁用密码登录在查询/更新用户之前返回拒绝；Python `/auth/token`
也受限制。注册关闭在创建用户、租户及文件之前拒绝。Go 采用当前 Web/CLI 的
RSA(PKCS#1 v1.5) + base64 密码格式，不把解密失败当明文密码成功重试；external-only
账户不能用本地密码登录。profile 不返回密码或数据库 access_token。

Go 的 canonical 主 API 路由与当前消费者一致：`/api/v1/auth/login`、POST `users`、
`auth/login/channels`、POST `auth/logout`、GET/PATCH `users/me`、`tenants`、
`chats/{chat_id}/sessions`。配置和版本可公开读取；运行配置列表仍需鉴权。现行 Go CLI
使用的 `/v1/user/login`、`user/info`、`user/logout`、`conversation/list` 等入口保留。
CLI HTTPClient 持有登录响应头的 opaque web token，所以历史已签发的 Go token 仍经
原解析器与当前数据库 access_token 核验；新的登录只签发 JWT。

Chat session 列表按认证用户的个人租户和 chat ID 查询，与当前 Python REST 的
`_owned_chat_exists` 合同一致。Dialog 当前没有 me/team 字段；加入其他租户、normal
或 invite membership 不授予其私人会话列表权限，也不把调用者伪装成租户所有者。
当前 chat detail 已解析有效 dataset_ids，本项保留它的现有实现。没有引入新的团队
会话共享产品行为，session 写入/生成等其他入口未在本项扩展。

SQLAdmin 保留配置控制的准入条件，Go Admin 保留 superuser-only，Go 主 API 仍拒绝
superuser。拒绝分支必须 Abort，不能继续下游 handler。Go Admin 登录先检查管理员
角色，不能先更新普通用户再拒绝。两后台不新增 Cookie 双向登录；Python 副本间
SQLAdmin Cookie 一致性与主 API JWT 互认分别验证。

验收入口：`python -m tests.support.shared_secret_live INFINITY_URI EVIDENCE_DIR`。
使用自有 Infinity/MinIO/Redis、scratch PostgreSQL、随机 loopback 端口和 0600 临时
配置；`AUTH_SMOKE_MINIO` / `AUTH_SMOKE_REDIS` 可覆盖本次拥有的端点，开启
`AUTH_SMOKE_HYBRID=1` 时另启动 Go live server。Go native tokenizer 静态库需预先构建，
也可只读复用本机已安装资产；不修改依赖环境。MinIO/Redis 凭据取私有配置，不在日志或证据中输出。

API 实际执行完整资源 bootstrap、路由和 SQL 身份链，关闭 worker lifespan 以避免启动
后台消费者。分别保存两个 `make smoke` exit、真实健康项、SQLAdmin Cookie、主 API
真实加密密码登录/身份、会话所属范围、管理员边界、停用与注销、关闭注册/密码登录
以及拒绝前后 SQL/自有 Redis 状态读回。Go Admin 使用真实注册路由；另有一个仅验收
使用的下游 SQL mutation 观察点，证明普通用户拒绝时没有穿透中间件。

这不证明生产已升级、后台 worker 已启动、所有 Go 路由已完成 Python parity、独立
JWT 会话撤销或密钥轮换。没有改 Go 模型 factory/provider，没有改 Web proxy 路由。
