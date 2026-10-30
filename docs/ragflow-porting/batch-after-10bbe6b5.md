# 10bbe6b5 后二十项本地交付

2026-10-30 冻结上游第一父序列
`(10bbe6b5d40bdd371df5d82c3732198ba2dde8f9,dcf623d60dde9620278a7827778eb2cea6f0c99a]`，
连续 20 项、128 条逐提交路径。后修参考上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch、未扩大批次。
后端基线 `0b99d915d2a9f3401dfecce3f804255ce7708604`，已包含此前 extractor globals
和 typed rowcount 修复。Web 基线为 `0a5f80fe312331794ffb968d23bc57e1b39880fd`。

实际 main 的可执行组合为 `4e86b1743e9993b4067f7db36dd289be54c39920`；
其后仅提交本记录、PROGRESS 索引和开发说明。实际 Web/master 与
`.github/consumer-web.json` 固定 `6bea392bee56268ecaa47ee5b4fc946d46253c19`。
最终文档提交 SHA、逐路径处置、原始输入/exit/JUnit、恢复和清理读回，以本地证据根
`/Users/xldu/.local/state/multirag/validation/20261030-batch-after-10bbe6b5`
中的 `delivery-complete.json`、`item-ledger.json`、`intake.json`、`validation.json`、
`cleanup.json`、`acceptance.json` 和 `handoffs/I.json` 为准。

## 逐项处置

128 条路径逐一记录行为覆盖、质量比较、处置、实际消费者和接受后的源码哈希；
本表按编号归纳。保留本地较强机制的路径不为形式一致制造改写。

| 编号 / 上游 SHA | 行为、质量处置与验收边界 |
|---|---|
| 1 / `64b860f7` | ES 显式 id 投影补稳定回退，保留输入不变、逻辑 id 优先、指定字段安全高亮及字面逗号文档名聚合。实际 Ask/Chat 经 ChunkService 消费；未回退到任意字段高亮或旧摘要算法。 |
| 2 / `88e4d6bd` | 保留已有 pagerank、n-hop 路径刷新和 Milvus 持久化；OceanBase 新表及增列补 nullable LONGTEXT `n_hop_with_weight`。将失效 Redis import 改为 typed lazy DDL lock 组合，失败继续上抛。真实 Milvus 读回通过；未验真实 OceanBase，增列不回填旧 extra 数据。 |
| 3 / `2f99d52f` | 保留当前注入式 typed model service，未恢复旧 string-driver API。保留完整 Go build/vet、三个链接、native/race 和汇总门禁，不采纳上游排除包的降级。 |
| 4 / `cf5cca5c` | 当前测试路径及 lazy import 已正确，保留；补超过 16 个片段的 think 流回归，保留真实 Markdown 合并阈值测试。 |
| 5 / `87b8062d` | Ask 接入鉴权、预算、作用域内引用回填、think 与流生命周期；I 在真实 cmd/router 共用 provider→chunks→Ask/Chat 依赖。保留 retrieval_test 的 kb_id 和现有 helper 所在模块；不采纳关闭 Go 测试。 |
| 6 / `a396b1ac` | owner-only Agent 会话批量删除保留业务部分成功、SQL 失败原子回滚；省略 ids 且 delete_all=true 才全删，[] 无操作，null/重复 JSON 拒绝。DB probe 只读、有界、公共 DNS 固定连接地址，真实 MySQL wire 验 SELECT 1。API key 唯一/有效租户及 DISABLE_SDK 失败关闭，JWT 路径保留。 |
| 7 / `d56aeb2f` | 文档 metadata PUT 在锁内全量替换 metadata，同时保留最新其他 parser 字段；精确 JSON 数字、0、false、[] 及失败后独立 SQL 无副作用读回通过。 |
| 8 / `9d9c2dc9` | 支持的 provider 及网址按本地实际 adapter 更新，避免仅凭目录宣称运行能力。 |
| 9 / `7c1bd9a5` | CLI 严格解析 USE API 'name' / USE ADMIN，切换整个 client、key、token、model 和 CE 状态；可选 public_key_file，历史隔离。实际重建 CLI 完成跨模式/profile 及拒绝路径 HTTP 验收。 |
| 10 / `bbc1f2ec` | Chat RAG 检查 actor/session/dialog/KB，保留完整多模态历史、引用及失败/取消无成功写入。do_refer=0 或 quote=false 抑制引用，KB 拒绝在流开始前 403。SQL 已提交后网络最终发送失败属于明确非原子边界，不称回滚。 |
| 11 / `7f4bf69f` | 生效的 Dockerfile.dockerignore 排除日志、缓存、私有 env 和本地产物，保留 .git/bin/mirror 输入。Redis 独立 builder，编译器/头文件留 builder，保留入口实际使用的运行库、pkg-config/jemalloc，分层清 apt lists；镜像验证脚本失败关闭。真实 COPY 合同、Dockerfile check 和 redis_builder 运行通过；未构建完整生产镜像。 |
| 12 / `9aa81e7c` | OCR suffix、现代/环境实例及数字身份贯穿 naive/pipeline；模板凭据留空。实际 PaddleOCR 表单保留 cloud Job URL/algorithm，Web→JWT→Python→受控 HTTP→SQL 读回通过。 |
| 13 / `093eec31` | Qwen rerank 严格校验真实响应形状和错误边界，异常响应不伪造排序成功。 |
| 14 / `38755c70` | 共享 DeepSeek 目录 9→77，新增 68 项并更新 2 个 V4 alias，其他 7 项不变；名称/别名查找忽略大小写，保留发现原始 id、顺序和数量。规范化 117 个冗余拼写后所有 alias 仍解析，跨模型冲突和失败原子性保留。真实发现 HTTP/SQL 消费通过。 |
| 15 / `c23809a4` | 成功加载的空 provider 隐藏，保留 pending/error/retry；补 portal 反馈、焦点和忙碌重复提交保护，0/false/多能力保持。初始反馈状态原已为 null；I 的初评误判已撤回，未宣称修复不存在的初始滚动缺陷。 |
| 16 / `f632bb4a` | 沿用现有 canonical backfill，补原子失败边界；未恢复过时 MySQL 迁移脚本。真实 scratch SQL 验首次/重复执行及失败无写入。 |
| 17 / `68223074` | Web 统一 api_key+endpoint_id envelope；Python 活跃 legacy ark 读取及空白 key 的 URL 编辑兼容保留。不同 endpoint 的真实固定 Web 消费通过。 |
| 18 / `899f76af` | OpenRouter 默认 URL 修正；空模型选择在 Web 零 verify/save HTTP，后端也拒绝。保留 typed 真实能力，不按名字猜测能力。 |
| 19 / `478c9846` | typed catalog 用于启动、注册、admin 和环境凭据事务；补首个 admin 创建，移除无效 creator User 参数，User/Tenant/credential 同事务。失败独立读回均为零，首次和重复成功通过。 |
| 20 / `dcf623d6` | 166 个目录差异逐项处置（95 修改、39 新增、32 移除目标），按实际 adapter 发布能力。原批次因 sample_rate=123 暂未发布两项 SiliconFlow TTS；随后单独[补修采样率合同和两项目录](siliconflow-tts-followup.md)，追加验证与接收证据，不改写原批次结果。 |

