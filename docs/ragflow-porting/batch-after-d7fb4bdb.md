# d7fb4bdb 后续 20 项本地交付

范围固定为 `d7fb4bdb4ec50db877afda7dbed8e3bf7ba526c0` 后的连续 20 个 first-parent 提交，
至 `2836a934b5bbf90b9107d8f6d52cacc6b3e99974`，冻结上界 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
后端基线 `c1d259f301edc7ee1f2d141415fcdde3069f77e7`。15 项采用本地框架适配，4 项保留已验证的更强等价实现，1 项不适用。

生产后端仍是 Python；Go 是独立并行实现。功能组合在 `4a20208319c978784315945ab611c556140a5611` 接齐，
`d7203b729023d1dfa8223723d3b01e53d5f6b1d6` 单独登记测试、native 专项和 consumer pin。
Web 接受 `de83cc7668a4e2d0012ae250b77ec80112809994`，保留已推进至 `79bd466256ed17f425b0a660d0dba502ba393c8b` 的 master 全部提交；
必要 Agent Hub 适配为 `f53082f7411a784133e4a94c42172303e57d1dc1`。
本地交付不等于远程 CI 或生产发布；未 push、未部署。

## 逐项结果

| # | 上游完整 SHA | 接受提交 / 结论 | 当前本地行为 |
|---|---|---|---|
| 1 | `41a9fc003066b8f030a426e2a5c0f3e818141332` | `fbc871c8` | 聚合图 GET/DELETE 先授权，再绑定规范 dataset/tenant；图损坏、重复记录和后端失败不冒充空图。 |
| 2 | `b69a6a5d803432c32a7176a0ae42443140e64604` | `4a202083` | sync/prune 按 KB 独立调度；单一 PATCH、严格状态/布尔输入、事务内文档版本条件；Web 与 Agent Hub 已迁移。 |
| 3 | `198f3c4b9a4750a80a7ecf6abcdd3c421e8beff4` | 既有等价 | 既有 Memory 模型 ID、tenant 与执行期身份校验已覆盖；保留更严格的当前合同。 |
| 4 | `525a87be0ff3e3ae75d68c0a0e12d186f0235f20` | 既有等价 | 目标拼写和 CORS 命名在本地已等价，无造改动。 |
| 5 | `db9e78274757fae76458bd32ddb79864ea40c773` | `9b047894` | MinerU URL 异步任务、轮询、结果校验与取消；CLI 不抓取 URL 文档。 |
| 6 | `4c9529ef3685ba6d81afc6e974dc156cf84fff1c` | `c4331454`、`e7261584`、`2c10b9bb` | Replicate prediction/SSE、严格终态、线性拼接；文本多段可组合，非文本在发请求前拒绝。 |
| 7 | `87d22a44153fb4deea692520de8876b9e90cf714` | `103adb85`、`e4968cc4` | draft Agent SQL 会话与不可变 origin 先持久化；输出/引用/结构化/pause/错误/think 可恢复；终态在 SQL 提交后写副本。 |
| 8 | `c6e3a2e713c1aae9f4f051e370a6cc396c0e6e61` | 既有等价 | 现有 MinerU 递归、身份感知的输出检测更完整，保留已测实现。 |
| 9 | `f17a66d4f0946f83e9eae8cfa2d3d1414db94f53` | `74678b1c` | OpenCC 长行、UTF-8 尾部、字典/配置与值终止边界；新增独立 sanitizer 专项。 |
| 10 | `f169ab4b3980c78a26e8da8b8c6ffda258d35b04` | `006912d7` | tenant/有效模型/配置隔离的 Redis TTS 缓存，默认七天；禁用、失败和空音频不伪造命中或用量。 |
| 11 | `7edabdf7c35ad3689846ec9d89e70d0de06ffe90` | `1345f517` | Manual metadata filter 每次按当前变量解析且不修改模板；Iteration 多轮复用保留类型与授权范围。 |
| 12 | `09a06f1b00fa24116f1e90af1ac40a90fd4414b0` | `0bc902e4` | Xinference 动态本地实例与聊天，可省 API key、必须指定 URL；模型目录、service 和 CLI 端到端补齐。 |
| 13 | `243d9ed2819befd79bf5767256bd5857c4e42809` | `63c58fb5`、`e61b2a65` | TogetherAI 聊天、reasoning 双字段与取消/发送失败边界；取消后不继续回调。 |
| 14 | `ce3402cbb99a61541539b8bcf750b2164335b929` | 既有等价 | 保存 API key 的回退已在当前模型配置路径实现，保留真实失败行为。 |
| 15 | `95b56e73f2a676d792512b86e367715a6a0568e9` | `a270012f` | 在当前 REST 管理/补全/音频面补真实 HTTP/SQL 合同；补全异常不再发送成功尾帧。 |
| 16 | `f58e0b3ecac52dcf0c12992b168bb296456beebf` | `f76dc469`、`0b1b71c9` | MinerU figure 通过 tenant VLM 补描述；四并发上限、输出根范围、fallback 和显式 bundle 的 Session 隔离。 |
| 17 | `6796a47b8d37ff4464e06ffc1a4ddb5c8ea830d1` | 不适用 | 当前和已核对历史 SDK 均无 Session.ask；不恢复退役接口。既有 canonical run 输入发现文档已补齐。 |
| 18 | `6b2fcb41160b470ca9187b7d6c0b191e76136e25` | `309c3e2b` | 补足 REST 补全/音频回归；StreamingResponse 建立前的异常也回收临时音频。 |
| 19 | `77834870fcaad410823dbd583e1ea81028e46689` | `db5e76f1` | 显式 ChunkStore/MetadataStore；ES dataset 范围、Python metadata 物理 ID；Infinity typed filter/父块合并，Milvus 通用写入明确失败。 |
| 20 | `2836a934b5bbf90b9107d8f6d52cacc6b3e99974` | `477efb79` | 302.AI、JieKouAI 模型及 CometAPI 非流式 ASR/TTS；注册工厂、错误、资源与用量按当前 Go 合同适配。 |

