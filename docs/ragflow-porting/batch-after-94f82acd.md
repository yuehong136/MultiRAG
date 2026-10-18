# 94f82acd 后续 20 项：本地串行集成与定向验证

2026-10-18 完成本批 **20/20**。本报告只记录本批；局部通过不代表完整门禁通过。
远程 `ci-required` **待运行，未 push**；未部署。

## 范围与冻结组合

上游 `94f82acd03a0ae82eeaae3cede57d319d9a5f792` 之后 first-parent 连续 20 项，首项 `ce2ec86b5e8b46dfcab3087d65c4d8e5869701d7`，末项 `1046042e01979a83fc2dc807422f674da093faaa`。
后修审查上限固定 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，不采用上限之外改动。上游 remote 为 `infiniflow/ragflow`；观察到的 origin/main 为 `543e16738b4883fcb7e70520e0b5849e7e18c9b7`，不将它替代冻结上限。

本地后端基线 `755dfc0d0ee92fd8ebbb8c26917ccb99f4f8db8b`，Web 基线 `53fdf0968b901daaa7d54ea6db911c409b049eaa`。
交付源码组合：后端 `ddd15f77d5d7c6c48095bd23d2ba5c49239f0a13`，Web master `f78ee6790640b04142cb85d696014e3ecef5de09`。
本报告及最小 PROGRESS 索引另以文档提交合 main；最终含文档 HEAD 和清理读回记录在外置 `delivery-complete.json`。
消费者 pin 的唯一来源 `.github/consumer-web.json` 已指向该 Web SHA（`87af3150`）。

## 逐项结论与候选到主干映射

下表 SHA 使用唯一短名；40 位上游范围见外置 `scope.json`，40 位候选→实际主干映射见 `integration/item-mapping.json` 及各线 accepted。等价项以当前基线代码、活动调用方和本次运行审计为依据。

| # | 上游 | 结论 | 候选 → 实际 main/master | 行为与质量比较 |
| --- | --- | --- | --- | --- |
| 1 | `ce2ec86b` | 等价保留 | 基线 `755dfc0d` | 用户 POST/Bearer 与 admin GET/raw token 已正确；失败保留会话，普通解析器拒绝管理列表。 |
| 2 | `a32ebf32` | 等价保留 | 基线 `755dfc0d` | 空元数据在引用消费者安全归一；保留生产者及引用格式。 |
| 3 | `4d6e8dff` | 直接移植 | `ed239bbd` → `353f7e35` | 移除 doc_ids 绕过相关度阈值的分支，保留纯词法语义。 |
| 4 | `ada6d478` | 本地适配 | `2447ea99` → `48e5e035`；`a3c77dff` → `cda29412` | 整批写前拒绝自身、后代及当前父目录自合并；显式合法重命名保留。 |
| 5 | `26d70189` | 本地适配 | `f4c9e424` → `5fa829ec` | 可信 beta tenant、dialog、session 写前与服务复验；续聊 JSON 独立复制以持久化。 |
| 6 | `7d35e40c` | 本地适配 | `90c70fb4` → `bfae57cf` | 集合检索复用共享服务，保留逐库授权、索引配对、int rerank ID 与活动 graph。 |
| 7 | `cacb7f2c` | 等价保留 | Web 基线 `53fdf096` | 活动 documents 子路由、侧栏及默认重定向已正确，无收益不换 enum。 |
| 8 | `0552b169` | 消费者适配 | `f4fbc1bd` → `f4fbc1bd`；`f78ee679` → `f78ee679` | Web 发送完整 dataset_ids 到集合入口；复用类型、鉴权及 rest200 校验。 |
| 9 | `2ad854c5` | 本地适配 | `88d94557` → `88d94557` | Aliyun 严格重排，校验每个索引/分值，保留零分、取消及实例隔离。 |
| 10 | `5e96c5ca` | 本地适配 | `3ddb5daa` → `3a7e81b0`；`21294fab` → `76d7cb41` | 普通与各 benchmark 搜索迁移集合入口；1 次 benchmark 也统计实际业务响应。 |
| 11 | `b6abce50` | 本地适配 | `925b7096` → `fbeab7b1` | Go admin ingestion tasks 与 CLI 接线，NULL 安全计数，失败立即返回。 |
| 12 | `d487a7f1` | 文档适配 | `2617aeb3` → `902d41ce` | 现有 Docker 指引补持久化证书、权限、续期、预检及限定服务变更示例。 |
| 13 | `653b00b9` | 生产等价，补回归 | `9fb0cc61` → `fffb19d6` | 保留更强 KB+connector 与历史候选机制；新增真实创建/查重/同步删除读回。 |
| 14 | `c44dc851` | 本地适配 | `d8b2c2a5` → `95e21990` | 仅带 IMAGE2TEXT 能力的 CHAT 可回退，保留 factory 限定并归一返回 mdl_type。 |
| 15 | `c4281873` | 本地适配 | `745fea5b` → `9693e3d1` | 所有 kb_ids 在模型/SQL 前 UUID 校验与快照，保留合法原始表示。 |
| 16 | `870bc593` | 生产等价，补回归 | `6200b2ff` → `b9b73d72` | 已有早期 key 回退与字段级 envelope 合并更完整，补保存/重载回归。 |
| 17 | `4f3711d3` | 等价保留 | 基线 `755dfc0d` | total/default/合并/终结事件已覆盖；保留异常与取消传播，不照搬吞错。 |
| 18 | `3234a0ef` | 不适用 | 基线 `755dfc0d` | 本地两种 README 无上游社群链接；不增加无归属链接或不存在语言。 |
| 19 | `42504fa1` | 结构等价，补文档与回归 | `d22e4de2` → `ddd15f77` | FastAPI 具名绑定无位置转发问题；活动客户端均 PATCH，保留 PUT 405。 |
| 20 | `1046042e` | 本地适配 | `197e9c34` → `3d4a355d` | None 默认加嵌套防御拷贝，覆盖拆分的 sync/async/tool/stream/CV 调用。 |

