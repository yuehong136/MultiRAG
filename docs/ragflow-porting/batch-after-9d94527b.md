# 9d94527b 后续 20 项本地交付

本批只处理冻结范围内连续 20 项，不开启下一批。Go 保持并行实现边界，Python 仍为当前生产后端。

## 范围与版本

- 上游区间：`9d94527b1d98f18b8c09a32e24f0721c9a2cc5b1`（不含）至 `d7fb4bdb4ec50db877afda7dbed8e3bf7ba526c0`（含）；后续修复观察上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
- fetch 快照 `origin/main=a55dccee4c211046b4e4d216a538bc60fae78dfd`；逐项完整 diff 的 SHA256 与 first-parent 顺序已核对。
- 后端基线 `41f526497014c7e7108f905fa15922373e44952c`；最终源码组合 `e4e59f1c7bc4d7a0cfd6c8f3c86ed6fd7896763b`（含测试夹具适配）。本文单独作 documentation commit。
- Web 基线 `170315a8b7d46b1b752b3f574e41b86738c7c2b3`，接受版本 `617be8b5f3785abff1d9fdd4d41e06ab43755c4c`，后端 [consumer pin](../../.github/consumer-web.json) 同值。
- 本地最终 main/master/ref、WIP 逆向读回、资源与分支删除状态，以本机交付单 `delivery-complete.json` 为准。未 push、未部署；远程 `ci-required` 待运行。

证据根目录：`~/.local/state/multirag/validation/20261024-batch-after-9d94527b/`。
`scope.json` 保存完整范围与 patch 哈希；`integration/candidate-actual-map.json` 保存完整候选/实际 SHA 和 25 组相等 stable patch ID；`handoffs/*-final.json` 保留各线范围证据及原始失败。


## 逐项结论

以下本地 SHA 为实际串行接受提交的短写；完整值与 Web 映射在上述机器清单。#10 无新增生产提交，#17 仅测试提交。

