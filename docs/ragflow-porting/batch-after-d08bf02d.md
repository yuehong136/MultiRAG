# d08bf02d 后续 20 项本地交付

2026-10-23 本批 20/20 已完成逐项评估、适用实现与定向验收，并串行合入实际后端 `main` 和 Web `master`。
仅本地交付，未 push、未部署，远程 `ci-required` 待运行；不是完整 core/全部集成或生产验收结论。

## 冻结范围与组合

- 后端起点：`24200323053904c31257e681c01881f19e3337fd`；Web 起点：`1250ebfbdabe4771e416d3f982b3bda0b1a310b3`。
- 上游 first-parent 区间：排除 `d08bf02d9bd4f66c9b4f9c1b62f021a4e1a92e15`，连续 20 项至 `bbb0798c41e760b9870f98b6f0c667d7659b2491`；后续修复查阅上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
- 后端最终可执行源码组合：`ada8c20209e1c8ff253b9fb5e36524ec6229b94f`。
- Web 最终实际 `master`：`9cb81b046e409690241495ee893dc077c5a37bf3`；[消费者 pin](../../.github/consumer-web.json) 固定此完整 SHA。
- 原始冻结清单、完整 diff/哈希、命令/退出码/JUnit、逐线交接、资源登记和独立读回保存在本机 `/Users/xldu/.local/state/multirag/validation/20261023-batch-after-d08bf02d`。后端最终文档提交及清理完成后的主干 SHA 记录于其中 `delivery-complete.json`，避免文档自引用提交 SHA。
- 上游远端观察值曾因其他 fetch 改变；本批仍只用冻结区间与上限，不随远端推进范围。

## 逐项结论

下表是整项处理结论；相同本地提交可以共同实现多项，保留已有更强实现不制造空提交。

