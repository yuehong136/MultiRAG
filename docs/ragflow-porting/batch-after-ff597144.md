# ff597144 后二十项本地交付

冻结范围为排除 `ff5971448b8ea2bead108732f55e20c3e9a42679`，截至 `67c3e73d70ffad2287bbdae3c9154c30a14c8c7e` 的连续 20 个 first-parent 项；直接后修仅核至 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。未 fetch 或扩大批次。后端基线 `f838fc5af51529fbbcb505a9835719a229786753`，Web 基线 `98712181c9c7bf8ccf39c955d8bbab467fd77946`。

本地可执行组合为 `c6d1adfd10999ef9b34f1f3275e0f39d0f9a0f97`；Web master 和 `.github/consumer-web.json` 均为 `e72d9730f17654b0b5ed6f6f4c011c940a35545e`。唯一集成者 I 串行接收 I/F/A/M/D/G/T/W 的候选及补件；候选到主干逐文件 blob/hash 映射、首错和恢复记录保存在本机证据目录。本文之后的收口提交只增加文档。

## 逐项处置与质量比较

完整账本按每项上游路径仅计主责一次：20 项、172 条足迹；次级审查和本地补件不重复计数。每行均保留冻结补丁 hash、原处置、当前本地路径/blob/hash、接收依据和质量比较，见 [full-footprints.json][footprints]。

| # | 上游 | 本地结果及比较 |
|---|---|---|
| 1 | `76968af0` | 保留授权先于存储读取、空 bytes 与缺失 blob 区分；补 preview 缺失、空值及存储不确定性见证，失败不伪装成功。 |
| 2 | `a75ea7ba` | REST/OpenAI 统一原设置与请求覆盖；保留省略/null、合法 0、enable flags 和冲突别名语义，拒绝 bool/非有限/越界；独立 Dialog 不改保存配置，流/非流到达实际 adapter。 |
| 3 | `76fc1d54` | 复用有序同连接 Alembic，修 Docker model-first 顺序与 schema 失败继续启动；默认/strict shell 均在消费者前退出。保留数值身份和幂等补填，不恢复 MySQL/Peewee marker 或旧 launcher；未宣称整个旧初始化原子化。 |
| 4 | `c41855da` | typed verify 与服务端创建必验保留；补 modal session/mount/auth identity guard，旧请求晚回不污染新草稿。保留特殊凭据 envelope、数值选择器和库存失败状态。 |
| 5 | `e1f19f66` | Gitee 严格有界余额/metadata，合法 0/负数保留，空 key 在 HTTP 前拒绝，错误脱敏，实例 URL/region 隔离；CLI 默认 region 和稳定 field/value 展示有实际 service/CLI/SQL 见证。 |
| 6 | `dbebc66b` | 35 条 receiver/注释路径逐核保留；不为形式改名改写已有向量校验、超时、取消和 transport 隔离。 |
| 7 | `ac3964b6` | 保留 Intl URL、typed custom_model、真实代表模型探测、无写入 verify 与创建时原子再验证；有界调用/脱敏/取消强于仅 GET /models 返回 200。 |
| 8 | `c0e00a7f` | 模板三条 Agent 分支经 VariableAggregator 汇入单一 Message，清固定模型/KB ID，统一 runtime/UI selector；实际鉴权创建、编辑、SQL/version/Redis 读回，按租户绑定。 |
| 9 | `473d06d1` | 保留单模型接口，增 1..100 批量声明、完整 capabilities、合法 0 和实例锁内整批回滚；保留 Moonshot reasoning/严格 raw/SSE，不只取首项能力。 |
| 10 | `11347699` | 更新 Python/Go 两套 Cohere 目录并保留旧身份；Python 29 项但无 ASR driver，故不宣传该能力；Go 19 项及真实 speech2text 接口由当前目录 loader/driver 分层验证。 |
| 11 | `a678ed7b` | chunks/documents 已在 page-size 变更时回第一页；补 logs 显式新 size 重置，省略 size 的翻页继续沿用，保留 filters/route owner 隔离。 |
| 12 | `d6fc50a4` | 按实际 Web/SDK/MCP 消费者保留 retrieval 和父资源绑定的单 session 操作，退无消费者批量路由；JWT/API 混合鉴权拒绝重复/歧义凭据，非法 JWT 不回退。 |
| 13 | `d736f358` | 大部分许可证足迹有据保留；Zhipu 采用原始 ID，严验空白 key、畸形/重复 discovery，保留 URL/default/region 隔离，不引入重复 BaseModel/全局配置。 |
| 14 | `b3631469` | 复用一次向量维度探测，去二次 encode 和失败回落 768；标题进入限流/线程边界，RAPTOR checkpoint 仅更新非终态进度。保留 cancel drain、模型 ownership、SQL/index 后置提交和 title-weight=0；CLAUDE 仍映射 AGENTS。 |
| 15 | `2061edd3` | 退四条无消费者旧 LLM 管理路由及孤立 catalog/DAO/service；保留活动入口与 SQL 兼容模型。补 admin 数字默认、实际 retrieval/Memory Create 及 DeleteUser 现代凭据清理/回滚/late-provider fence；public Register 和 Memory Update 不扩范围。 |
| 16 | `ae316b34` | orphan source 允许展示名 rename，只更新准确 index；无源 parser/ingest 仍拒绝，失败恢复名称，保留 source/parser 与 presentation 分层。 |
| 17 | `2eed0d46` | 必需接口保留包装后的 ErrUnsupportedCapability，可选接口保持缺席；不恢复 panic 或把 unsupported 变 nil 成功，保留 Mistral OCR/XunFei 真实探测。 |
| 18 | `b946df8b` | beta 保持 tenant-only，不提升 owner；mindmap/search/persondata 先核 bot/KB/document，再触发模型、SQL 或存储，拒绝副作用独立读回。 |
| 19 | `df55880b` | Google connector OAuth 绑定 user+source/connector+PKCE，state 单次原子消费；SQL 授权先于精确 CAS 消费，保留临时失败重试，拒绝回放/跨账号，postMessage 安全。受控 TLS/OAuth 验收不代表真实 Google 账号。 |
| 20 | `67c3e73d` | Go 批量文档 DELETE 保留显式 ids/[]、授权、取消 drain、可重试清理和全量锁后重验；Python 活动 mapping 写入者补 Doc→KB→sorted File 锁与 actor 重验、原子新行/计数，SQLAdmin 的 File2Document create/edit/delete 入口关闭（5 个 method/path 组合已鉴权拒绝）。两侧并发用例及同一锁序核对，未执行两栈进程同时对打。保留现有较新 GORM/PostgreSQL 依赖，不加入无消费者 SQLite。 |

