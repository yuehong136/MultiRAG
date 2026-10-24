# 2836a934 后续 20 项本地集成记录

本批的源码、定向验收和清理状态分别记录。未 push、未部署，远程 `ci-required` 待运行；
本记录不代表完整 core、全仓测试、付费供应商或生产验收。

## 冻结范围与证据

- 后端基线：`894d134fb249aad99d1b120cb6a9613bcc1f0c46`；Web 基线：`de83cc7668a4e2d0012ae250b77ec80112809994`。
- 上游 first-parent 排除 `2836a934b5bbf90b9107d8f6d52cacc6b3e99974`，连续 20 项至 `440153c378d9166d72b30d2c9f86e81f905af230`；后修参考上限保持 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
- 实际 Web master 与 consumer pin：`2883cd896a2d42bc6a0a606102393dea8fdc49a9`；包含日志列改进和 Sandbox 管理页面。该提交尚未 push，远程 consumer 需要先发布依赖。
- 本机原始补丁、逐项机制比较、source hash、运行输入、命令/exit、JUnit、恢复包和资源账本位于 `/Users/xldu/.local/state/multirag/validation/20261024-batch-after-2836a934`。
- 最终主干、候选映射与清理边界见该目录 `integration/state.json`，避免文档提交自引用。既有 28 项他人 WIP 不属于受验组合。

## 逐项处置

| 项 | 原始 RAGFlow SHA | 本地处置与质量选择 |
|---|---|---|
| 1 | `aea90f4e3974a0e506f83e6da96c62bbaa734fe7` | 适配 REST 合同测试和计数恢复；原生写成功/SQL 失败后重试重新对账，排除 parent 物理行，文档锁沿同一请求事务。 |
| 2 | `d69518ea420d78cdbeb94002d2c1b65d6c29f034` | 已有等价：自定义 URL 构造失败显式返回，实例凭据/region 隔离；补实际 HTTP/SQL/CLI 证据，不制造生产 diff。 |
| 3 | `1ed8a118cf7a1f7ce986fe2b23dd4d8ba7f164ea` | 当前无文件夹移动树活动入口，不引入没有消费者的上游组件。 |
| 4 | `6499bce2a67c55191efb7dc4cfe3ff359b64c5ef` | 保留更强的 Langfuse observation 生命周期、失败和隐私处理，不引入共享状态。 |
| 5 | `b28e134944bffbb0de16efac1a67d5de9a3ad7ab` | 适配 Python/Admin/Web schema、凭据遮蔽、原子保存、配置指纹刷新、Local/SSH 与重连清理；发现目录移除五个退役预设，保留已保存租户实例。 |
| 6 | `6ce76e6799521bce60d6e4c825718bc3783495b3` | 保留更强的 Discord 事件循环、客户端关闭、取消和背压实现。 |
| 7 | `4a91ca53492edef2431b94b716d419bd5d4896d4` | 适配 Go MinerU_Local 文件内容任务、严格任务状态/结果、可选凭据和 CLI DOC_PARSE 声明。 |
| 8 | `31bf29bc3857961e4922532bbd3032c991176bb3` | 上游发行草稿无独立本地实现目标，不复制产品能力和性能声明。 |
| 9 | `90c76e73d072a2fba9ffdd8cdde694a9cb4a31af` | 32 个版本足迹按本地产品、镜像、依赖和迁移核对，保留 MultiRAG 自有版本。 |
| 10 | `dbef3e361ffabe763dc3712bf4155ff3ac314948` | Go 显式 chunk scope、CLI DOCUMENT、metadata flattened、tag DELETE；Python Infinity 空 scope 不查库、metadata 限定 dataset；Go 管理读取不扩大检索可见性。 |
| 11 | `aa0a3d69888cf940aee24fffb92985b0aa77ac38` | 受启发的本地布局改进：日志列设置最小宽度，完整长文本、表格滚动与分页可用，保留当前 API。 |
| 12 | `dc01e0e51c20391ce8023806bd4fada2b011d20c` | Go TogetherAI indexed embedding，复用严格批次、索引、维度和有限值验证。 |
| 13 | `63db30f0d98b761b6f5ffad91de9cc490ee69db0` | Go N1N chat/embedding/rerank、自定义 URL、thinking false 与严格结束语义。 |
| 14 | `536ed07d2792b71948809a240a0dffe82ad99aee` | Go Xinference rerank，保留 Top 0、严格索引/分数与取消。 |
| 15 | `fec0b968e7708588aa412363d67b4904707dbf29` | Go Novita rerank，复用有界 HTTP 和严格结果验证。 |
| 16 | `394cd5d1169f800a8d1ceb821748a4d4e79eaea1` | Go Xinference embedding 的索引重建和实例隔离，不把同维度向量当成真实厂商兼容证明。 |
| 17 | `c7ac9b7171a3428e0c55074264a014f198199f6b` | Go GPUStack chat、/v1 路径归一化，过滤与未知终态失败。 |
| 18 | `2d3a1a448393a0a8a22dbdb922ed7ae94f6e546f` | Go Azure api-key/api-version、/models 发现与手动 deployment 分离，过滤和晚到错误不成功结束。 |
| 19 | `0c93161a14aca1dd8cb1c00e84553ca267d94771` | 保留服务器身份约束并补 JWT/API key、流/非流、显式/自动 session 验收；伪造 user_id 不改变 SQL 归属。 |
| 20 | `440153c378d9166d72b30d2c9f86e81f905af230` | 保留更强授权并过滤 SQL 文档已删除的普通 chunk；仅当前可信 GraphRAG 返回对象可以没有文档。 |

