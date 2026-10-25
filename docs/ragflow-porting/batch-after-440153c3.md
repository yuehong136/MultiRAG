# 440153c3 后二十项集成记录

冻结 first-parent 排除 `440153c378d9166d72b30d2c9f86e81f905af230`，连续二十项至
`775ea5567948ea12783c514be12abb62defcbe63`；直接后修参考上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。本地 Python/FastAPI 生产架构保留，
Go 并行实现按指定项单独适配。原始证据目录为
`~/.local/state/multirag/validation/20261025-batch-after-440153c3/`。

源码验收组合为 `e238e987ed0f2360abf4daf35c229be64f3240c4`；实际 Web master 与
`.github/consumer-web.json` 均为 `1eae01eca90f4212dad46ee67970d93a9c616de1`。
后续仅文档提交不改变该组合的可执行输入。最终 main SHA、祖先关系及清理读回保存在上述目录的
`delivery-complete.json`；各执行线 handoff 与独立接受记录分别在 `handoffs/` 和
`reports/integration/*-acceptance.json`，不能单靠候选完成状态判断主干交付。

| 编号 | 上游 | 行为与质量处置 |
|---|---|---|
| 1 | `69326158` | 按回退后有效重落，在两条 SDK 文档下载复用受控文件名 MIME；保留生成附件 ext 和授权边界。 |
| 2 | `da4eaf9f` | 保留共享 typed metadata helper；无重复 wrapper，同步评测与异步聊天均有活动调用。 |
| 3 | `a6186244` | 保留更严格的集中 beta 鉴权，补九端点五类坏 header 的零业务 I/O 回归。 |
| 4 | `e7544562` | 可信 `@tool` 注册、严格参数/schema、原子绑定与默认/逐工具超时；接入 Base/LiteLLM/LLMBundle/CV，保留 MCP 授权链。 |
| 5 | `da112e3d` | 保留去重批量文档/元数据、owner 范围、逐记录复制及可信 KG 特例；无需重复 doc_map。 |
| 6 | `3e5b11a5` | Browser 后端与 Web 表单/注册/渲染联动；每轮 `sys.file_refs` 保留原始 ID 并在下一轮清空，`sys.files` 仍是解析内容。文件重新鉴权，URL 每跳 DNS pin，profile 按 owner 隔离并跨进程串行，取消和失败清理。 |
| 7 | `7740ec6c` | Replicate embedding 原始响应、预测轮询、来源/版本、索引/维数/有限数值及取消校验。 |
| 8 | `5a18df0f` | Avian 最小共享聊天适配、工厂和 catalog；严格区分合法空模型清单与畸形响应。 |
| 9 | `d5ba14a1` | Astraflow 严格模型发现、聊天与 18 项 catalog；不扩大到冻结范围外能力。 |
| 10 | `bd4ce390` | Perplexity 按官方 native base64_int8 解码有符号字节，严格维数、32 项分批及完整顺序还原；保留 Sonar。 |
| 11 | `9d372349` | 补 APPLE 依赖发现、显式 sstream、Join cast；采用 CMake 并行和 Apple 库链接，保留 Linux 行为与本地 OpenCC 安全修复。 |
| 12 | `85d0b46d` | 补 Magistral catalog，保留现有更完整的推理解析。 |
| 13 | `38a8bc3d` | 保留共享严格流传输与同事件 reasoning/content 顺序，定向组合回归已覆盖。 |
| 14 | `bf4864e6` | Infinity `extra` varchar/字典序列化及副本隔离；RAPTOR 按原生字段和 float32 快照校验、失败恢复，实际 2/768 维生成读写闭环。 |
| 15 | `85caad55` | 按冻结直接链 pin nginx 1.31.3；双架构官方包及三套原配置语法检查，保留 Python 镜像结构。 |
| 16 | `ce9a4425` | IMAP 多 sender 取首项并仅记录 count；保留运输错误、checkpoint、完整清单与 logout。 |
| 17 | `12a148d5` | 保留缺失/path/source 先拒绝与原子 DELETE，真库拒绝后独立读回不变。 |
| 18 | `111cdc77` | 补六处空 choices 和两处工具最终空响应 guard；facade 去除元信息后校验可见内容，保留 usage，已有 guard 不重复。 |
| 19 | `a725e114` | Xinference 可选鉴权 ASR/TTS 与后缀、Novita 整数余额单位、Moonshot 上下文/闲置连接/实例隔离及余额 CLI 鉴权；Fish 与 Dify 现有合同保留。附带 Dify 两份测试仅补 get_by_ids fake，本地批量和 KG/orphan 已覆盖。 |
| 20 | `775ea556` | 完整 diff 未改 Python floor；保留 >=3.12,<3.14、双版本 CI 和已满足的 ormsgpack>=1.6，不复制上游独有文档树。 |

A 的指定 worktree 名含 `api`，实际验证暴露模块注册取首个同名路径段的基线缺陷；
按真实包根计算模块名并补回归，路由 URL 不变。