| 项 | 原始 RAGFlow SHA | 线 | 机制与最终处置 | 本地实际提交（短 SHA） |
|---|---|---|---|---|
| 1 | `14332dd75c32aaec9107335c0bda5b7a94b80b9e` | A | 移植。四处时间写入复用同一截断到秒的 instant，数值统一 UnixMilli，保留原子计数。 | `c0b0f873` |
| 2 | `7d3836907aa0324d6ef7dfef233231996bbe2b3f` | B | 适配 Mistral chat/SSE、embedding、发现与目录；共享 transport，结构化内容与 output_dimension 后续修复纳入；严格索引/维度/有限值。 | `9e430c60`, `c8f48b93` |
| 3 | `45ee5ca9cd0d4e7ac042a7c235eeeb238b61b13c` | B | 适配 Jina embedding/rerank/发现；query/passage task、id 解析与 TopN nil/0=全部；负数及畸形响应失败。 | `9e430c60` |
| 4 | `127aeac4aa1e248a793e3958a0e27efd209edb62` | H | 补齐 Python/Go OpenAI 目录 gpt-5.5、gpt-5.4 及原提交能力元数据；不以目录存在推断供应商可用。 | `0088028f`, `57dccce3` |
| 5 | `3f41f8cfae143024b66c3c9928b27cd1e15e1f96` | C | 共享 Wait 消息投影，显式来源 ID 和无碰撞合成 ID；暂停提交后持久化历史，保留推理/附件；同名 file_id 附件不得误去重。 | `f4e31125`, `9cb81b04` |
| 6 | `76d5240fb5eb203c7a8c808e44291d27f97b5ca4` | D | 远程 keyword 查询、分页与选中项详情缓存；晚响应隔离，未解析模型约束时 fail closed；Agent 复用通用选择器。 | `e119cffa` |
| 7 | `ad4717f40a0152329aa7c07baed59659fe2a0538` | B | 复用集中模型类型校验，补 vision-as-chat；未知 nullable 自定义模型不再放行任意能力。保持媒体 20MiB/120s/501 合同。 | `40dbea42` |
| 8 | `5e46457c28d615aec5f9676740c8408121cdcce7` | E | 按实际 Bitbucket Cloud Basic email/token、仓库/项目优先级、PR 内容及关联/auto_parse 生命周期重写指南。 | `98943e8c` |
| 9 | `c34c81e8e6756c1d15124feffb76e2e784e1837c` | E | 直接移除音频后缀列表重复的 .wav/.aac；不扩大到后续临时路径生命周期改造。 | `0872b235` |
| 10 | `5a5e766386f27489a0d499007358ed0dab6f5062` | F | 本地安全适配。可信 Principal 与同请求 AsyncSession 查 active membership，完整校验 owner_ids 后在 SQL 限定 count/page；用户 ID 相等不授予权限。 | `d5bb787e`, `39834b5a` |
| 11 | `64bd0130d3b89ad9dbbe50f17d81b6cf05ccf4d0` | G | 保留已有六项 graph/index 兼容映射和统一处理；agents_openai 按本地既有退役策略保持 404，并证明无执行副作用。 | `51bbb513`, `2a49fe11` |
| 12 | `8b6dd6a5c2b3380f8713776f0a5230952abd7e0b` | E | 去 table 标签后仅空白的 embedding 输入用同位置 None 占位；标准 worker 保持 chunk/vector 对齐，不伪造向量成功。 | `4d7b0070` |
| 13 | `733d75d6a740cd9b891380a32b5e48d1e6e15d37` | H | 保留更强的共享 Baidu decoder：批次/顺序/数量/空值/维度/有限值严格校验；保留空输入 no-op。没有空提交。 | 已有等价/更强实现 |
| 14 | `45d676bc05d083a20217927bacbeb4d7ea158409` | F | 保留已有 legacy 字段重置；修复 canonical Task 删除与 KB task/finish 清除为同一 SQL 事务；真实 UI 先关闭菜单再确认删除。 | `30a50990`, `66ef0c78` |
| 15 | `71d327b11ce9e543891206b65ab8acb320dd6764` | D | AddChunkModal 局部 resize-y/max60vh；原生纵向拖拽、窄屏及暗色验收，不改变通用 Textarea。 | `c49756f7` |
| 16 | `d63d3bb7d2686da563cf59978ed27e69df113c53` | B | 适配 Novita chat/SSE/reasoning；跨帧标签状态机，失败不发成功终帧；复审修复 UTF-8 被固定字节预留切断的问题。 | `9e430c60`, `41a0cd61` |
| 17 | `09e1fd290a23184706ddcb7d726bbaa93d3059ac` | G | 逐一映射原 26 文件/138 测试家族到现有 REST 合同；补 Infinity get id/list-index、严格异常/释放、鉴权零副作用和 CI 精确 opt-in 识别。 | `c3098e48`, `51bbb513`, `e192b756`, `2a49fe11`, `48f49357` |
| 18 | `ff685d3131b4bef6c8706bc78f46c4a5bf679d43` | F | 保留 canonical /graph 与 /index 的统一 helper，修正过时注释；实际 JWT/API key 覆盖 legacy 四条 run/trace 路径及外租户读前拒绝。 | `30a50990`, `ada8c202` |
| 19 | `8b539608199af48b289b7fc124423fdd57716c54` | B | 适配 LongCat chat/SSE/reasoning，复用 transport；原范围外 discovery/embed/rerank 等能力留给后续游标。 | `9e430c60` |
| 20 | `bbb0798c41e760b9870f98b6f0c667d7659b2491` | D | 异步租户 embedding 默认值仅初始化未触碰草稿；显式选择/清空优先，租户切换、reset、reopen 隔离。 | `727bc628` |

第 5 项另有集成 owner 后端依赖 `12c111d083394e645a26138c5c2aa1c0f8556102`：
在成功提交后持久化 `runtime_pause`，暂停终帧沿用 Canvas 原始顺序；普通完成仍保持原工作流终帧顺序。
SQL 提交失败不制造已保存的 assistant。该依赖先于 C 的 Web 消费者合入。

