# EIM-O5 / CHN-O16 secure leave 本地全链路运行手册

> 本页是验收编排、回滚和证据手册，不是日常产品启动器。正常源码启动请使用
> [LOCAL_SECURE_RUNTIME.md](LOCAL_SECURE_RUNTIME.md)：MultiRAG 读取
> `configs/local.service_conf.yaml`，of_mcp 读取自己的 ignored `.env`，新增 MCP 服务不需要复制一套
> leave E2E 脚本。

本文是以下纵向切片的权威冷启动、恢复与验收入口：

```text
真实飞书用户
  -> MultiRAG Channel/API
  -> P3 短期委托令牌
  -> HTTPS secure of_mcp Gateway
  -> leave preview
  -> 显式 Ecology simulator
  -> 飞书 strict terminal v2 结果卡
```

状态以 [ROADMAP 的 EIM-O5](ROADMAP.md) 为准；涉及 Channel 进程与交互投递时同时以
[CHN-O16](../channel-program/PROGRESS.md) 记账。两条任务已于 2026-08-30 以 fresh 真实飞书会话、
双仓完整门禁和仓库外脱敏证据包标记为 `✅`；验收记录位于
`/Users/xldu/.local/share/multirag/secure-leave-e2e/evidence/eim-o5-20260830T060041Z-1847e961`。
这仍是 loopback + simulator 证据，不代表真实 OA、远程入口或生产 rollout。

## 1. 安全边界

- 只收集并预览请假信息。`create_leave_draft`、`submit_leave` 和 Ecology
  `doCreateRequest` 在整个运行期间必须保持零调用。
- 飞书消息、Channel binding、linked identity 与 published Canvas 必须已经真实存在；工具不会创建、
  猜测或自动修复这些权威数据。
- Ecology OA 必须由操作者显式选择本地 simulator。不得把 simulator 当成真实 OA authority、生产联调
  或写能力证据。
- MultiRAG 复用当前数据库；A6 replay/audit 使用独立数据库。Gateway 只检查 migration head，不自动
  迁移。
- 应用进程运行在 macOS/Linux 宿主机并只绑定 loopback；PostgreSQL/Redis 等基础设施可以复用现有容器。
- 不修改 `configs/service_conf.yaml` 或 `configs/local.service_conf.yaml`。完整部署覆盖写到仓库外，加载
  优先级固定为：环境变量 > 外部 overlay > local 配置 > base 配置。
- 本地 CA 只通过两个窄配置点信任：of_mcp 的 JWKS fetcher 与 MultiRAG 的 delegated MCP client。
  禁止 `verify=false`、全局 `SSL_CERT_FILE` 和修改系统信任库。
- 密钥、bearer、JTI、DSN、原始 user/tenant/agent id、表单正文和 Provider PII 不得进入 argv、stdout、
  异常文本、普通日志或证据包。
- 自动接管进程必须同时满足：cwd 精确属于目标仓库、argv 命中 allowlist、PID/create time/argv hash 与
  ownership record 一致。其他 worktree、其他仓库和未知命令只报告 PID/cwd，绝不停止。

## 2. 外部部署目录

选择不在任一仓库中的绝对路径。目录由 bootstrap 创建为 `0700`；秘密、配置、grant、日志和进程记录
为 `0600`。不要把该目录放进云盘或 Git 工作树。

```text
<root>/
  deployment.json
  selection.json
  secrets/
  pki/
  artifacts/
    tool-policies.json
    mcp-grants.json
  run/
  logs/
  evidence/<run-id>/
```

`deployment.json` 只能保存路径、版本、key id、公开指纹和端口；不得保存密钥值、DSN、tenant 或用户
标识。bootstrap 可幂等复用已经通过校验的密钥，拒绝部分目录、弱密钥、错误权限、符号链接和任何隐式
覆盖；它不是轮换命令，也没有顶层 `--force`。

## 3. 一次性 bootstrap

```bash
export EIM_O5_ROOT=/absolute/private/path/secure-leave-e2e
export OFMCP_REPO=/absolute/path/to/of_mcp

uv run python scripts/secure_leave_e2e.py bootstrap \
  --root "$EIM_O5_ROOT" \
  --ofmcp-repo "$OFMCP_REPO"
```

