# 0580c137 后续 20 项：本地集成与定向验收

2026-10-22 本批 **20/20** 完成。仅本地提交，**未 push，远程 ci-required 待运行，未部署**。定向组合结果不代表完整 CI 通过。

## 范围与组合

上游 `0580c137fa2eaac8f8774ce32076d1003274c2a7` 之后 first-parent 连续 20 项，首项 `530edbac999b515e646abcd02dd08b3400819fb6`，末项 `9b3850339bc0ea29eb691dbad28811bb9dd81e31`。后修审查上限保持 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。核对的 `infiniflow/ragflow` origin/main 为 `4fd8c439af2cf31e19731675e27bbb9ccb854bf1`，仅用于相关后修分析，不推进本批游标。

后端基线 `ff6572f8bf0ce6e85459d813a5c187a3289e501a`；Web 基线 `eed958f792c52fac91490acec6a9eae00edd659a`。
最终源码组合：后端 main `5559786ad731527ae2b524862c490635286fbe07`，Web master `90e16ff204e533806e3e23851dcbb8adc1b452d6`；`.github/consumer-web.json` 唯一 pin 已固定该 Web SHA（`72b61cd6`）。本报告及 PROGRESS 最小索引另作文档提交，含文档最终 HEAD 与清理结果见外置 `delivery-complete.json`。

## 逐项结论

上游列为完整原始 SHA；候选与本地映射用唯一短名。完整 source→main/master 与差异足迹在外置 `integration/item-mapping.json`、`handoffs/*-final.json`、`handoffs/*-accepted.json`。第8/10/14/16/17等价或空项均核过实际消费者，不制造空提交。

