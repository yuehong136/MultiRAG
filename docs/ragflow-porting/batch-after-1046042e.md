# 1046042e 后续 20 项本地交付

2026-10-18：20 项均已完成逐项评估与本地验收，适用改动串行合入后端 `main` 和独立 Web `master`。
本批只做本地提交，没有 push 或部署；远程 `ci-required` **待运行**。这份报告不代表完整
`make verify`、完整 core 四分片或生产验收通过。

## 范围与锁定组合

| 输入 | SHA / 约束 |
| --- | --- |
| 上游起点，不包含 | `1046042e01979a83fc2dc807422f674da093faaa` |
| 连续第 20 项 | `7ec87f7cb78889180a1cd67d46d10a247977ae7e` |
| 上游冻结上限 | `519e7d98a5651564d4e35d6648f006cba4baaf4f` |
| 初始后端 main | `a3ca713fd2f278e137586ac477459180353adfe5` |
| 最终代码与测试组合 | `4eda20812871a63b8debbf31e97002c6af06801f` |
| 初始 Web master | `f78ee6790640b04142cb85d696014e3ecef5de09` |
| 最终 Web master / consumer pin | `419fc9a2425efea53673bd9af4c9a7c062e2d150` |

20 项与冻结的 ancestry 顺序一致。实现按完整 diff、现有消费者和同链后修评估；冻结上限外的
后修只辅助判断方向，没有增加批次项目。最终代码组合之后的本报告、进度索引提交只改文档；
最终文档 HEAD 和精确候选映射记在外置交付清单中。

## 逐项结论与本地主干提交

表内提交均为实际后端 main 提交；标注 Web 的提交属于独立 Web 仓库。没有收益的等价项不造空提交。

| # | 上游 | 本地结论与机制 | 主干提交 |
| --- | --- | --- | --- |
| 1 | `3b6eeabb` | 私有数据集授权适配。同步/异步查询共用有效数据集、明确权限和有效成员条件；普通成员只看本人 private 或 team，owner/admin 按管理合同。补齐已注册的旧详情、文档列表、过滤与元数据入口。 | `0023e9cd`、`7580cd03` |
| 2 | `ee0de582` | HuggingFace driver、目录与 factory 接入，复用聊天传输、取消与终结错误处理。目录使用本地 `url_suffix`；严格向量解码拒绝 null/尾随 JSON，保留合法零值。附带 Aliyun 名称已等价。 | `c679a982`、`d0f0622c` |
| 3 | `de2abe9e` | 已等价：活动 API 边界把 `chunk_method` 映射为 `parser_id`；Radix 单选通过 `onValueChange` 传值。保留现有 DTO 与组件机制。 | 无新增代码 |
| 4 | `f7e8c39d` | 保留全量 SQL 聚合及重复关联去重，filter 分支忽略不用的 metadata/id/ids，仍校验 type/status。补齐存储异常传播、可选关键词归一和安全的非零 REST 错误响应。 | `c0e1ede9`、`eb50dfc8`、`b2f2be18`、`1a340929` |
| 5 | `64657539` | 上游发布说明不适用；不把上游 0.25.2 发布事实写成本项目版本或交付能力。 | 无代码改动 |
| 6 | `c11650bb` | Python 活动链原有可信 actor 接线保留，service 参数改为必传；Go 同步收紧拒绝为类型化 102 响应。实际祖先/父级 HTTP 拒绝不泄漏路径，SQL 不变。 | `68c7617c`、`63c633a0` |
| 7 | `8ac14b59` | `dataset_ids`/`kb_ids` 兼容在活动 MetadataFilter 中已等价。当前批量工具没有上游对应的选中行元数据入口；现有批量动作使用 selectedDocs，单文档编辑有明确 document_ids，独立全库管理保留全库语义。 | 无新增代码 |
| 8 | `efe6d23d` | 活动 ES mapping 的 id 为 keyword，排序使用 keyword unmapped 类型；修复两个维护查询的同类默认排序。旧 text id 索引继续跳过不兼容排序，本批不迁移旧索引。 | `66e8bd9c`、`1ca16716` |
| 9 | `17d71e5d` | 结构化 rerank 响应与 TopN、Embedding Dimension、全部既有 provider 签名、CLI EMBED/RERANK、HTTP/service/NLP 一起迁移。保留本地 Query 语义；按 index 映射，严格拒绝重复/缺失/越界/非有限结果，零分有效。新增实际 CLI→HTTP→SQL/受控 provider 验收。 | `6f52a2e8`、`2a81212f` |
| 10 | `330257b6` | 没有活动旧 `/v1/system/healthz` 消费者，旧别名保持 404。正式健康端点保留 200/500 失败语义并去掉内部诊断字段。 | `9bbd50c9` |
| 11 | `f4b8f53b` | 新增 `/datasets/{id}/embedding/check`，允许既有 chunks 切换模型。检查先鉴权并用数据集 owner 的模型，释放读事务后执行阻塞 IO，按数据集集合取有限样本，严格检查归属/数量/维度/有限非零向量，使用原始平均相似度 0.9 阈值。检查不写配置或向量；Web 保存前检查，失败保留草稿。 | 后端 `4cd052f3`；Web `4ef47da`、`419fc9a` |
| 12 | `a3de8736` | 上游发布日期修订不适用，不改本项目发布记录。 | 无代码改动 |
| 13 | `57b24be6` | 完整版本引用足迹核查后，仅调整 Docker 文档，解释 `MULTIRAG_IMAGE`、实际 compose 默认值与源码配对；本地包和锁文件的 0.9.9 不跟随上游改号。 | `180fad54` |
| 14 | `7931b693` | 百度目录、factory、chat/stream/embedding/rerank 适配共享传输与严格响应模型，含 thinking 映射和实际目录加载。附带 OpenRouter 名称已符合本地契约。 | `ea4a811d` |
| 15 | `78208478` | S3 先完整廉价枚举，再按需 GET；GET 派生 ETag 经 FileObj 持久化。相同完整快照保留未变及超限对象，支持本地规范及两类旧 ID；枚举/获取失败不执行删除、DONE 或 checkpoint。强制重建仍取内容。 | `fc77f4c6`、`a64c6f87` |
| 16 | `779cd838` | 百度 TopN nil/0 取全量，与 14 一起交付，避免已知缺陷中间态。 | 包含于 `ea4a811d` |
| 17 | `048ec2fc` | SiliconFlow TopN nil/0 取全量，由 9 的共享默认解析器实现，并保留专门回归。 | 包含于 `6f52a2e8` |
| 18 | `6bfe0f9a` | OpenAI embeddings 按 32 输入分批携带 dimensions，核对实际返回维度；负值、null、尾随 JSON 和错误索引拒绝，保留 Query 与零值。目录、factory 及公共管线一起验证。 | `9344e3d` |
| 19 | `6cb4bc29` | 已等价：Radix context 传播单选状态，无 cloneElement 遍历；混合 ReactNode、fragment、禁用态均由现有机制处理。 | 无新增代码 |
| 20 | `7ec87f7c` | 本地没有可建立可信用户会话的 OAuth/OIDC producer；绑定路径明确拒绝。保留 JWT/API key 优先级，不加入失败 header 后的 cookie 身份回退。补真实 HTTP/SQL 回归证明 header/cookie 组合、禁用用户与 logout 撤销。 | `3dface69` |