## 实际接收映射

| owner | 候选 → 实际后端 main / Web master |
|---|---|
| I Docker | `894363b7` → `894363b7` |
| C | `372e6382` → `fad18b9a` |
| D | `7471c586` → `d45db817` |
| M | `81f974b6` → `81f681c0` |
| P | `9d8ffbcf` → `105c7c6b`；首 admin 补修 `ba3af673` → `3119d1e1` |
| G | `f706ecdb` → `b0a0b215`；夹具 `a63aac61` → `4b86dcd0`；SDK 边界 `2809ed7e` → `452e5a08` |
| S | `021a7a66` → `ee6b814f`；`422800e8` → `d94523b6`；流授权补修 `b280c8f5` → `4364f415` |
| W | `6bea392b` → Web `6bea392b` |
| I 组合 | router/cmd `5916e865`，CI、pin、专项收集 `4e86b174` |

每次接收核候选路径/blob、不可变输入证据和 bundle 可恢复性；主树原 28 项 WIP 逐次核哈希。
PROGRESS 只暂存本批新增索引，原 Manual/cancel 段保留在工作树。

## 实际验证

Python 是生产后端。本批 23 文件定向 unit **760 通过、0 skip**；实际 PostgreSQL、
Milvus、Redis、MinIO 依赖在收集前就绪，`REQUIRE_SERVICES=1`，定向 integration
**67 通过、0 skip**。覆盖 parser 身份、provider/default backfill、启动/注册/admin/env
事务、GraphRAG 持久读回及发布失败、固定最终 Web 表单消费者。后者包含四种表单、空选择
400/400、16 个跨租户 404、0/false/多能力及 Volc 不同 endpoint 的独立 SQL 读回。
供应商端为受控 SDK/HTTP，不能据此推断真实账号或付费服务。