## 验证层次

Python 集中检查包含 Ruff、format、mypy 纳管范围、八项 import contracts、async DB 门禁和
133 个相关单测。锁定 `infinity-sdk==0.7.0` 与实际 `infiniflow/infinity:v0.7.0` 的补验为
21 个单测和 26 个实际 SDK 用例，零失败/零 skip，26 个 UUID 临时库独立读回不存在。
此前共享环境的 `0.7.0.dev7` 结果保留原始身份，不作为锁定稳定版的证明。

最终 Python/实际 Web 组合原始结果是 17 passed、1 failed：11 项 REST 存储、1 项 Web chunk
客户端和 6 项 Admin/Sandbox。原 A 的额外 shared-owner/Dify 存储用例另补 1 passed，
共同覆盖 A 的 12 项存储及 1 项实际 Web 客户端。Sandbox 新增路径均通过。

唯一组合失败是既有 Admin variables 排序用例：`en_US.UTF-8` PostgreSQL 的标点排序与 Python
`sorted()` 不同。在同一自有 scratch 库实际执行基线 `894d134f` 的原始读取/格式函数和
未改动查询，重现相同顺序与原断言失败；另存 PG 版本、collation、SELECT、独立 SQL 未变
和临时库不存在的读回。这是有界原函数/查询复现，没有执行整套基线 pytest。未改断言、
SQL 排序或门禁来换绿；C 自有 PG 环境中原 6 项通过仍只对应其确切配置。

C 的独立证据还包括 183 个单测、18 项真实自有 OpenSSH/Python/Node 验收、实际 Admin JWT、
SQL、Local/SSH 连接和浏览器操作。浏览器普通主壳的 user/team 响应受控，管理路由和 SQL 实际运行。
Web 最终 master 的 build 与全部 1,890 项 `test:ci` 均 exit 0；六格 contact sheet 已一次审阅。
D 的长日志滚动/分页覆盖桌面和窄视口；页面高度改善不作为后端性能声明。

F 的原 14 实例 SQL/JWT/CLI 验收与终态尾修分开保存：尾修后的三个 strict provider、六个实例
补跑过滤路径，流/非流 CLI 失败、HTTP 无成功 DONE、SQL 读回不变。全模型 race 原证据、
新终态定向 race 和实际二进制来源分别绑定，不把旧 full14 结果冒充尾修后的完整重跑。

Go 的 internal build/vet 与三个独立 main 链接均 exit 0。八个相关包的集中 race 原始 exit 1：
六包通过；NLP 缺仓库内 WordNet 路径，补精确固定资产只读链接后仅复跑 NLP，exit 0。
entity 的三组旧目录计数断言仍失败。在不可变 `894d134f` 完整 Go/catalog 快照上只运行这三组，
实际复现相同五个叶子失败及父组失败，基线 exit 1；快照与临时 archive 已清理。未改旧计数。
默认 race 的 30 个 opt-in skip 另有四个旧入口尚未登记，保留为 unexpected skip，不扩大名单换绿。
因此扩展 Go 组合和审计没有全绿。原始失败、NLP 补跑和基线实际复现分别保留。

E 在最终接受源码上分别完成实际 ES/Infinity、SQL、新 CLI 与固定 Web 客户端验收。Web helper
四个 API 入口的 23 个本地依赖和 package/lock 与最终 Web master 字节相同，按相同执行输入复用。
原始命令、源码和独立读回见 `integration/state.json`、`integration/reports/` 与 `E/reports/`。
两个本批新增 live opt-in 在普通无服务 race 中只登记为未验证 skip；缺少必需配置且
`REQUIRE_SERVICES=1` 时必须失败，不把 owner 的实际验收与默认 skip 混为一种证据。

B 的 76 个 Langfuse/Discord 用例含受控 HTTP，客户端、任务和循环读回关闭，不声明真实
Langfuse SaaS 或 Discord 凭据验收。A 另有 24 个实际 chat/SQL 合同及 67 个相关单测。
追加图片单测中的既有加密存储异常文案失败已在基线复现并保留，没有改无关存储行为。

## 独立补修与清理限制

MySQL C extension 9.3 的重复 fetchmany 行通过游标迭代修复，未切换 pure Python 或降依赖。
独立提交 `e00f6a98ad23b61780c34884016c4f61279eb13a` 已进入实际 main；76 单测、
13 实际 MySQL 与 13 实际 PostgreSQL 用例通过，相关 scratch 已独立确认清理。

集成 runner 在创建私有 overlay 前记录精确目录、文件、进程与源码 hash；正常/异常/TERM
出口清理，SIGKILL 留可恢复账本。57 个 harness 单测及实际 PG 入口检查通过。历史两个
未预登记的中断 overlay 无法仅凭时间戳确定归属，未猜测删除，也不声称全局临时目录清空。

已知自有运行资源、私有配置、二进制/native 副本和只读资产链接按精确账本清理并读回；
共享服务、字典/模型资产和他人 WIP 保留。恢复 bundle 校验与接受字节核对后删除本人分支。
Codex managed worktree 的一次归档被主 checkout 保护拒绝，错误为
`The task's primary checkout cannot be removed.`。受保护目录保留，未手工删除或改应用数据库
绕过；因此 overall cleanup 仍为 false。普通 Web worktree 已由 owner 正常清理。

本批不自动开启下一批，不恢复旧自动化。远程 CI、完整 core 四分片、生产 worker、真实付费
供应商、生产 SSH 与部署均保持未验收。