各线完整 diff 足迹、相关后修和取舍见 `handoffs/<线>-final.json` 与对应 audit/report。D 的映射说明提交 `c865aed6` → `98250b06`；C3/D6 双路由双库组合回归提交 `85c21a05`。这些配套工作不另计上游项。

## 本地验证与证据复用

证据根目录：`/Users/xldu/.local/state/multirag/validation/20261018-batch-after-94f82acd/`。
每个 integration 的 run/JUnit/execution 报告记录精确选择、排除、服务与 exit；所述最终范围报告无 failure/error/skip。不同运行包含重叠用例，以下数字不相加为全量总数。

| 范围 | 已执行的定向验证 | 原始证据索引 |
| --- | --- | --- |
| A1/10/11 | CLI/handler/router/admin/DAO race；internal build/vet；3 个独立 cmd build；真实 PostgreSQL admin HTTP/CLI、拒绝无读写及清理；实际 Go 可执行文件对 Python JWT/API-key、普通与 1×1/1×3/2×2 benchmark、拒绝与源数据读回。 | `A/*race.log`、`A/admin-tasks-integration.*`、`A/python-cli-http-final/`、`A/resource-readback.json` |
| B9 | 模型包 race 684 test/subtest pass；相关 service HTTP race 12 pass；build/vet、独立 cmd build。严格 malformed/缺失/重复/越界/零分/业务失败/取消及并发实例。 | `B/checks.json`、`B/validation-identity.json`、`integration/B-evidence-audit.json` |
| C2/3/17 | 150 相关 unit；64 格阈值矩阵包含在其中；1 真实检索 HTTP/SQL/Milvus 场；6 组生产代码离线等价探针。 | `C/unit.log`、`C/equivalence-audit.json`、`C/integration-passed/` |
| D6/7/8 | 103 unit；3 API 集成；1 实际 Web APIClient HTTP；Web API 239、workbench 13、metadata 3、路由 4；lint/build/file-size、mypy/import/async DB。 | `D/*log`、`D/integration-api/`、`D/integration-web/`、`D/database-cleanup-readback.json` |
| E4 | 49 unit；真实 SQL/MinIO 拒绝整批不变及合法移动；当前父目录补修后 2 场与 1 场显式重命名分别通过。 | `E/item4-followup-*`、`E/item4-rename-report/` |
| E5 | 40 unit；3 真实 HTTP/SQL 场内含 token/dialog/session/stream/null 矩阵与新建续聊独立提交读回；mypy/import/async DB。 | `E/item5-report/`、`E/item5-unit-final.log` |
| E19 | 35 unit；13 core 场验证 PUT 405/OpenAPI、拒绝零副作用、canonical PATCH 与预览下载授权；1 chunk PATCH 场；另 1 consumer 场实际 Web 预览/下载。 | `E/item19-*-report/`、`E/consumers.json` |
| F13 | 40 unit；17 集成包含新/两代历史 ID 三场创建→查重→更新→删除，真实 SQL/MinIO/Milvus/Redis 及独立读回。 | `F/integration-passed/`、`F/resource-readback.json` |
| G14/16/20 | 14 named fallback unit；73 credential unit；20 最终隔离 unit；246 相关回归与其有重叠；9 受控 SDK/HTTP+SQL 集成。 | `G/audit.md`、`G/20-call-boundaries.json`、`G/20-integration-report/` |
| H15 | 154 unit；14 PostgreSQL scratch SQL 场及独立源行不变/表清理读回。 | `H/unit-final.xml`、`H/integration-final/` |
| I12/18 | 33 静态断言：4 段 Bash 仅语法、YAML、挂载源/生成目标、配置路径/链接/本地品牌。 | `I/docs-check.json` |