## 最终本地验证

Python/FastAPI/SQLAlchemy 是活动生产后端；Go 是停滞并行实现，以下证据分开报告。测试在隔离的已提交组合运行，原主工作树 WIP 不纳入验收。

| 层 | 实际结果与证据 |
|---|---|
| Python 静态 | 改动文件 Ruff/format、当前 mypy、8 个 import 合同及 async DB 检查全部 exit 0。 |
| Python unit | 745 passed，0 failure/error/skip；显式 client fixture 顺序探针另有 24 项通过。 |
| Python integration | 82 passed，0 failure/error/skip；REQUIRE_SERVICES=1，实际 PostgreSQL TCP 127.0.0.1:5432、Redis、MinIO、Milvus，覆盖完整 bootstrap 文件、模型默认补填、鉴权、worker/取消/索引及 File 附件并发。 |
| 固定 Web 实际消费者 | e72d9730 的真实 Node 客户端对当前 Python HTTP/SQL，1 passed；provider 为本批拥有的 loopback HTTP。 |
| 当前 Go 组合 | internal build/vet 与 server/admin/CLI 三个入口实际链接均 exit 0；race 主集合 47 顶层/141 含子例，目录 1/23，模型边界 14/63，合计 62 顶层/227 含子例，全部通过、0 skip，预期顶层名称集合完全匹配。 |
| Web 候选证据复用 | 同一 e72d9730 的 48 Vitest、297 Node API、build/lint/i18n/size/库存门禁、Browser03/04 明暗交互；逐文件输入等价已核，contact sheet 已审。受控浏览器 fixture 与上行实际消费者分别记证。 |