bootstrap 生成或复核互不复用的 Channel AES key、内部控制 token、P3 ES256 key、interaction payload
key、requestState key ring、A6 fingerprint ring、identity HMAC key、simulator RSA key，以及独立本地
CA、JWKS leaf 和 Gateway leaf。CA 默认 365 天，leaf 30 天；两个 leaf 都必须包含 `127.0.0.1` 与
`localhost` SAN。TLS key 只用于 TLS，不复用 JWT/RSA/HMAC key。

bootstrap 还会在 `<root>/secrets/ofmcp-a6-dsn.txt` 生成一个不进入 manifest/stdout 的本地 DSN，默认
复用当前 PostgreSQL 连接坐标但把数据库固定为独立的 `ofmcp_a6`。已有合法 DSN 只校验、不覆盖；需要
独立角色或外部 PostgreSQL 时，应在第一次 `up` 前由操作者写入该 `0600` 文件。`up` 会复用已经运行的
基础设施，缺少 `ofmcp_a6` 时才用当前本地数据库角色创建它，随后显式执行 migration；不会启动、停止或
接管仓库外的数据库/Redis/MinIO/Milvus 容器。

默认端口是：

| 进程 | 地址 |
|---|---|
| MultiRAG API | `http://127.0.0.1:8123` |
| JWKS publisher | `https://127.0.0.1:9277` |
| secure Gateway | `https://127.0.0.1:8765/mcp` |
| Ecology simulator | `http://127.0.0.1:18765` |

端口已被未知或非目标仓进程占用时必须 fail closed，不能自动 kill 或静默换端口。需要改端口时在第一次
bootstrap 显式传入新的部署端口；已有 manifest 的端口漂移会被拒绝。

## 4. 发现并冻结现有权威

```bash
uv run python scripts/secure_leave_e2e.py discover --root "$EIM_O5_ROOT"
```

discover 只读验证：健康飞书 Channel、唯一企业 Provider link、active linked identity、当前 binding
generation，以及已发布且在同一个 MCP entry 中精确引用目标 secure MCP server 与
`leave_preview_leave_form` 的 Canvas revision。

- 恰好一个候选时，工具原子写入 `selection.json`。
- 多个候选时，只输出不可逆 opaque ref。操作者必须把明确选中的 ref 写入 `selection.json`；工具不得按
  第一条、最近一条或显示名猜测。
- 零候选、选择漂移、revision/generation 过期或身份歧义都 fail closed。

## 5. 生成 policy、grant 与 Stage overlay

先看 dry-run，再显式应用：

```bash
uv run python scripts/secure_leave_e2e.py prepare --root "$EIM_O5_ROOT"
uv run python scripts/secure_leave_e2e.py prepare --root "$EIM_O5_ROOT" --apply
```

apply 的固定顺序是：

1. 让 of_mcp 在仓库外生成 secure policy snapshot；
2. 复核 `leave_applicant = feishu + provider_tenant + user_id` 的真实部署 binding；
3. 生成只允许当前 tenant/platform user/published agent revision、目标 HTTPS
   server/resource/audience、Streamable HTTP 与 `leave:read` 的最小 grant；
4. 原子发布后，通过 MultiRAG 生产 loader 重读 policy/grant 并复核 canonical revision；
5. 生成完整的 `api-stage-a.yaml` 与 `api-stage-b.yaml`。两者只有 interaction producer 开关不同。

直接 `prepare --apply` 要求五个受管应用进程都已停止，防止活着的 Stage B producer 在 policy/grant
原子替换时继续发请求。运行态升级统一走 `up`，由它先关闭 producer、切 Stage A 并 drain consumer。

`credential_generation` 只在 grant 的语义内容变化时单调递增；完全相同的 prepare 必须复用现有
generation，确保等待表单输入期间重启不会使已发出的委托失效。旧 generation、旧 Canvas revision、
错误 audience/scope/transport/server 或身份歧义必须 fail closed。

## 6. doctor 与启动

离线检查不接管进程，也不发网络请求：

```bash
uv run python scripts/secure_leave_e2e.py doctor --root "$EIM_O5_ROOT"
```

完整启动：

```bash
uv run python scripts/secure_leave_e2e.py up --root "$EIM_O5_ROOT"
```

`up` 是有补偿动作的两阶段状态机，顺序不可交换：