| # | 上游 SHA | 结论 | 候选 → 实际 main/master | 行为与比较 |
| --- | --- | --- | --- | --- |
| 1 | `530edbac999b515e646abcd02dd08b3400819fb6` | 本地适配 | a22b7c49 → d6d2e879 | LM Studio 补全 embeddings、可选 key、目录后缀和维度转发；严格校验每个索引与向量。 |
| 2 | `13e6554901d7ae0c2a987b63a312c003ded1edd7` | 严格等价并补齐 | df77fed1 → 072c584d | OpenRouter 原 decoder 已比上游更严格；沿用 null/缺失/重复/非有限拒绝，补 Dimension 路径。 |
| 3 | `cc207b5b05532f6296e72bbe01e9813ae0ead7e1` | 本地适配 | 5896cf2f → f1fd0430 | file_service 线程池用 context manager，输入/提交/结果失败仍等待兄弟任务；顺序与图片保留。TOC 既有 shield+线程排空更强。 |
| 4 | `3838770e7a8074d3e7be2933562ba2862c3515ce` | 本地适配 | 10691525 → fcae981c；7d89cf09 → 47b4cd8e；Web 90e16ff2 | GraphRAG 增加 NER 和中英文固定 spaCy 资源；保留原始 chunk、全部出现与描述、中文身份；推理离开事件循环并排空取消。补 Apple/小写 apple 源 span 混淆。 |
| 5 | `0734fd793a9b23cc1f4d916a6f8d8453f06f3b15` | 直接移植 | 5e1ae5f9 → a2cfc724 | pending_cell_images 在每个 sheet 内重新建立，避免跨表图片污染。 |
| 6 | `16354f4e1470f792a3ad9c97d0e049158b72bf75` | 本地适配 | ef473cde → 9d328b8d | Dify 路由局部捕获请求校验，缺参和畸形参数返回 Dify 400；安全参数不回显敏感查询，授权先于模型/存储。补空白 query 与零权重。 |
| 7 | `46897d6fa44296bdb32f6825453ee73eb2c13b02` | 本地适配 | 1eb934a5 → 00b0befb | 可信 Principal 区分 WEB_SESSION 与 SDK_API_TOKEN；JWT 消息归属 actor，API key 精确保留请求 user_id，包括空白/空值。 |
| 8 | `024c8cb0b56815ce2159cddbdd00f3e04abc6e9b` | 等价保留 | 后端 ff6572f8；Web eed958f7 | 活动 SearchDatasetRequest(s) 已为 int|null，真实 Web 客户端为 number|null；保留租户归属预检，不恢复废弃重复 schema。 |
| 9 | `a03b95f8c448e2c422d1cd0d6bc1e98f098894df` | 本地适配 | c9a7b39e → 638bbfc5；59fb354f → e4560299 | chunk 路由按已授权 KB owner 找索引与数值模型，删除图片传具体索引；Milvus 点读用 Strong 保证修改后实际 Web 立即读回，不改变全局检索一致性。 |
| 10 | `5ef7f50eef15fbe74e566649fe92e43b865e0070` | 由第3项覆盖 | 5896cf2f → f1fd0430 | 同一 file_service 线程池生命周期已完成；不为等价写法再加空提交。 |
| 11 | `c55e23e7e263c60715aedd7716bcff19e3b38e53` | 本地适配 | 0d77c9cb → 5c36a083 | Go embedding 从 Encode 统一为 []EmbeddingData 的 Embed；所有 provider/handler/CLI/NLP/skills 迁移，32 条批次用全局索引，输出真实向量与计算维度。 |
| 12 | `a0efc453f3834e5269596d3804884008d66653cf` | 本地适配 | 2af36e72 → 8cd0da4e | reset_password 空字符串在 Redis 前拒绝；本地原为明文密码且只有一次 Redis get，不增加无调用方的解密步骤。 |
| 13 | `6ce014c23b6aee2bd42631f3e9bd88ca5c9161e2` | 本地适配 | d9f24d94 → c9eaeca7；443a4fe9 → 61fa4305；e02db524 → d082165e；4b0a22b2 → ecb87b7d | 保留 request AsyncSession.run_sync 的事务；仅深复制反馈 payload 的自有外部 IO to_thread。工厂 ID 查询绑定租户；Retrieval 以可信 actor 授权全部选择，再按 KB owner 构建 ID/name embedding。 |
| 14 | `592dba14891e21ed31eaefcb2ccd7714ff984f67` | 等价保留 | 后端 ff6572f8；新增回归随 C/G | 既有 SQLAlchemy helper 已约束有效成员、角色、private creator 与管理角色；sync/async/document/name 消费者复用，原始 tenant_id 不能授权。 |
| 15 | `6fb8c31c22430d24bb3f8584fd46ac4081b213ac` | 直接移植并适配 | 802a9064 → 00bc7141 | ES/OS bulk insert 使用 refresh=wait_for，失败不伪成功；实际 worker DONE 后独立客户端立即可查。 |
| 16 | `1e80be77a2b2cc7ea047045420fe5e7db2b1fbf4` | 等价保留 | Web eed958f7；最终 90e16ff2 | 活动 DOCX Mammoth/SafeHtml 容器已有 overflow-auto；长引用末段、Tab/PageDown/End/Home 在最终 Web 的四种视口/主题组合通过。 |
| 17 | `c58906b69e472bdd277d9eb4b8bf3ec11c342b1d` | 空提交审结 | 上游 c58906b6 与父树相同；有效前置 c2ce49e0 | 不制造空移植。真实 ONNX 空白图和实际 OCR.detect/PDF 消费者已处理 None、空框并保留排序；不误改 __call__ 的 tuple 契约。 |
| 18 | `292b0b8bcee76e140686011f29317ec5b056b6f9` | 局部适配 | f4b80d91 → ae94b262 | 只修活动 ExeSQL dose→does；其余注释及 LLM 诊断已有更清楚的本地表达，无收益不机械替换。 |
| 19 | `663fc1d42cb26ec22e81f4f6e477094eb61a1f39` | 本地适配与补修 | 58d0e144 → 86c724cb；349684f8 → 5559786a | ES/OS 统一 metadata adapter，完整对象替换；count/update/refresh 严格回执，mget 完整 ID 集，删除需成功回执和独立 scoped absence。批量服务不把 False 计成功；保留恢复/删除可见性屏障。 |
| 20 | `9b3850339bc0ea29eb691dbad28811bb9dd81e31` | 本地适配 | 854e44aa → 854e44aa；7c26721b → d1545c15 | Go 构建第三个 CLI 二进制，检测 Apple PCRE2 和原生依赖；缺失明确失败，无 sudo/apt 副作用。开发指南扩展现有 docs/development.md，不入库 internal 笔记。 |

