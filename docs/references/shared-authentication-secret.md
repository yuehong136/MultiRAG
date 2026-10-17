# API 与管理后台的共享签名密钥

Python 的 JWT、API SessionMiddleware 和 SQLAdmin 认证 Cookie 在启动时使用同一份
`common.resources` 签名密钥快照。资源必须在这些 signer 构造之前初始化；lifespan 太晚。
`common.settings.SECRET_KEY` 继续作为资源 facade，不另建第二个缓存或请求级 Redis 查询。

固定密钥来源按当前配置体系解析：标准
`MULTIRAG_MULTIRAG__SECRET_KEY` > 有效的 `MULTIRAG_SECRET_KEY` 兼容别名 >
外部配置覆盖 > local 配置 > service 配置。外部/local 覆盖仍按顶层 section 整体替换。
密钥是原始字符串，即使环境值看起来像数字也不做 YAML 数值转换。

- `multirag.secret_key` 使用 `SecretStr`，模型 repr/JSON 不显示明文。
- 固定密钥至少 32 字符。空配置或历史短占位值继续使用共享 Redis 回退；
  短兼容环境别名不遮盖 YAML 中的有效固定密钥。标准环境变量显式留空表示未配置。
- Redis 回退使用 `multirag:system:secret_key`，仅缺失时生成候选值，通过 SETNX 创建，
  不设 TTL。竞争失败必须读取实际写入者；键反复消失或 Redis 故障时失败，
  不使用未持久化的本地候选值。消失重试有界，不递归。
- 仅创建者提示使用自动密钥；正常读取及竞争失败者不重复提示，日志不含密钥内容。
- 进程运行期间保留初始化快照，避免 Redis 驱逐触发请求级自动换钥。
  Redis 丢失后新进程无法恢复旧进程内存中的密钥；多副本长期一致需要有效固定配置
  或可靠持久化的 Redis。配置变更是部署变更，本项不实现运行时自动轮换。

本项不改变 JWT 格式、Cookie 的授权用途或用户权限。跨 Python/Go 的 token 格式、
管理 Cookie 边界、登录开关与会话归属由混合鉴权项另行审结。

验证分三层：配置/失败路径单元测试；真实 Redis 自有随机 key 上的多进程竞争和删除读回；
两个独立、绑定当前源码的 API 进程验证 JWT 与 `multirag_session` 双向签名互认、篡改拒绝、
无 Cookie 的区别及进程/端口清理。签名 HTTP probe 使用固定测试密钥和合成 subject，
不替代真实账户权限或 SQLAdmin/Go Admin 跨后台授权验收。


完整资源验收入口为 `python -m tests.support.shared_secret_live INFINITY_URI EVIDENCE_DIR`。
为脚本准备自有 Infinity、MinIO、Redis；`AUTH_SMOKE_MINIO` / `AUTH_SMOKE_REDIS` 指定
本任务拥有的端点，MinIO/Redis 凭据取私有配置。它创建 scratch PostgreSQL、独立
配置和两个真实 API 进程，执行 make smoke 及真实 SQLAdmin 登录/Cookie 互认；关闭
worker lifespan。日志、源文件 hash、清理读回写入 evidence；最终删除私有配置、库和桶。
