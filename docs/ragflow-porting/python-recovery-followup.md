# Python 恢复密码与模型遗留检查收尾

2026-11-01，在 `f006747e8aed07d074c75be6de0b8a0b5b9630e6` 上修复
[上一批交付](batch-after-dcf623d6.md)保留的 Python 恢复凭证风险、VolcEngine 目录旧断言
和三处 provider 类型错误。当前生产入口仍是 Python，未改 Go 路由或部署方式。

## 恢复流程

四条 `/api/v1/auth/password/...` 路由保留原始图片、其余布尔响应、明文密码请求后
服务端 bcrypt 哈希及不自动登录合同。验证成功签发五分钟、host-only、HttpOnly、
SameSite=Strict、Path=`/api/v1/auth/password` 的 `multirag_py_recovery` cookie。
除 HTTP loopback 开发环境外使用 Secure；Redis 只保留随机凭证摘要。

Python challenge 和 proof 使用 `otp:py:<kind>:<user_id>`，绑定精确账户身份。
这避免大小写不同邮箱账户共享旧 lowercased OTP 状态；旧邮箱 `"1"` 标记及 Go proof
不能授权 Python。升级前尚未完成的恢复流程需要重新获取验证码，不提供旧标记回退。
只允许 active 的 local/hybrid 账户；禁用密码登录时也拒绝恢复。

OTP 比较、错误次数/锁及一次消费使用 Redis 原子操作；发送新挑战不会清除锁或覆盖
pending claim。重置先锁定账户并原子领取同客户端凭证，然后在同一异步 SQL 事务写入。
普通更新失败完成 rollback 后，以 claim 比较和原 TTL 恢复凭证；COMMIT 结果未知时
不恢复授权。只有独立、最长五秒、READ COMMITTED/FOR UPDATE 读回本次唯一新哈希
才能确认已提交。不存在客户端凭证、过期、重复或错误 cookie 均不能借用他人的验证窗口。

SMTP 发送被调用方取消时，先等待已接受的发送任务结束；送达的 OTP 继续有效。
DATA 已接受之后的 QUIT 错误不撤销挑战，SMTP 上下文仍关闭 socket。
发送失败的补偿仅匹配本次确切 challenge，保留原剩余 TTL，不覆盖较新发送。

恢复专用 Origin/CORS 层位于通用 CORS 外层，四路与 OPTIONS 均在副作用前检查。
默认同源，并保留仓库默认的 HTTP loopback `:5173` Web 开发消费者（API 必须是同名
loopback 主机）。其他跨端口同站地址需精确配置，不能使用 wildcard、null 或任意
forwarded header 授信；cross-site fetch metadata 拒绝。配置示例：

```yaml
authentication:
  password_recovery_origins:
    - https://app.example.test
```

该字段由现有 authentication extra 保留并在恢复边界严格校验，无需改共享配置类。
显式 `[]` 关闭所有跨源恢复，只允许同源。SameSite=Strict 仍要求浏览器同站；配置
跨站来源不代表支持跨站恢复。生产代理需保留合法 Host/scheme 或精确配置来源。

## 上游与当前消费者对照

核对 `0f62bfda21fbb723ee135a74a3da91e28f877759`、
`50715ba332dfdb3fba18c652be721fd5d46ca943` 和
`bf59eb77cc8b32473c5f1c83da104204231879f9` 的恢复流程及其当前本地消费者。
这些目标中的邮箱验证标记不能作为安全绑定证明；本次是同问题链的本地修复，
不宣称上游已解决。保留当前 Web 的密码和图片合同，不搬旧 RSA/base64 分支。
后修参考上限保持 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch。

固定 Web/master 与 consumer pin 为 `0833c49c643ac266aead69169f25c01af88496b3`，
其四个方法已有 `credentials: include`；两个独立 Chromium context 通过真实 SDK
访问 Python/Redis/PostgreSQL，证明另一客户端被拒绝、合法客户端成功且 cookie 删除，
原登录状态保留。Web 源码无需修改，pin 保持该提交。
VolcEngine 元数据适配已在上游 `b05d5a5228bc3d289f96b9c3b96be526401f92ea`
出现，本地旧 registry 测试应声明当前已交付的适配器，不能删除生产注册以满足旧断言。
三处 mypy 修复分别为 ORM 实例类型收窄、legacy 查询独立变量和 FastAPI handler 的
Coroutine/Response 返回类型；保留租户/管理边界及脱敏响应。

## 验证边界

回归覆盖另一客户端/旧标记/Go proof 拒绝、合法消费与重放、并发一次消费、SQL 更新
回滚重试、受控 COMMIT 结果未知/确认丢失、真实 Redis 失败关闭、Origin/preflight、
账户大小写隔离及 loopback SMTP DATA/QUIT/取消。真实网络 ACK 丢失、SMTP TLS/认证、
真实供应商、生产代理与部署不在这些本地证据内。

业务命令经注册 runner，保留配置/锁/源码/消费者哈希、原始 exit/JUnit、首败和清理
独立读回。部分 schema 的 backfill fixture 与完整 schema fixture 使用不同注册进程，
避免共享 session scratch 引入 schema 相互依赖；初次组合失败保留。
本次定向 unit 239 项、integration 48 项、独立 backfill 5 项通过，零 skip；浏览器
专项复用其中一条恢复测试挂载真实消费者，不与 48 项相加。四个目标文件定向 mypy
及当前纳管 mypy、改动 Python 的 Ruff/format、import-linter 和 async DB 检查通过。
本地结果不替代完整远程 `ci-required`；commit，不 push、不部署，不自动派发下一批。