1. 盘点进程/端口；若发现同一部署的旧 runtime，先停 Stage B API，再拉起 Stage A API，确认 ready 后
   经 supervisor drain workers，最后停止旧 Gateway/simulator/JWKS/Stage A；
2. 启动或复用基础设施，执行 MultiRAG additive migrations 与独立 A6 migration；
3. 重新 discover，生成并复核 policy/grant；
4. 启动只读取 P3 公钥清单的 loopback TLS JWKS publisher；
5. 用 Stage A 启动 API：delegation 开启、interaction producer 关闭；
6. 启动显式 Ecology simulator；
7. 启动 TLS secure Gateway，验证 PRM、JWKS、bearer 与授权后的 `tools/list`；
8. 通过仓库 supervisor 脚本重启 supervisor/workers，并确认当前 generation 声明 interaction
   delivery capability；
9. 用 Stage B 重启 API，最后才打开 interaction producer；
10. 运行 online doctor。

随时可读取不含秘密的机器状态：

```bash
uv run python scripts/secure_leave_e2e.py status \
  --root "$EIM_O5_ROOT" --format json
uv run python scripts/secure_leave_e2e.py doctor \
  --root "$EIM_O5_ROOT" --online
```

online doctor 的 check ID 是稳定接口，至少覆盖文件/证书、端口与 ownership、两库 migration head、
A6 durability/append-only trigger、policy/grant revision、Channel generation/identity/Canvas/binding、
API/JWKS 字节一致性、Gateway PRM/live/ready/authorized tools、唯一 leader/WebSocket/heartbeat/interaction
capability、leave 写工具关闭、simulator 写调用为零和 A6 ledger 可读性。跨仓 trace、精确 audit stage pair
与 simulator 六个只读端点的正向调用证明属于 fresh live probe，不由健康检查冒充。

## 7. 停机、失败补偿与恢复

```bash
uv run python scripts/secure_leave_e2e.py down --root "$EIM_O5_ROOT"
```

无论主动停机还是 `up` 中途失败，补偿顺序固定为：

1. 先以 producer disabled 重启 API，阻止产生新的 interaction；
2. 让 supervisor 优雅停止 workers；
3. 依次停止 Gateway、simulator、JWKS publisher 与 API；
4. 保留数据库、audit ledger、policy/grant 和证据，不自动删除或回滚数据。

Channel worker 只能经 supervisor 停止。超时后也只能升级终止 ownership record 已证明的同一 process
group。若 PID 被复用、create time/cwd/argv hash 漂移或端口属于其他进程，恢复动作立即停止并报告，
由操作者先处理冲突。

等待输入期间的恢复演练只能使用 `probe --restart-waiting-runtime`。它先从数据库证明当前 probe 内恰有
一个未过期的 `awaiting_input` interaction、一个已投递且仍 open 的七字段表单、零 callback receipt 和
零 resume job，然后固定执行 `Stage B API stop → Stage A API start → supervisor stop/start → Stage A API
stop → Stage B API start`。Gateway、JWKS 与 simulator 前后运行记录和 simulator counter 必须逐字节不变。
六段 stop/start 的完整 PID/create-time/PGID/cwd/argv hash/repo state 写入 append-only segment ledger，
API 记录还必须携带实际 Stage 与外部 overlay SHA；证据验证器会重放固定顺序，绑定部署制品摘要、严格
时间窗、新 PID/create-time，以及不可重启进程和 simulator counter 的前后 checkpoint。Gateway、JWKS 或
simulator 重启会废弃本次时间边界并要求重新发送提示。重复、过期或跨操作者 callback 都不得触发第二次
业务调用或结果投递。

如果这段事务被异常、中断或进程账本写入失败打断，**不要提交已经收到的表单**。下一次 `probe` 会在
online doctor 之前识别 `begin/failed` 标记：先停止任何同仓 API，恢复 producer disabled 的 Stage A，
再拉起 fresh supervisor 并复核当前 generation/capability；随后把旧 probe 归档为 abandoned，并返回
`waiting_restart_recovered_run_up`。此时运行一次 `up` 恢复完整 Stage B，再从无 flag 的新 probe 和固定提示
重新开始。失败 probe 永远不能继续封存为通过证据。

## 8. 自动 probe 与真实飞书验收

先运行无副作用 probe：

```bash
uv run python scripts/secure_leave_e2e.py probe \
  --root "$EIM_O5_ROOT" --case leave-preview
```