第13项参考同链 `7f699d12` 完成模型工厂与活动 Retrieval 消费者适配；第4项参考 `a9ec78cb`/`10e86908` 保留原始字符串 chunk。其他后修、撤回与不采用理由保存在各线 final，不增加游标项。
额外配套文档提交 `d1c80274` 更新 Go Embed API 与现有 Ollama v1/embeddings 描述；E fixture 修正精确 memory size-cache 清理 `6f5b3616 → a3e33d7b`。

## 验证与复用

证据根：`/Users/xldu/.local/state/multirag/validation/20261022-batch-after-0580c137/`。报告保存命令、exit、输入 hash、选中/排除项和服务 readiness。以下运行有重叠，数字不相加为全量用例数。

| 范围 | 实际证据与边界 | 报告 |
| --- | --- | --- |
| A/I Go | provider/entity/CLI/service/NLP/skills race、build/vet 与实际 handler/CLI 向量；根组合构建 server_main/admin_server/multirag_cli，字面指南 embed/rerank 成功，旧 dimension-only 输出拒绝。570 个最终输入未再变化。 | A 原始 race/运行日志；integration/review-A-independent.json、go-combined-build.json、go-guide-consumer.json、guide-consumer.json、go-source-inputs.json |
| B/H 解析与线程 | B 生命周期35与消息29；Excel/table-role30（含4项修前失败回归）；真实空白 ONNX 与 OCR/PDF None/empty/sort。TOC 真实线程结束/异常/取消排空保留。 | B/lifecycle-unit-final.xml、message-unit.xml；H/excel-and-table-roles.junit.xml、ocr-detect-audit.junit.xml |
| C 权限与即时读回 | 定向126 unit、10真实 HTTP/SQL/Milvus/最终前 Web；模型/KB owner、shared/private/inactive、混合拒绝零副作用；发现修改后立即点读旧值后补 Strong 并重验。最终 Web 客户端另进入根组合。 | C/integration-verified/；integration/combined-report/ |
| E 身份与重置 | 原76 unit 中67无变复用；归属修后9 unit、真实1归属与1清理检查重新执行；密码重置/权限矩阵真实9项范围证据复用。初始 strip/owner fallback 已修，API key 精确 user_id 保留。 | E/final-static.json、E/unit-attribution-final.xml、integration-attribution-final/、integration-cleanup-final/；handoffs/E-final.json |
| D ES/OS | 实际 ES8.11.3、OS2.19.6、SQL/worker；第19项241个唯一 focused unit（两报告重叠），后续修复125 unit与4真实 CRUD/HTTP。独立复现缺失/布尔回执、部分 mget、永久删除失败。旧值及_seq_no保全，解锁清空、异KB保全、重复删除合法零。 | D/review-repair-final-*；integration/review-D-independent.json、review-D-followup.json |
| F NER/Web | 私有 frozen env、spaCy3.8.14、en/zh model3.8.0；未变包版本，numpy保留1.26.4。原82 unit 与受影响7最终NER；2真实中英文 HTTP/SQL/Milvus/Redis图持久化检索；实际模型推理，无 chat 调用。Web build/lint(0 errors/1366既有 warnings)、19 settings、4实际检索客户端；浏览器选择保存、code0、重新加载NER及独立SQL。 | F/checks.json、source-inputs-final.json、integration-case-fix/、browser三份白名单业务/SQL报告及标注拼图 |
| G 事务/模型 | 反馈占用4线程时 SQL HTTP 探针继续运行；未提交请求态仍同事务。89 factory unit、42 clean集成，16 Retrieval unit与26集成，等值fixture迁移27集成；拒绝选择先于模型/metadata/store。 | G/integration-review-final/、integration-retrieval/、integration-fixture-final/；G/fixture-migration-equivalence.json |
| F/H 最终Web | Web 90e16ff2 上真实页面组件、Mammoth/SafeHtml，160段DOCX；1280×900/390×844及明暗主题四组合 Tab/PageDown/End/Home、末段可达、无横溢。F配置保存浏览器证据按相同提交复用。 | integration/docx-browser-final.json、FH-web-inputs.json、docx-contact-sheet.png |