第 10 项原提交后被上游 `214ee319f804f88d6c317a86f066ae2df75d839e` 撤回。
本地按本次明确要求保留基于实际成员关系的安全适配；不把撤回的实现照抄为授权策略。
第 14 项 Graph 原生存储删除与 SQL 不能组成跨存储事务：SQL CAS/更新失败时保留可重试的 Task/KB 映射，
已经完成的原生图数据删除不会回滚。失败注入明确覆盖并保留这一边界。
Mistral/Jina 相关后续修复已按相同机制纳入；Novita/LongCat 后续能力扩展及 Python 删除不推进本批游标。

## 候选到实际主干映射

每项引用的短 SHA 均可在此还原。右侧提交已在对应实际主干，左侧只用作工作线审查/恢复身份。

| 线 | 仓库 | 候选 SHA | 实际主干 SHA |
|---|---|---|---|
| A | backend | `c8ae8fad67dc50f26589f1dfebe5cc202c5adad2` | `c0b0f873a34b8cd80423bd0c8fc54b24e33530bc` |
| B | backend | `ea0e4ec0d8a9c0713657ae0610b9b408c8763f87` | `9e430c609797c09cabe0e6206d58808596b4e508` |
| B | backend | `0290727820e4bc66e33d29033c699e01e43857b5` | `40dbea428781446f75409b4b35b29bbcba8f33ba` |
| B | backend | `9fdcef83305046ca128f12764d1040f142b1c28e` | `c8f48b93ec995d97789eea9798e8537c8a8f5b6f` |
| B | backend | `188e1681e8fdc37815a099b727d32f97beec6cd6` | `41a0cd610bca2652e1f7e1309348afb7c76b8ea3` |
| C | Web | `3972cd517ffef2854f3ca6edcb3585a9ab97c0a1` | `f4e3112517982d2ffb01eb112931fb4955512f41` |
| C | Web | `9944ee123a196f1037dc044ad16d8d70f1fca36d` | `9cb81b046e409690241495ee893dc077c5a37bf3` |
| D | Web | `e251c4fb194089b4bf324061aee5ffc1404d7d79` | `e119cffaeeb09e6b075b0bd2424452bc899d5a3c` |
| D | Web | `9e19e1933c3df18247dd3036449577c26fadd2ba` | `727bc62880271f2bd0f4c55a9f1e54c677def299` |
| D | Web | `3ba5bac71226403d83975a49a78822f2d6268dd7` | `c49756f7391178a04cd93082626ac61ed0b79adb` |
| E | backend | `01c788de9e4bd6881b0307c2bb97a7a7b8103fd2` | `98943e8ccf0d6c29745c012deea23564f6e475bf` |
| E | backend | `427ae1ad934b7c69da26eba3eec2831f0acdb9c7` | `0872b2359b39ff13b4f5dcea3fcfe481513a298a` |
| E | backend | `e07526e5528d8d524a3cd32bc388e20131886638` | `4d7b0070af6586f6d926fae5de24bafd623e7986` |
| F | backend | `ed93192f5c662a1e7da03cc2256240ffc64fc851` | `d5bb787edfbca406f5752ac37e0ba47f60f7376a` |
| F | backend | `5d9e76210855003a3df52a2c06c9f0cfe7cd75b0` | `39834b5a59571952454bd8b6ef6fa4f9aa87be86` |
| F | backend | `11a777732d378a4c4a13fbbec292351646fe200b` | `30a5099022927f1ecd1cdb93a3b06030603ccadd` |
| F | backend | `c4fa355a679bcece46d0b1a37271567855b8101b` | `ada8c20209e1c8ff253b9fb5e36524ec6229b94f` |
| F | Web | `380a2e26714368dadf53331d188d7b9293e5e908` | `66ef0c78921029cbd71f8d7dd95f8f8e3abc086e` |
| G | backend | `30c26d6a6b64b0f26e319e214adb6daffbecbe32` | `c3098e485e3468ea902a844969052ce39a3cb840` |
| G | backend | `a91e25383a936225a65ea2ad069191be2dbca179` | `51bbb513e6076c2e8a5e1cbefe272c933888eee6` |
| G | backend | `fb5088e5a25007f8309234eb9e92b1474176fae4` | `e192b75640bf5f2eb857b2465f7f76d9f16951e8` |
| G | backend | `b15ca1a79883c6e5493c008d539bbd8d7306d775` | `2a49fe114a8d0f0467585df40d1fa159e3619cd4` |
| G | backend | `591bea6570c8220774c0eb2d7dc3d53df49e0337` | `48f493575432cb59d3f030b5c3781e10734fafbe` |
| H | backend | `bddb66e3652c75d801abc620159e51d9c65d440c` | `0088028f633417d345bb1333c54f9e0e955545e0` |
| H | backend | `7ef1820015604b2e208d5b12b90f83dec5110947` | `57dccce3283a23e668889c1573379b8e103065a0` |