另有三个必要共享修复：`d063ac29` 补 Xinference 目录初始化，`1703103b` 把 connector 文档版本条件放进真正删除事务，
`389d526e` 令已被其他删除器移除的条件目标保持无副作用。CLI 支持提交 `29ddbc5f`、`3414ffc6` 与 provider 实际验收 `0ecb079f` 随组合接受。
23 个候选提交的稳定 patch ID 均从实际 Git 历史独立读回；完整候选/接受 SHA 在本地交付清单中。

## 定向组合证据

证据根目录为本机 validation 的 `20261024-batch-after-d7fb4bdb/`。原始命令、退出码、输入与二进制哈希、JUnit、失败历史和资源读回保留在其中。
不同 HEAD 的复用按文件、锁、配置、后端与消费者输入核对；不会将分项报告当作另一最终 SHA 的全量通过。

| 层级 | 实际执行结果 |
|---|---|
| 静态 | `integration/reports/final-*`：所有改动 Python Ruff/format、149 文件 mypy、8 条 import contract、async DB 门禁通过。CI YAML/bash 仅验证配置语法，未声称 Linux job 已执行。 |
| Python 单测 | `python-unit` 原始 exit1：509 pass、2 个既有 harness fail，无本批失败；新增条件删除的事务/零副作用回归另有定向通过记录。 |
| Python 真实服务 | `python-core` 72 pass、`python-connector` 19 pass，0 fail/error/skip。覆盖实际 SQL/Redis/ES、REST chat 管理/补全/音频、draft origin/恢复、TTS、Iteration、connector worker、删除失败和并发新版本保留。 |
| 固定 Web 消费者 | `python-consumer-fixed` 3 pass：draft run/日志/续跑及 connector 明暗主题、移动端、SPA、严格 422、权限拒绝和失败保留脏表单；实际 HTTP/SQL。五张截图先拼成标注图整体审阅。 |
| Connector 细分矩阵 | D 的固定 `08231c5a` 另有正式登记+UTC 的 19 项 worker/调度/迁移/checkpoint、2 主题消费者；Hub 固定 `f53082f` 的 8 步真实 adapter 请求与独立 SQL 读回。来源清单与补验限制在 D 最终报告中分列。 |
| MinerU | F 精确候选 111 unit、1 真实 PostgreSQL 模型解析；包含真实本地 PDF→ZIP→parser→LLMBundle/CV HTTP→chunk。VLM HTTP 为受控供应商边界。 |
| 原生库 | C 的 13 ASan/UBSan、3 archive probe；127 个 native 源输入、Darwin arm64 archive、PCRE2、编译器与 24 词典文件绑定。I 用该新 archive 重链并跑 5 项分词 race。 |
| Go 当前组合 | `go-build-internal`、`go-vet` 和三个独立 cmd 构建通过；显式哈希 race binary 的 provider 14 项、ES/Infinity store 各 1、graph 各 1 及固定 Web API 客户端均通过。受控 provider 路径实际触发 154 CLI / 150 HTTP。 |
| Go 定向回归与复用 | I focused race 12 pass、2 个显式 live opt-in skip；其中 graph 随后在两个真实引擎执行，DocumentStatus 的 B 实际 HTTP/SQL/Infinity 故障与行锁报告单列复用。模型包只有自身 main-module 依赖，189 个源码/目录/锁输入与 A 最终报告相同，复用 1,794 项 race 全过；不重复执行相同输入。 |
| Web 当前接受提交 | `de83cc76` 的 npm ci、lint、file-size、完整 test:ci、build 均 exit0；实际 API/浏览器消费者也使用该提交，不复用旧 617be8b5 pin。 |