| # | 上游完整 SHA | 实际本地提交 | 处理与实现质量比较 |
|---|---|---|---|
| 1 | `e98f3e5c0d81528c798f77b7ab822cb56358e531` | `74045a88` | 补齐服务端可信附件回收；REST 与活动 Web 会话入口共用，保留其他会话/API4 引用，同 owner 删除串行，取消排空。存储不确定时保留 SQL 供重试。 |
| 2 | `6794ad2f70f0b738c9456be278b69582c9687cf9` | `2da03216` | 补齐 Novita embedding，复用严格索引/维度/有限值与批次校验；目录及真实实例绑定同时验证。 |
| 3 | `fe1433d1ff2e784736498743b5b86286e48f3ab8` | `a75e95ec` | 补齐 Jina chat 及目录；保留实例参数、历史/图片协议与错误传播。目标不提供的流式能力明确拒绝。 |
| 4 | `2eba2c4d7590d595076aa9180b73fdc9eee84eb8` | `f2504fd6` | 实现 Anthropic 原生 Messages、system/文本/图片及思考输出、模型发现；避免兼容端点假成功，流式明确未支持。 |
| 5 | `9f2fb4611fc8734238c188294b5f21d821583bf9` | `0feaf251` | 批次/query 的 None、空串和纯空白转为字面量 None，不改变数量/顺序或伪造向量；保留真实模型 ID 计费、合法零和调用者事务。另采纳后续 95% 截断触发裕量。 |
| 6 | `7cdc74bbe5e0e9e0ea3cbdd8b4264ca7f16b49b2` | `8a4cc393` | ES/OS 服务端 exact cosine+1 替代候选向量载荷；保留词法/向量融合与失败语义。只有确需 citation 时按授权 KB/doc/source 补取向量，普通空 fields 合同不变。 |
| 7 | `f1d238357258b792dc8937845e0ab6cfd0473982` | `9e7129f9` | Infinity 只下推安全候选超集并精确复核；负多值条件不取不安全补集，缺失/JSON null/字面 null 与 typed 值按真实 SDK 验证。查询错误不转空成功。 |
| 8 | `b09da6e347ffc19927ad3cc5d9e0720d2385e9d9` | `d739ef2a` | 实现 CometAPI chat/SSE/embedding/发现/额度；独立额度主机和绝对路径处理，0 合法、畸形/非有限额度失败，请求不泄漏凭据到发现或重定向。 |
| 9 | `fe82a961932fdb656d4d65517e3705746b361e0c` | `be9e01bf` | 保留登录鉴权、全 DNS 校验和所有驱动资源释放；补充 NAT64/兼容 IPv4 私网识别。保留更强的 Trino 全生命周期同源与 DNS pin。 |
| 10 | `b40b0bf99694bf72ab97c95ac440e91231e8804b` | 等价保留 | 生产等价保留：现有结构化解码不消费无用 object 字段，已有严格向量索引/数量/有限值合同；不复制宽松 map 或制造冗余 diff。重新运行供应商边界与实际绑定验收。 |
| 11 | `56d73d0c2c833215e716b3b43523a675dc21f93d` | `93aad8f5`、`0bbe274f` | 按类别首次访问构建/原子发布注册表，失败可重试；177 个工厂映射一致。Canvas 导入放在既有授权/预检后，避免丢失活动调用方。 |
| 12 | `b12eaee38b77681dc27abadaf5a54957c917b453` | `e53fcb4f` | 保留七个既有路由与两套 facade 权限；rebuild 服务同时验证 connector、KB 与持久绑定，拒绝发生在删除和调度前。保留本地 owner/normal/admin 与租户生命周期规则。 |
| 13 | `13b422037fce1847bcf8def78cbcc533c2a6aeee` | `a0fe84be` | 保留活动 KB writer lease、Strong 快照与 NER；在文档 semaphore 内按需加载，恢复时不重复读取。配置 512..8196/default4096 双端保存；独立 generic retrieve_all 使用 scoped native scroll/Strong snapshot。仅退出无调用旧 run_graphrag。 |
| 14 | `b8ac9976061aff826cad3a89424fe8af3206e17c` | `5a3b947a` | 五个 REST 别名复用已认证路由链，路径/查询冲突拒绝；模型默认设置绑定个人租户，拒绝越权默认写入。 |
| 15 | `92145dc7642a63820966b0f4e3b06ab21ec7b48a` | `913dced8`、`be7704cb` | 补齐 DeepInfra/XunFei 的 chat/embedding/媒体、密码包/模型别名/版本和连接检测；DeepInfra 余额保留有符号语义。另修 SiliconFlow stream 自定义 suffix，保留更强 MiniMax/OpenRouter 能力与自定义共享 RoundTripper。 |
| 16 | `2dbe3b8a6227c769df50a837f8799fecbbb1ba6a` | `c932bd42` | 保留活动 REST 的 None/[] 差异，修正 get_list 等路径的显式空选择早返回；0/false 条件及零匹配经 SQL/HTTP/实际 Web 文档客户端确认。 |
| 17 | `732e4741c4cde4286a759f30f80bf7b303b120a3` | `dbb71a54` | 生产 tuple 聚合已等价，保留更强授权分组/去重；只补真实 Infinity/ES→Dealer→REST→Web 的标签验证，不为已匹配接口改 shape。 |
| 18 | `93d3deb5e425966197575449a88ccb077312251a` | `0262b2b8`、`dec7a942`、`02752933`、`24dcad28`、`6e773e51` | Python/Go 分别适配变量 CLI/HTTP/SQL：typed 0/false/空串、排序/字面 prefix、严格结果行、完整命令语法；GET query 传参，未知长度 body 也拒绝。 |
| 19 | `32902570141c8da9976b3adbbeb5f0781ccdf887` | `113d9e83` | 记忆更新先授权，再比较规范化差异；等价更新保留时间/prompt，合法 0/空值有区分。填充后的实际类型/embedding 变更明确拒绝。 |
| 20 | `d7fb4bdb4ec50db877afda7dbed8e3bf7ba526c0` | `05a10d1d` | 丰富既有已授权文档列表，唯一 user/pipeline LEFT JOIN 保留 orphan、稳定分页/总数；run/时间/解析器/元数据严格映射。CSV 类型已等价，无虚构 Web 改动。 |

## 最终组合检查

验证只陈述执行过的层级。原始失败报告没有覆盖或改成成功，分项通过不能使它们所属的混合命令变绿。

| 层级 | 实际结果与证据 |
|---|---|
| 静态 | `integration/reports/static/`：改动文件 Ruff/format、当前纳管 mypy 149 文件、8 条 import contract、async DB 与 diff 检查通过。后续仅两处测试夹具变更，另作局部 lint/format；生产和纳管类型输入未变。 |
| Python 3.12 / 3.13 | `unit-312/313` 原始命令各 1199 pass、18 fail、exit1；16 个阈值夹具失败已修复。`regression-312/313` 各 102 pass、0 fail/error/skip；覆盖原阈值断言和实际 Agent/Canvas 模型捕获。剩余两个基线 harness 问题另列，不声称全部 unit 通过。 |
| D/G 工厂/计量 | `dg-embedding-312/313` 各 9 pass；实际 OpenAI SDK→自有 HTTP→SQL 独立读回，保留模型 ID、零计量、caller pending writes 与错误 trace。 |
| 检索/元数据存储 | `search-storage` 30 pass：真实 ES8.11.3/OS2.19.6 评分/引用/完整 scroll、Infinity0.7.0 投影/残差、SQL 与空选择。 |
| HTTP/实际 Web 消费者 | `http-consumers` 原始 116 pass、1 fail、exit1。唯一失败是已退出路由级 Agent 别名的捕获夹具；修正后 `runtime-attachments-final` 1 pass。完整会话清理/SDK、connector、模型、chunk、变量，以及 pin617be8b5 的文档/标签/会话删除客户端已实际执行；F 新 helper 的 document mode 已重新通过。 |
| GraphRAG | `graph-lifecycle` 28 pass；覆盖批次预算/NER、10005 源块 Strong 完整有序读取和既有 checkpoint/lifecycle。`graph-web-final` 1 pass；亮色英文512/暗色中文2048保存与重载、NER禁用保值、真实 worker入口与SQL读回；拼贴图由集成 agent 审阅。 |
| 冷启动/完整 lifespan | `cold-startup` exit0：原生产入口、健康/OpenAPI、匿名401、8个并发首次模型探测、真实SQL保存/失败零写入、Agent会话、chunk与strict smoke；自有API/provider/Redis/卷/SQL资源清理。该命令证明功能，不是性能基准。 |
| Go 当前组合 | `go-build-vet` exit0：internal build/vet及三个独立main二进制。`go-combined-live` 18个叶用例、0skip，五个本批实际入口、112次受控供应商请求、同一个CLI二进制与独立SQL读回。 |
| Go 扩大范围 | `go-scoped-race` exit0，1918叶pass、31个显式live opt-in skip；随后 `go-result-audit` exit1，存在4个基线未登记旧opt-in；不称无skip或CI全绿。额外5个旧catalog失败在A候选与冻结基线实际执行均复现。 |
| Web 确切617be8b5 | 本轮 lint/build/test inventory/file-size及完整 `test:ci` 全部exit0；source-node809、source-vitest665、desktop-node81、tooling-node10通过。历史MCP时序失败原始记录保留，本轮不再复现。 |