根组合锁定 `5559786a`，使用 F 私有 frozen env，独立 unit/integration 进程。392 项定向 unit、57 项真实服务集成检查通过，failure/error/skip 均为0。
涉及54个Python改动文件的 Ruff check/format、两个纳管类型入口、8条 import contracts、async DB 和 diff 检查通过。根 integration 在收集前确认 PG16.14（自有scratch PG）、Redis、MinIO、Milvus2.6.22、ES8.11.3、OS2.19.6；REQUIRE_SERVICES=1，不以缺环境 skip 换绿。实际命令见 `integration/run_final.py`，记录见 `unit-combined.json`、`integration-combined.json`、`combined-report/run.json` 与 `final-source-inputs.json`。

全局 test-to-test import AST 门禁发现基线 `test_retrieval_parent_http.py` 两条跨 test 导入（ff6572f8逐字相同）；本批新引入项已迁 tests/support，未修无关基线或称该全局门禁通过。报告见 G/unit-fixture-harness.xml、fixture-harness-static.json、D/baseline-import-failure.json。

修前失败与环境问题保留：C空白query/零权重与即时点读，D回执/部分mget/删除错误，E subject规范化和缓存清理，F person-only校验fixture与source大小写，G早期Redis EVALSHA记录器、fixture跨test导入。只复跑受影响范围，未削弱断言。根DOCX首开缺少本地VITE_API_BASE_URL，补测试进程环境后最终四组合通过；未改共享Web配置。

## 清理、WIP 与限制

初始后端28个WIP路径已保留原文件、补丁及SHA-256，未stash/reset/clean或宽泛暂存。每次main快进均逐字节核对；最终PROGRESS只加本批索引，移除该索引后与原WIP文件完全相同。Web收尾期间出现的其他README/assets四个文件另存hash，未修改或暂存。

九线只在整线accepted后清理自己的工作树；A/B/C/E/H/I早先完成，D/F/G也已完成。根复核source→main全部23个patch-id等价、恢复bundle有效、无工作树引用后删除本批源分支（含Web F）。各线cleanup保留历史边界；C服务absences复用coordinator/worker证据，未称本轮根重复查询。

根最终组合结束后独立fresh读回：2个scratch DB、23个集合、4个Memory ID后缀、61个Redis键、7个桶、12个ES/OS索引、14个listener均无残留（combined-independent-readback.json）。创建前日志未完整覆盖Milvus ORM/Message路径；图/C fixture清单在运行后回收，Memory按精确UUID键的后缀核查，不倒补历史登记。D/F/G清理后另独立确认13个DB、288个键、F七个集合、12个端口、13个PID及D三个容器/一个匿名卷、四份私有登录/覆盖文件均不存在（DFG-independent-cleanup-readback.json）。F最早rerank场的逐项collection清单已不保留，维持旧fixture与namespace读回边界，不声称根补齐了原始ID证据。

本报告与最小索引提交后归档根自有集成工作树，并删除其本地分支；最终目录/分支/资源/PID及WIP读回、含文档HEAD记录在外置delivery-complete.json与integration/cleanup-final.json。未prune其他任务的工作树、分支、容器或业务数据，所有外置证据和bundle保留。

未执行完整 make verify、完整 core四分片、远程最终组合CI或生产切换。实际供应商调用、模型领域质量、生产负载/SLA、Linux新原生构建未据本地证据验收。A明确排除的TestChatSessionScratchPostgres、TestRetrievalScratchPostgresInfinity、TestSkillsLiveServer未运行，不把selected零skip当作其已通过。Go race父/子TestAction不是独立业务场景数。

NER句内共现关系不是经验证事实；仍有每进程模型CPU/内存开销，下游实体归一/社区/摘要按配置可能调用租户LLM。F NER验收用真实spaCy、SQL/Milvus/Redis，embedding与chat边界受控；Web DOCX控制预览字节端点，未覆盖完整认证壳。C点读Strong有读延迟取舍。各线受控transport/模型不证明真实云provider、完整常驻worker或生产部署。远程ci-required需后续push后运行。