## 保留失败与验证界限

两个 unit 基线失败是已有外部 consumer 模块未登记、以及 `test_retrieval_parent_http.py` 跨测试模块导入。
未把无关旧模块补进 allowlist 换绿。四个旧未登记 Go live opt-in 和五个旧目录固定数量断言仍是已记录的更大范围限制，不能据本轮定向通过声称远程 CI 全绿。

I 的首次 graph fixture 使用 UUID4，被既有 UUID1 合同拒绝；更正自有 fixture 为 UUID1 后，在同样断言下通过，原始 exit1 保留。
首次 consumer 命令遗漏 consumer suite，3 项被排除、exit5；用正式 suite 重跑通过。另有本地命令路径/配置绑定校正记录，不改生产断言。
D 早期 scratch 的本机时区、plugin 优先级与创建前登记差异明确保留，正式验收改为匹配生产 UTC session 并独立读回。

Agent/DataFlow 本地分类不同：DataFlow 保留排队 Task/message_id，不声明已经有 draft SQL 会话等价。
SQL 提交与 Redis 副本不具跨系统原子性。TTS 并发未命中允许重复合成，不承诺 singleflight。
Go graph 仅聚合图，doc_id 子图明确不支持；通用 Milvus 写入和固定 Infinity SDK 的双引号字面量限制没有伪造成功。
MinerU 显式 Session/bundle 不会被 worker 线程共享；真实付费服务、供应商限额和生产网络仍未验。

额外 MySQL 实源补验未通过：同环境的冻结基线 RDBMS 模块逐字节复现 mysql-connector-python 9.3.0 C cursor 重复末行、不耗尽结果；保留原始失败、终止和有界基线复现报告。未改生产驱动或用 skip 换绿，不能把 PostgreSQL 来源通过扩称 MySQL 全过。

未执行完整 core 四分片、最终远程 ci-required、Linux native rebuild 或生产切换。
Web pin 尚未推送，远程 consumer checkout 需先发布相应 Web 提交；本轮不自行 push。

## WIP、恢复与清理

后端 28 项既有 WIP 按初始清单保留；重叠 PROGRESS 使用三方合成并逆向剥离本批 delta 核对原始字节。
Web 保留本轮期间其他任务合法提交，必要 Agent Hub 保留 .idea；只串行合入本批已接受提交。
各线和集成的恢复 bundle 均先验证，再精确清理自有工作树/分支。
仅处理已登记的 scratch 数据库、索引、对象/Redis、进程、端口、容器和临时凭据；借用服务与原资产保持。
最终三仓实际 SHA、pin、逐项 actualMain、WIP 与清理独立读回集中在 `delivery-complete.json`，报告和恢复包保留。