所有新运行的原始命令、退出码、源码/锁文件/配置哈希、JUnit和输出哈希在 `integration/reports/` 及对应 runner 目录。
Go二进制、native库、词典/资源与A+B注册路由绑定见 `integration/go-results.json`；不同SHA复用时按相关输入逐文件核对，不能以相同分支名或旧报告代替。

## 保留失败、差异与验证界限

两个 harness 基线问题分别是既有 `test_app_list_web_consumers.py` / `test_shared_chunk_owner_contract.py` 未列入 consumer，以及 `test_retrieval_parent_http.py` 的跨测试导入。当前失败函数AST及触发文件与冻结基线一致；归因基于 Git blob/AST，对基线未另跑 pytest。证据 `integration/baseline-harness-qualification.json`。
Go audit 的4个旧未登记 opt-in 为 DatasetRead、GoogleEmbedding、Media、NVIDIASiliconFlowEmbedding；测试字节及基线allowlist已核对。没有把未验旧入口补进allowlist换绿。本批新增5个opt-in已独立真实执行。证据 `integration/go-opt-in-inventory.json`。
旧catalog失败为 CoHere、FishAudio、StepFun、Baidu 与 OpenRouter/VolcEngine 固定数量/元数据断言；A实际基线与候选各28叶pass、5叶fail，原始exit1和对应源码哈希保留。

组合收集曾两次因Milvus协议不就绪exit1，无skip替代；释放本批自有Java/Infinity实例后按协议重探并补跑通过。未重启共享服务。Graph大源读取与其余生命周期原始历史也保留。

附件清理与新引用写入没有全局原子性，SQL提交失败可能出现对象已删而会话尚存，重试完成SQL；其他owner/无效描述符不由此入口回收。Infinity完整投影由SDK在内存物化，不声明有界内存。ES减少向量载荷会增加按需citation查询，不声称请求整体更快。

G单线新进程观察中导入时间约12.25s→10.45s，峰值RSS约1.523GB→1.519GB；共享主机、单样本，不证明整服显著省内存。最终组合另有冷启动功能验收，没有重做性能对比。
Connector保留本地owner/normal/admin策略，未补上游joined Tenant.status过滤，不能称所有维度都更严格。调度SQL行是真实排队信号，本轮不证明实际连接器worker或填充向量清理。

本集成scratch PG（Go/Python共用实例、独立库）关闭fsync/synchronous_commit，证据限于提交后独立读回，非崩溃耐久性。Go为Darwin arm64、已有native库，非Linux源码构建。供应商SDK/HTTP受控传输不代表真实付费账号可用。全量core四分片、最终远程CI、生产切换均未执行。


## WIP、恢复与清理

开工前冻结后端28项无关WIP与Web独立分支 `codex/frontend-modernization@156d23c781460553ff80c6537df42211a8255508`。后端只按本批delta合入；对重叠PROGRESS采用三方合成并逆向剥离本批delta核对原始字节。Web master单独快进，原工作分支、HEAD、index及工作字节保留。

八条后端候选及Web候选已制作增量bundle并验证；基线作为恢复前提保留在主干历史中。root源码/docs组合另作bundle，完整SHA、校验日志、WIP快照、对应清单保存在证据根目录。

只清理本批登记的scratch库、索引/collection、bucket/key、监听、进程、容器/卷、临时凭据、运行时及after9d工作树/分支。借用配置、模型/词典与native资产的原目标保留。最终独立读回、精确主干SHA和所有权清单见 `delivery-complete.json` 与 `integration/*cleanup*.json`。不使用全局prune，不删除其他批次worktree。