## 实际检查与复用边界

测试集合有交集，以下按报告独立列出，不相加为唯一总数。命令、输入文件/锁文件哈希、PID、退出码和原始日志均保留。

| 检查 | 绑定源码 | 结果与证据（本机证据根目录下） |
|---|---|---|
| Python 定向组合 | backend `48f49357` | 849 唯一节点，0 fail/error/skip；`integration/python-combination.xml`，覆盖本批相关单测 |
| Graph/REST 最后改动相关单测 | backend `30a50990` | 117 唯一节点，0 fail/error/skip；`integration/python-graph-final.xml`；后续 c4fa/ada8 只加已实际运行的 legacy HTTP 测试 |
| 最终真实 Graph/REST | backend `ada8c202` | 20 项，0 fail/error/skip；`integration/graph-rest-report/junit.xml`；真实 HTTP/JWT/API key、SQL、Redis、Milvus，模型提取受控 |
| 列表 owner 隔离 | backend F10 精确文件哈希 | 6 场景，0 fail/error/skip；`integration/F-owner-exact/junit.xml`；真实成员、显式/空 scope、计数/分页及越权零读取 |
| C 暂停后端依赖 | backend `12c111d0` | 78 单测、10 HTTP/SQL/Redis 集成分别通过；`integration/C-backend-unit-rerun.xml`、`C-backend-integration/junit.xml` |
| Python 静态 | backend `48f49357`，最终改动补验 `ada8c202` | 改动文件 Ruff/format、受管 mypy、8 项 import 边界和 async DB 均退出 0；最终 5 个相关 Python 文件再做 Ruff/format，退出 0 |
| Go 模型组合 | backend `48f49357`；之后 Go 输入未变 | 1485 pass 事件，1332 叶节点、245 顶层测试、2 包；不重复计父/子节点；`integration/go-combination-summary.json` |
| Go 实际 SQL/HTTP | 同上 | 14 pass 事件、12 叶节点、6 顶层测试、2 包；真实隔离 PostgreSQL + 受控供应商 transport；A/B/H 合同组合通过 |
| Go 构建 | 同上 | 相关包 build/vet 与 server/admin/CLI 三入口均退出 0 |
| Web 最终全量 test:ci | Web `9cb81b04` | source Node 798、Vitest 624、desktop Node 81、tooling Node 10，四个独立 lane 共 1513，0 fail/skip；`integration/web-tests-delivery.log` |
| Web 最终构建/静态 | Web `9cb81b04` | build、改动文件 lint、agent strict、inventory、size 均退出 0；`integration/web-*-delivery.command.json` |
| E 标准 worker | E 验收源码精确哈希 | 94 单测与 3 实际 worker/HTTP/SQL/Redis/Milvus/MinIO 场景分别通过；paid embedding 响应受控；`handoffs/E-final.json` |
| G Infinity | G 验收源码精确哈希 | 原运行 19 通过/2 新断言失败；修正断言后只补跑 2 项通过。保留两次报告，不能称为一次全 21 项通过 |
| 实际消费者/UI | 各线 manifest 中后端/Web SHA | C 真实 Canvas Loop 初次暂停+两次恢复、Share/Widget；Explore/Studio 运行实际生产 hook/历史组件的 harness。D 生产选择/创建/chunk 组件与真实 API harness。F 完整 documents 页鼠标/键盘删除后 reload，SQL/native 独立读回。contact sheet 已逐组审阅 |