集成阶段另外提交 `3f57a889` 固定最终 Web 消费者、`0e296c13` 将新 embedding 消费者登记进 CI
consumer 清单、`4eda2081` 保留创建前登记的独立 embedding 临时资源记录。没有修改跳过策略或放宽断言。
F 的 `eb50dfc8` 只集成一次，D 分支中的等价 cherry-pick 没有重复应用。

## 审阅修复与质量比较

私有数据集审阅发现中央权限修复尚未覆盖六类已注册的旧入口，随后补齐并以真实 HTTP 拒绝及
SQL 零副作用验证。HuggingFace 审阅发现目录键名与本地 loader 不匹配，以及 JSON null 会变成
浮点零；修复后通过实际目录绑定和受控 HTTP 验证。

Web 审阅发现旧模型不可用、另一厂商恰有同名模型时，模糊名称归一会跳过检查并直接 PUT。
`419fc9a` 保留明确厂商/实例身份，只在唯一精确裸名时归一；跨厂商切换先检查，不兼容时零写入。
19 个相关 UI 测试、最终构建及真实浏览器保存行为通过。文档过滤保留原有 SQL 聚合收益，
不引入上游按分页 ORM 行再聚合的重复实现，也不宣称元数据聚合是常数成本。

S3 保留完整快照与删除安全边界，Go 模型复用本地实例隔离和终结流失败语义。上述严格输入、
错误传播和测试资源审计属于本地可靠性补充，区别于上游行为本身。

## 实际验证与证据复用

原始输出位于本机外置目录：

```text
/Users/xldu/.local/state/multirag/validation/20261018-batch-after-1046042e/
```

`scope.json` 冻结范围；各线 `*-final.json`、`handoffs/*-final.json` 保存完整 diff 足迹、后修、
质量比较与逐命令退出；`integration/commit-mapping.json` 保存完整候选→main SHA。
`integration/final-validation.json` 按 JUnit 节点合并补跑结果并记录源码哈希复用，重复节点不累计。