验证应区分源码/单元、受控供应商 HTTP、真 SQL/对象/向量、真实 Chromium/Web、native、
远程 CI 和生产。未 push、未部署；最终远程 `ci-required` 待运行。nginx 包语法检查不代表
完整应用镜像或生产代理验收，也不声称冻结链之外的后续安全公告均已修复。

## 组合验收与证据边界

`reports/integration/final/` 的命令回执绑定源码 SHA、tree、锁、配置和 Web pin，并保留原始日志/JUnit。
`integration/frozen-combination.json` 另绑定改变的源码、native archive、字典和实际依赖闭包。

| 层次 | 实际证据 |
|---|---|
| Python 定向单元 | 46 个相关文件、895 个唯一用例。首次 820 通过、75 因自有 pytest 父目录缺失而 setup error；补目录后只重跑这 75 项并通过，按完整 nodeid 集合核对。无断言失败或 skip，不称单次 895 全绿。 |
| Python 静态 | 所改 Python 文件 Ruff/format、import-linter、async DB 检查、当前纳管 mypy 和 `uv lock --check` 均通过。 |
| 真 SQL/对象存储 | 27 个 core 定向集成通过；真实 Web API helper→FastAPI→SQL/Redis/MinIO 的 Browser 消费者通过，上传原始引用、保存/读取 DSL、debug/run SSE 和输出文件字节均有独立读回。外部 Browser 行为在这层受控。 |
| Infinity stable 0.7.0 | 完整专项 47 个唯一用例：44 首次通过，3 个 metadata fixture 缺少显式 URI；准备独占端点后这 3 项通过，未将缺失服务改成 skip。包含 RAPTOR 真实生成、原生快照、失败恢复及超过一万项元数据快照。 |
| 真 Chromium | 冻结组合的本地受控模型驱动真实 Chrome，执行动作/上传/下载、profile 持久与取消；证据验证临时目录、进程和监听器清理。不是付费模型效果验收。 |
| Web | C 候选全量 1899 项及 lint/type/strict/i18n/size/build；I 的原始引用 UI 改动再跑受影响 Agent/Browser 测试和上述静态/build。实际浏览器 en/zh、light/dark 保存/reload/debug/upload/run 通过，contact sheet 已审阅。C 的全量结果不称为 I 最终 SHA 全量复跑。 |
| Go | 冻结源码默认 internal race（3279 个测试/子测试通过事件、40 个已登记的外部 opt-in skip）、build/vet 和三个 cmd external relink；Go JSON 严格审计另列未执行的外部服务 opt-in。D 的真实 HTTP/JWT/SQL/CLI 专项 19 个通过事件按当前 Go/catalog/native/字典输入核对后复用；供应商 HTTP 受控。 |
| native / nginx | F 的 Darwin/Linux native、OpenCC ASan/UBSan、真实 tokenizer race 和双架构 nginx 官方包语法证据；源码/产物哈希独立复核。完整 Docker 应用镜像、生产代理和部署未验收。 |

首次 Go 默认 race 因字典路径和 MinIO 配置缺失失败；补齐后进程通过，但严格 JSON 审计发现
基线 `TestLegacyEmbeddingScratchPostgres` 意外 skip。使用自有 scratch 数据库实际运行该用例，
再次执行默认 race 并严格审计；未扩大 skip 清单。首次 Infinity 自有容器创建使用了不存在的无 `v`
标签，未启动服务；随后采用 Compose 已固定的 `v0.7.0`，失败记录保留。

最终 Web 消费者首次运行还暴露 macOS Unix socket 路径长度限制：自有 TMPDIR 太长导致 tsx IPC listen EINVAL。改用预登记的短临时目录后复跑通过，未修改消费者或产品断言。

D 最早 Python CI-auditor 回执附带的五个 Go 文件哈希较早；该回执只覆盖 Python auditor，
不用于证明 Go。最终组合已重跑 auditor，最终 Go/race/live/build 回执输入一致。

自有 Python 环境安装与锁一致的改变依赖并只读借用本机未改依赖；有效依赖闭包与锁无版本冲突。
这不是全新机器的完整 `uv sync`，未用到的可选 pyobjc frameworks 未安装。Python 3.13、完整
Python unit/core 四分片、全部历史 Go opt-in 和远程最终组合 `ci-required` 仍由对应 CI/专项完成。
URL 上传限制不等同于通用浏览器导航沙箱；同步工具超时不能停止已运行线程的外部副作用。

## 主干与资源收尾

串行合入保留 A/B/D/E/F/G 的真实候选祖先，Web C 候选及 I UI 修订进入实际 master。
主干只快进已验收组合；原有 28 项 WIP 不入本次提交。27 个文件保持原始字节，唯一重叠的
PROGRESS 保留完整原始工作内容并追加 I 所有块；index 从已提交版本构造，只纳入本批追加内容。

恢复 bundle 验证、普通 worktree/候选分支删除及本批自有数据库、bucket、容器、卷、端口、进程、
环境和 symlink 的最终独立读回见 `delivery-complete.json` 和 `reports/integration/*cleanup*`。
借用的字典、模型资产、主仓环境以及七个历史 managed worktree 均在保护清单中保留；不把历史
资源计作本批已清理。证据和恢复材料保留在外部批次目录，私密运行配置清理，不入库。