然后在已选择的飞书 p2p 会话发送固定提示：

> 帮我做一次请假试算，只生成预览，不创建草稿、不提交审批。缺少的信息请通过飞书原生表单向我收集。

表单出现后先不要提交，执行唯一受支持的等待输入重启演练：

```bash
uv run python scripts/secure_leave_e2e.py probe \
  --root "$EIM_O5_ROOT" --case leave-preview \
  --restart-waiting-runtime
```

命令返回 `awaiting_feishu` 且明确提示重启已验证后，再提交原表单；不要重新发送提示。结果卡出现后，
最后运行一次不带 restart flag 的 probe，完成数据库、trace、A6、simulator 与证据封存：

```bash
uv run python scripts/secure_leave_e2e.py probe \
  --root "$EIM_O5_ROOT" --case leave-preview
```

验收必须同时成立：

- 同一 p2p 会话出现七字段原生表单；
- 提交后在同一交互出现 strict terminal v2 请假预览结果卡；
- 当前飞书用户被解析为经过验证的 `leave_applicant`；
- callback claim、resume、terminal delivery 各成功一次且无 `safe_error`；
- 等待输入期间受控重启严格形成六段 lifecycle，Stage A/B overlay SHA 受证据约束，三个不可重启依赖
  前后不变；
- 草稿、审批、`create_leave_draft`、`submit_leave` 与 `doCreateRequest` 调用数均为零；
- simulator 的 applytoken/loadForm/linkage/formula/vacation/duration 六类只读调用相对 probe 基线均有正向
  增量；
- MultiRAG delegated transport 记录的两个 trace id，逐序等于 A6 两次逻辑调用的四条
  PRE_EXECUTION→OUTCOME 审计事件。

健康端点、一次 HTTP 200、历史 live 或 simulator 单测都不能替代这次 fresh 会话。

## 9. 证据包与完整门禁

每次验收生成 `<root>/evidence/<run-id>/`：

```text
run.json
checks.json
timeline.ndjson
logs-safe.json
SHA256SUMS
```

证据记录两仓实现 SHA、migration head、policy/grant revision、证书/密钥公开指纹、fixture 版本和 trace
id；只保留 allowlist 脱敏投影。它必须排除 bearer、JTI、DSN、原始 user/tenant/agent id、表单正文、
Provider PII 和全部密钥。完成前必须重新校验 schema、文件哈希和敏感字段扫描：

live probe 开始时两仓实现必须已提交；of_mcp 工作树必须干净，MultiRAG 只允许用户既有的受保护
`configs/service_conf.yaml` 保持修改。deployment/selection、Stage A/B overlay、JWKS public manifest、
policy/grant、迁移、证书/密钥指纹、等待输入重启事务与两仓 repo state 会在开始和封存前精确复核；
任何漂移都不能给旧交互背书。

```bash
uv run python scripts/secure_leave_e2e.py evidence verify \
  "$EIM_O5_ROOT/evidence/<run-id>"
```

两仓最终门禁：

```bash
# of_mcp
uv run ofmcp verify
uv run ofmcp contract diff

# MultiRAG
make fix
make verify
REQUIRE_SERVICES=1 make integration
make smoke
make mcp-compat
```

预期 MCP 工具契约零漂移。若出现 breaking 漂移，必须暂停并取得明确批准，不得自动刷新 snapshot。

## 10. purge 与后续路线

v1 不在任一应用进程内运行 purge scheduler。replay 正确性不依赖 purge；过期记录由 doctor 报告。
生产目标是使用独立 maintenance DSN 的外部 timer 调用 `ofmcp-a6-purge`，而不是把清理权限交给
Gateway runtime 角色。

本任务之后再分别推进：独立 migrator/runtime/purger 角色、外部 purge timer、OTel Collector、备份与
轮换演练、OCI/SBOM、production check-only migration job、KMS/Secret Manager、企业 PKI/DNS、远程
发布 gate、Kubernetes HA，以及公司网络内真实 OA 只读 canary。leave 写能力必须另立任务，并同时具备
一次性确认、业务幂等、unknown-outcome recovery 与真实 OA 权威验证。

明确不在 EIM-O5/CHN-O16 内：medic M1/M2、A7、真实 OA 写入、KMS、完整 Collector、远程公网发布与
Windows runner。