最终报告见 [final-validation.json][validation]，原命令、原始退出码、JUnit、源码/锁/配置/后端/消费者输入及首错回执均保留。Python 在 a81db947 执行；接收仅 Go 的最后两笔提交后，2198 个冻结 Python/配置/模型/锁输入逐字节相同，因此不重复执行。Go 则在 c6d1adfd 新冻结 725 个仓库 Go 文件，包含 D/G/M 的最新交集。

native 的 133 个输入与借用 archive/hash 完全匹配，当前三个入口重新链接；未重建 native archive。合法 0、显式 []、拒绝路径无禁止副作用和失败后 SQL/资源读回没有通过放宽断言或 skip 换绿。

## 保留失败与边界

首轮生成参数 fixture 因动态请求 client 的安装顺序，出现 604 pass/141 fail；只补显式 fixture 依赖，未改业务鉴权。首轮组合的私有运行根缺 mapping.json、evidence 目录等 setup 失败也原样保留，补齐登记输入后复跑。迁移 identity rollback 用例改为实际 revision，并保留 provider destructive downgrade 拒绝见证。其他 owner 的模型离线缺失、overlay/import、格式、取消、Web 旧请求晚回等首错及修复回执都未覆盖。normalizer 首轮 59467f10 拒绝 3 条原已声明缺席的 D Go 路径；修正元数据处理后 e0eb7cb9 生成 20/172，原失败及输入保留。

M 的初次 replica/lock 两键原写入前未预登记，formatter 也遗漏 --no-cache 产生自有 .ruff_cache；这些是历史协议偏差，不能以补登记冒充事前登记。精确身份、原发现/清理和最终不存在读回均保留；资源读回通过不会改写为严格事前登记全部合规。首轮 app 导入缺少单独 OCR 借用记录、离线阻止下载的事实也保留。

## 恢复、清理与原 WIP

独立只读资源审计已通过（passed-with-documented-deviations；strictProtocolPassed=false）。逐名检查 55 个 scratch DB、76 个 bucket、222 个 Milvus collection、1132 个外部 Redis key、558 个 listener 地址及所有登记进程/自有路径，均无剩余自有资源。原始 230 个 canonical run（含失败/取消）保留；三个 M 历史登记偏差仍明确不合规。读回脚本不删除或终止任何资源；其自身进程组/tmp 的结束另由 canonical finished 回执确认。见 [独立读回](/Users/xldu/.local/state/multirag/validation/20261028-batch-after-ff597144/integration/final-resource-readback-v8.json)，后续最终主干/WIP 及文档接收回执见本机 delivery-complete.json。

八棵候选树 I/F/A/M/D/G/T/W 的精确 checkout/ref/admin 均已移除；恢复 bundle 保留，已有实际隔离对象库导入/恢复与源码 hash/clean 证明，I 另对完整 c6d1adfd 代码包及其候选分支实际恢复。I 自有 Go cache/native 副本已清，借用 archive、模型、.venv、node_modules 和基础服务保留；借用目录仅主张保留，具名文件按原 hash 核对。

原 28 项 WIP 保留：27 个文件逐字节不变；PROGRESS 仅加入本批顶部索引，剥除此块后与原快照逐字节相同，原中部 hunks 未暂存。接收与清理未 reset/stash/broad clean。

这是本地定向交付，未执行全量 unit/core、干净重装、常驻 worker、真实付费供应商、真实 Google 账号、部署或生产验收。Admin DeleteUser 原有 first-owned-tenant legacy 级联范围和 Warn/continue 仍保留，不宣称完整 legacy 多租户级联重写。未 push、未部署，远程 ci-required 待运行；不同 SHA 的最终 CI 不能由本地定向结果代替。

[footprints]: /Users/xldu/.local/state/multirag/validation/20261028-batch-after-ff597144/full-footprints.json
[validation]: /Users/xldu/.local/state/multirag/validation/20261028-batch-after-ff597144/integration/final-validation.json