集成者按实际交集补验：

- `85c21a05`（干净 diff）上 259 unit 与 5 个真实 HTTP/SQL/Milvus 场通过：路径式和集合式入口均同时选择两个库，文档/元数据过滤不绕过阈值，排序/总数/分页及授权模型范围保留。见 `integration/combo-{source,checks}.json` 和 `combo-integration/`。
- `5fa829ec` 上 149 个 auth/model/Agent/ask 等相关 unit 通过；CLI/admin 五包 race、Aliyun driver/service 定向 race、internal build/vet 均 exit=0。见 `integration/final-scoped-checks.json`。其后 A10 同一路径的单次 benchmark 修复按 A 的红测→完整 CLI race/vet/build→实际可执行文件 HTTP 证据接受；不是复用修前结果。
- 最终源码 `ddd15f77` 上 43 个改动 Python 文件 Ruff check/format、纳管的两个类型模块、8 条 import contracts、async DB 检查及最终 Go internal build/vet 均 exit=0。见 `integration/final-static-checks.json`。
- E19 owner 的预览/下载消费者固定于 `53fdf096`。集成者另在最终后端 `ddd15f77` + Web `f78ee679` 上执行实际 Web 预览/下载：1 passed、无 skip，业务响应/原字节/匿名拒绝与独立 SQL/对象不变均通过。D 的集合检索消费者也已单独执行。见 `integration/preview-web-final-check.json`、`preview-web-final/` 和 `E-review.json`。
- 最后两处文件移动/CLI 补修与文档退休测试都有各自修后结果；不因无关 HEAD 或最终文档变化重跑完整 verify/core。

保留了预修红测、导入前缺本地 OCR 资产、Milvus 测试读回缺 limit、错误 runner 调用及 scratch 服务初始失败日志；对应输入修正后只复跑受影响范围，未转 skip 或放宽断言。缺本机资源通过忽略的链接/标准临时服务处理，未改共享配置。

## 清理、WIP 与验证边界

主 checkout 初始 28 路径（含 PROGRESS 的既有 56 行 delta）已外置为补丁、原文件与 SHA-256：`integration/initial/`。每次主干推进均核对 WIP；最终只为本批追加最小索引，原 delta 保全，证明见 `integration/wip-preservation.json`。未 stash/reset/clean 他人改动，未触碰 SDK/ironwood 分支和工作树。
各 worker 仅在全部项接受后归档自有 managed worktree（D 的 Web 自有 worktree 移除），清理证据外置在 `handoffs/*-cleanup.json`。集成者核对无独有 patch/WIP、无活动依赖且主干包含后删除本批分支；精确结果见 `integration/deleted-branches.json`。自有集成工作树在文档合入后同样归档，最终读回在 `integration/cleanup-readback.json`。
所有 scratch 资源按所属 fixture 精确回收。组合运行的 5 个清单经 fresh PostgreSQL catalog、Milvus collection 和 listener 查询均不存在；桶/基础行在临时服务回收前由独立连接读回为空。见 `integration/resource-readback.json`；其他线见各自 resource/cleanup 证据。未全局 prune/down 或操作业务数据。

未运行完整 `make verify`、完整 core 四分片或远程 CI；`ci-required` 待运行，未 push。受控模型/SDK/HTTP transport 证明契约，不证明真实云供应商、模型质量、生产运行或部署。H 的集成使用真实 PostgreSQL 执行可移植 SQL，不宣称 live Infinity/ES/OceanBase 方言验收。G 的可选旧 zhipuai 运行库缺失，旧适配器仅静态检查；现行 LiteLLM 路径有回归。SSL 仅文档检查，未执行证书/容器/reload/restart 命令。