静态源码、受控 transport、真实本机服务、完整浏览器路径、远程 CI 和生产分别记账。
C 的 harness 不声称完整已登录 Studio 编辑器导航；D 不声称全站导航或真实供应商推理；
H 目录验证不声称现存业务库已重播种。G 在 Web 起点的 33 项客户端合同使用受控 transport，
F 的完整浏览器使用其 `380a2e26` 候选，最终 Web 全量检查绑定集成后的 `9cb81b04`。

## 失败、修复与仍未验证项

- 复审发现并修复 Novita SSE 多字节切分、Infinity 拒绝计数断言、Graph canonical SQL 原子性、删除菜单焦点以及同名附件误去重；均有定向回归或实际运行证据。
- 一次 Web `66ef0c78` 全量运行在未修改的 MCP 异步 timing 用例失败；相关源文件与初始基线哈希一致，当前 MCP+本批 UI 6 项定向通过。新增 C 附件修订后，最终 `9cb81b04` 四 lane 全量通过；保留前次失败，不将其描述为已修复的本批缺陷。
- Go `TestChatSessionScratchPostgres` 已知 multimodal 调用计数断言在基线 service/test overlay 复现失败；该 overlay 不是完整基线全套。G 更宽 CI harness 仍有 2 个未改动的基线失败（47 pass/2 fail）。均未放宽、未擅改，无全库通过结论。
- E 首次失败的原 raw log/JUnit 曾被覆盖，保留恢复的原始工具回执并明确原文件缺失，不伪造完整原日志。C 首次离线模型导入与集成 owner Ruff 首次相对路径启动失败也保留；后续正确环境/绝对路径运行通过。
- D 一次旧配置加载器忽略 DB override，向本机默认库 `xldu` 尝试插入 LLMFactories(OpenAI)；主键冲突拒绝，事务回滚，之前没有 DDL/commit。独立读回证明测试 UUID 不存在，已有 factory 行不同。原命令和 stderr 摘录保留，完整首次 stderr 不可恢复；未对业务库做清理。
- 集成 owner 在 F10 哈希断言失败后，shell 顺序错误使该项提前合入；随后用精确文件绑定、6 项真实场景和 849 相关组合补齐审查证据，已记录 `handoffs/F-review.json`。
- 未执行本机完整 `make verify`/全部 core；远程 `ci-required` 尚未运行。新 Web pin 尚未 push，远程 runner 当前无法检出本机新提交。没有 paid provider、生产服务切换、升级部署或业务数据迁移验收。

## 资源、WIP 与清理

每条线通过验收后才清理其登记的 scratch DB/role、Redis key/queue、索引/collection/bucket、容器、进程/端口及私有配置。
外部报告和验证过的 Git recovery bundle 保留，普通 worktree 移除后再以主干包含/patch 等价检查删除对应本地分支。
没有创建本批 managed worktree 附件，不归档其他聊天的工件；不清理共享容器、缓存、业务库或其他工作线文件。

集成 owner 独立连接核对：3 个 Python scratch DB、156 个精确 Redis key、25 个 Milvus collection、
21 个端口及 3 个 runner PID 均已不存在；40 份资源回执移出临时目录保留。
Go 的 3 个自建数据库及 1 个角色也已独立确认删除。最终各线清理读回、精确路径/分支删除结果见
证据根目录 `cleanup.json`、`handoffs/*-integrator-cleanup.json` 与 `integration/final-resource-readback.json`。

初始 28 项无关 WIP 已按文件哈希冻结。唯一共用文件 [PROGRESS](PROGRESS.md) 只暂存本批新增索引，
原有两段未提交工作未入本批提交；去掉本批索引后逐字节还原初始文件，其他 27 项哈希不变。
最终 Git/WIP、pin、资源/worktree/branch readback 全部记录于 `delivery-complete.json`，不以聊天完成状态替代交付证据。