改动 Python 文件 Ruff check/format **31 文件通过**，当前纳管 mypy **156 文件通过**，
import-linter **8 合同通过**，async DB 新增违规/双 session 均为 0。最终 CI 私有配置
字段修正后，静态检查和 **38 个 CI 单测**另行补验；该 38 项与此前 unit 有交集，不相加。
CI 私有 MySQL listen 与签名配置仅在 RUNNER_TEMP 生成，报告不保留凭据值。

Go 是停滞的并行实现。本批从当前 C++ 源码真实重建 native archive，校验 PCRE2、
SIMDe 0.8.2、锁定 modules 和字典；Go 1.25.14 / Darwin arm64，CMake 4.0.3。
保留 archive、CMakeCache、compile_commands 作为编译证明；5 个 tokenizer 消费通过。
当前组合完成全 internal build/vet、server_main/admin_server/multirag_cli 三个独立链接；
受影响 race 的 **781 个顶层测试、4558 个含子测试的通过事件、0 skip**。
新增 Ask/Chat JWT/SQL、Agent/metadata JWT/API/SQL、MySQL wire probe、DeepSeek
发现 HTTP/SQL 四项消费者均实际通过；JSON 原始事件、stderr、exit 和派生 JUnit 分开保留。
重建 CLI 完成 **22 次受控 HTTP 请求**，含 4 条预期 exit 1 的拒绝路径。
二进制 BuildInfo 如实保留 `vcs.modified=true`：当时存在自有 native 链接和待提交文档；
不声称干净 checkout 产物，也未宣称未选中的历史 live 测试全部通过。

独立 Web 的最终提交运行完整 `test:ci`：**2333 通过、0 skip**
（source node 1011、Vitest 1208、desktop 93、tooling 21；清单 325 文件）。
复用 W 同一最终输入的 build、consumer TypeScript、文件体积、格式和实际浏览器证据；
lint 为 0 errors、1099 warnings。浏览器覆盖 en/zh、light/dark、390×700 与 1280×800、
空选择零请求、忙碌 Enter 零增量请求、候选 verify/save 一致、反馈可见和键盘焦点。
多截图按 contact sheet 审阅。Node 24.4.1、npm 11.5.1，锁文件未改。

Docker 使用真实 BuildKit COPY 哨兵验排除/保留合同，实际 Dockerfile `--check`
及独立 redis_builder 构建、Redis 7.4.3 版本/ldd/运行通过；结构检查的 deps 使用占位
镜像，不能替代完整 Python 生产镜像构建或入口运行验收。

运行输入用逐文件 manifest/归档绑定。Python 业务运行后只有 CI YAML 私有配置补修改变
其记录投影，该变化由最终静态/CI 单测覆盖；后续 Go 提交未改已运行 Python 业务字节。
最终 Go、Web 输入以及全部锁和 consumer pin 独立读回一致。最后文档提交不改变可执行行为。

## 清理、历史偏差与未验证项

各 owner 接收后精确移除自有 worktree/ref/实际 admin，原始日志、首错、候选 bundles
和源码快照保留。I 独立外部读回确认自有 65 个 scratch DB、10 个 Milvus collection、
35 个 Redis key、3 个 MinIO bucket 和 194 个登记 listener 均不存在；受控 ES 测试
仅创建 HTTP 夹具身份，没有真实 ES index。最终 process group、临时目录、source/ref/admin
另作独立读回。I 短运行根 397 个证据文件逐字节迁入持久证据根后移除，保留原路径映射。
借用 venv、node_modules、原模型、token、字典及其他任务资源不删除；只移除自有副本和链接。

`strictProtocolPassed=false`。I 首次登记 Web worktree 时把相对 common-dir 按后端 cwd
解析，导致预计 admin/common-dir 错误；创建后的实际读回发现并版本化修正，原 v1 和
偏差回执保留，不能追认为事前正确。C 首次源码清理因缺失 ref 的返回码断言失败，删除命令
输出未在断言前落盘；保留该缺口及后续独立状态/bundle 证明。其余原始失败包括 root
modules 链接校验、Docker v1 测试解析器及各 owner 版本化首败，未改写为成功。

本批定向本地接收完成，远程最终 `ci-required` **待运行**，CI 配置提交不等于远程执行。
未 push、未部署、未运行完整生产镜像、真实 OceanBase、付费供应商账号或常驻 worker
全平台验收；真实供应商库存和生产权限不在上述受控证据内。不自动推进下一批。