| 范围 | 结果与边界 | 证据，相对外置目录 |
| --- | --- | --- |
| Python 最终相关单测 | 最终覆盖 245 个不同节点。首轮 244 通过、1 个 consumer 清单断言失败；修复后仅补跑相关 harness，33 通过，覆盖首轮失败及 32 个重复节点。 | `integration/python-unit/`、`integration/python-unit-repair/` |
| Python 定向组合 integration | 20 个不同场景完成。首轮 17 通过、3 个 embedding fixture 初始化错误；Milvus 在期间重启。恢复后同文件 4 项全部通过，包含 1 个重复节点。使用 REQUIRE_SERVICES=1，无缺服务 skip。 | `integration/python-integration/`、`integration/python-integration-repair/` |
| Python 静态检查 | 改动 Python 文件 Ruff/format、5 个相关文件 mypy、8 条 import contract、async DB guard 和 diff 检查通过。两个最终测试支持文件另做定向 Ruff/format；生产静态输入未变。 | `integration/python-static/manifest.json`、最终补充退出记录 |
| Go 最终组合 | `9344e3d` 上 7 个相关包 race：1332 个通过的测试/子测试事件；13 个已登记的无关 live opt-in 未验收。新增 inference SQL 测试实际执行。全部 internal build/vet、三个 main 独立构建退出 0。最终 411 个 Go/目录/锁文件/检查脚本输入哈希一致，后续仅 Python/Web/文档变化。 | `integration/go-combo/manifest.json`、`audit.json`、`tests.jsonl` |
| Go 实际业务链 | CLI→实际 HTTP listener→scratch PostgreSQL→两个受控 provider，检查 SQL 绑定和返回契约；目录 loader、实例绑定和批量维度另有受控服务验收。 | `A/`、`B/` 的命令与资源清单 |
| ES | Elasticsearch 8.11.3 自有容器上 10 项真实集成通过；14 个精确测试索引和容器删除后读回不存在。受影响 30 单测通过，33 个未改 harness 结果复用。 | `G/resource-readback.json`、`G/owned-resource-events.jsonl`、G final |
| S3 / 权限组合 | 145 相关单测、17 个 PostgreSQL/MinIO/Milvus/授权组合场景及 4 个补充回归通过；真实 HTTP 拒绝与持久化/失败副作用独立读回。计数为该线不同执行，不与最终组合相加。 | F final、`F/cleanup-resource-readback.json` |
| Web API / UI | 239 API 测试；最终 19 个设置/元数据 UI 测试；最终 build、相关 lint、文件大小检查通过。完整 lint 为 0 错误、1366 个既有 warning。E 的 27 UI 测试及浏览器行为证据按 6 个不变输入哈希复用。 | D final、`D/input-hashes.json`、E accepted |
| 浏览器与真实消费者 | 按标注 contact sheet 审阅 E 解析器/单选及 D 最终浅深主题页面。D 浏览器受控 HTTP：不兼容累计 check=1/write=0，兼容后累计 check=2/write=1；真实后端消费者另跑实际 Web API client，最终补跑通过。 | `E/contact-sheet.png`、`D/settings-contact-sheet-final.png`、`D/browser-acceptance.json`、最终 integration 报告 |

首轮失败均保留红色原始报告，没有改写成通过。consumer 清单漏登由严格 inventory 断言发现并修正；
Milvus 重启的只读诊断保存在 `integration/milvus-diagnostics/`，没有重启或替换共享服务。
父 fixture 会重写资源记录的问题已修复，新记录独立保留集合、模型、chunk ID 和清理结果。
两轮 integration 的 SQL 库、对象桶、端口及自有集合均有新连接独立读回不存在的证据。

额外扩大 mypy 范围时，C 的 `move_files` 一项和 F 的 `utils.py` 三项既有错误均在隔离基线复现；
没有修改无关实现或忽略规则来换绿。最终纳入本批的定向检查通过，仍由远程最终组合 CI 把关。

## 清理与未验证边界

八条工作线均已外置证据并清理自有工作树、监听进程和临时资源；本批分支在核查主干包含或
patch 等价、无工作树引用后精确删除。集成工作树在报告和进度索引入实际 main 后归档，
外置 `integration/cleanup-readback.json` 与 `integration/deleted-branches.json` 记录最终结果。
SDK、ironwood 等无关分支、工作树和既有服务保留，没有全局 prune/down。

初始 28 个未提交路径保留；其中 PROGRESS 的原有 delta 不入本批提交，只添加本报告索引。
其余文件字节哈希不变，PROGRESS 去掉本批索引后与初始快照完全一致。证明见
`integration/wip-preservation.json`。提交之后的实际工作目录仍含这些原有 WIP，测试针对锁定的已提交组合。

真实云模型、真实 AWS/S3、生产身份供应商及生产切换未验收。S3 集成关闭自动解析，不能据此声称
worker/解析生产链通过；ES 旧业务索引未迁移。embedding 比较是有限样本兼容建议，直接 PUT
按上游行为仍允许切换且不重建旧向量；浏览器保存流程会先检查。桌面浏览器受控 HTTP 证据与
真实后端 API 客户端证据分别列示，没有移动端或完整端到端浏览器生产验收结论。

Web 最终 consumer pin 已提交，但两个仓库均未 push，远程 CI 尚不能据此完成拉取和验证。
最终远程组合须通过适用的 `ci-required`，本地定向通过不替代该结果。
