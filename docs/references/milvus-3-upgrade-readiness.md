# Milvus 3.0.2 升级准备

状态：2026-10-04 完成代码、官方文档和本机版本核查；2026-10-06 补充更新安全前置门禁。
服务/SDK 升级、nullable schema 和冗余向量字段退役尚未执行。
目标为 Milvus 3.0.2。生产版本、Standalone/Cluster、消息队列、数据规模和停写窗口待补齐，
当前不能确定生产升级路径或恢复耗时。本次没有升级服务、改写业务数据或改变依赖。

## 能力与语义

| 输入 | 应用语义 | 准备原则 |
| --- | --- | --- |
| `[0.0] * dim` | 有指定维度的全零数值向量 | 不等于缺失；不能据此判断生成成功，也不能一律转 NULL |
| `None` 或省略字段 | 向量尚未生成或该记录不需要向量 | nullable schema 下保存 NULL，向量索引与相似度搜索跳过它 |
| `[]` | 长度为零的数组 | 不作为非零维 dense vector 的缺失表示；在应用边界拒绝维度错误 |

Milvus 2.6.18 已引入 nullable vector；这不是必须等到 3.0 才能使用的能力。
见 [2.6.18 发布说明](https://milvus.io/docs/v2.6.x/release_notes.md#v2618)。
字段需要在创建时声明 `nullable=True`，已有字段不能直接切换 nullable；向量字段也不支持
`IS NULL` / `IS NOT NULL` 过滤。因此待生成、失败等状态需要可查询的标量字段，不能依赖向量空值过滤。
见 [Nullable Fields](https://milvus.io/docs/nullable-and-default.md)。

## 当前差距

| 核查项 | 2026-10-04 观察 | 影响 |
| --- | --- | --- |
| 依赖约束 | [pyproject.toml](../../pyproject.toml) 为 `pymilvus>=2.5.11,<3.0.0`；锁文件和本机 SDK 为 2.5.11 | 需独立处理 SDK 3.0.2 与 grpc/protobuf 的依赖解析 |
| Compose | [docker-compose-base.yml](../../docker/docker-compose-base.yml) 声明 2.6.22 | 不能据此推断实际服务版本 |
| 本机服务 | Docker 镜像和 `get_server_version()` 均为 `3.0-beta` | 本机既有验证不等于 3.0.2 验收，也不代表生产 |
| schema | [create_collection_with_mapping](../../core/utils/milvus_conn.py) 的 `vector` 和 `q_<dim>_vec` 都未声明 nullable；集合存在就返回 | 仅改建表代码不会迁移已有集合 |
| worker 转换 | [convert_data_types](../../core/svr/task_executor.py) 对缺失 FLOAT_VECTOR 补零，显式 `None` 会进入 `list(None)` | 即便 schema 支持 NULL，当前链路也不能正确使用 |
| adapter 写入 | [MilvusConnection.insert](../../core/utils/milvus_conn.py) 已跳过 nullable 字段的默认填充 | 必须同时修上层转换，不能只改 adapter |
| 字段选择 | ANN 使用 `q_<dim>_vec`，部分结果输出、重排和 RAPTOR 对 768 维选择 `vector` | dataflow 的正确 q 向量与补零标准字段可能走向不同消费者 |
| SDK 耦合 | adapter 使用 `pymilvus.client`、`orm` 和连接 handler 等内部接口 | 仅 SDK import 成功不足以证明读写、BM25、排序兼容 |

2026-10-06 只读复核本机服务仍为 `3.0-beta`，SDK 仍为 2.5.11。服务版本升级不会自动
更改旧集合字段类型，也不会移除应用层预删；两者需要独立修复和验收。

已有 dataflow 问题及复现边界见 [embedding 批量累积](embedding-batching.md)。
nullable 不会把已经存储的标准字段零向量自动还原为有效向量。

## 分阶段实施与独立提交

### 目标：退役知识库集合的冗余 `vector`

知识库 chunk 的最终物理 schema 只保留 `q_<dim>_vec` 作为该模型的 dense 向量，
有值时保存真实 embedding，业务允许缺失时保存 NULL；不再创建/索引/填充冗余标准字段。
nullable 与字段退役是两个独立改动：已有 q 字段变 nullable 仍需新字段或新集合方案。

| 名称相同但用途不同的 `vector` | 处置范围 |
| --- | --- |
| 知识库 chunk 物理字段 | 本次升级准备的退役目标 |
| 检索结果、对话引用的内存字段 | 保留输出契约，由 q 字段映射；[dialog_service](../../api/db/services/dialog_service.py) 仍消费它 |
| QA / 语义层独立集合 | [qa_service](../../api/db/services/qa_service.py)、[text_embedding_service](../../api/service/semantic_layer_service/text_embedding_service.py) 使用它作为实际 ANN 字段，不属于冗余字段清理 |
| 搜索模式、相似度分数与前端展示名 | 不属于数据库字段，不改名 |

2026-10-04 静态检查 Web 的 `DocumentChunk.vector?` 类型和搜索展示，未发现对知识库
物理字段名的直接依赖；不据此宣称前端端到端验收通过。同期 Skills 集合工作区也使用
独立 `vector` schema，属于其他任务，不能用全仓替换误改。

退役顺序：

1. 消费者统一读 q；修改 [chunk_app](../../api/apps/chunk_app.py) 两处手工新增/编辑的双写，
   worker 普通/dataflow/RAPTOR 及后续处理取文档路径；兼容逻辑按集合实际 schema 决定是否双写。
2. 新集合采用只含 q 的 schema；旧集合仍有非 nullable `vector` 时暂时双写真实值，直到该集合迁移完成。
3. dry-run 证明只有标准字段的历史向量已经迁走、冲突得到处置；全部 API/worker 写入方均升级，
   旧版本回滚窗口结束后，再删除旧物理字段。
4. 删除后检查 schema、索引、写入 payload 和 `$meta`，确认 dynamic fields 没有重新收进同名冗余数组；
   更新全量记录读回/更新逻辑，防止 `output_fields=["*"]` 再次带回并写入旧数据。

Milvus 3.0 提供 `drop_collection_field()`，可删除非最后一个向量字段，关联索引随 schema 清理；
空间回收由后续 compaction 处理，不保证立即下降。字段被 Function 使用时还有限制。
见 [Alter Collection Schema](https://milvus.io/docs/add-fields-to-an-existing-collection.md#drop-user-defined-fields)。
若原地删字段经过隔离演练且不需要改变 q 的 nullable，可避免为“删 vector”单独全量重建；
若同时迁 nullable，则仍优先评估新集合迁移。此处没有执行删除。

### 0. 先完成更新安全门禁

2026-10-06 排查图片 PATCH 时，API 的合法 `tag_kwd: list[str]` 与既有集合的
`VARCHAR(256)` 不同；普通更新绕过插入路径的转换，应用先删除原行，再因 SDK 类型校验
失败而丢失切片。这是 adapter 写入顺序与 codec 的缺陷，当前 SDK 已有原生 upsert，
无需等待 3.0.2。只升级服务/SDK 不会解决旧 schema 与列表类型不匹配。

[MilvusConnection](../../core/utils/milvus_conn.py) 的普通更新、反馈权重更新与重复主键插入使用单次
完整行 upsert，不再应用层预删；更新保留未提交字段、向量和创建时间，检查写入数量。
按实际 schema 编码标签/特征与位置字段，读回标签恢复列表/对象，BM25 输出交给服务重新生成。
更新条件保留主键之外的文档/知识库限制，无法表达的条件明确失败，不可扩大更新范围。
结构化标签超过 VARCHAR 长度时拒绝写入，不截断 JSON。

升级前后都运行 [adapter 专项](../../tests/integration/test_milvus_safe_mutations.py) 和
[图片 PATCH 真链路](../../tests/integration/test_chunk_image_replacement.py)，独立 Strong 读回：

- 空标签、中文与包含空格/标点的标签、特征对象、位置、BM25 字段均能往返；旧空字符串与
  worker 逗号分隔标签保持可读，本次 API 的列表/对象写入用 JSON 保留标点。worker 仍会预先
  转成逗号字符串，后续需统一 codec；历史逗号分隔本身不能区分标签内逗号。
- 确定未提交的类型/长度/向量维度校验失败或写入拒绝保留原行，正文/向量/创建时间及图片对象和 SQL 计数不变。
- 重复主键插入失败保留已有行；错误/数量不符不能报成功；网络超时不能自动重放可能已提交的增量更新。
- 单次 upsert 不等于客户端查询到写入的并发事务，不同批次/集合也没有整体事务。网络结果不确定时
  需独立读回；恢复旧快照前需比对来源身份、向量/schema 和后续编辑，不能覆盖已出现的新行。

3.0.2 + 配套 SDK 的 [partial update](https://milvus.io/docs/upsert-entities.md) 可作为后续优化候选，
先验证省略字段、显式空值、ARRAY/JSON 覆盖、动态字段与同切片图片/标签并发修改，再决定是否替代
完整行合并。当前 2.5.11 SDK 不发送 partial-update 请求字段，仅传 kwargs 不能启用该能力。
结合 [CAS 修复](https://github.com/milvus-io/milvus/pull/52495) 验证冲突与有限重试；ARRAY 增删不可
按普通替换重放。候选能力不能代替本节的失败保留门禁。

标签原生化可一起评估 `tag_kwd: ARRAY<VARCHAR>`、`tag_feas: JSON`，这些类型本身不需要等待
3.0；旧 VARCHAR 不会自动转换，需显式新集合/新字段迁移。先统一 API/worker 的读写 codec，
并适配标签筛选、聚合与特征排序：当前 ARRAY filter 字段未包含 `tag_kwd`，聚合也未按数组元素
计数，不能仅改 schema。验收容量/长度/JSON 大小越限拒绝、中文/空格/标点与 JSON 查询路径转义，明确
ARRAY/JSON 的整体替换语义，partial update 不会自动合并 JSON 内键。历史 CSV 歧义列清单，
不猜测拆分；在隔离集合演练回填、切换与回退后再决定迁移。保留 VARCHAR 时也需统一共享 codec。

已有数据丢失需要独立恢复，提交代码不会重建被删原行；常驻 API/worker 需受控重载后才使用新代码。

### 1. 先统一有效向量的读写契约

- 以模型维度对应的 `q_<dim>_vec` 为主要检索向量；旧 schema 仍需要 `vector` 时，普通 worker、
  dataflow、RAPTOR 使用同一结果双写。不能将一个维度的向量填入另一维度的字段。
- 统一 [search.py](../../core/nlp/search.py) 与 worker 的读取规则，去掉按 768 特判字段的分歧。
  兼容只有标准字段的历史记录时，先验证 schema、维度和来源；两字段冲突记录为异常，不能悄悄覆盖。
- 将“未生成”和“生成失败”与成功状态区分；生成失败不能借助 NULL 变成任务成功，token、chunk 账本仍需一致。
- 新转换逻辑根据实际 schema 保留 nullable 字段的省略/None，拒绝非法维度和非有限数值。
  旧 schema 缺少应有 embedding 时明确失败；确需保留的历史占位行为必须按记录用途限定。
- 回归普通任务、dataflow、RAPTOR、结果输出和重排；零向量、NULL、空数组分别验证。
  此项可以先于服务升级完成，不必等待生产环境信息。

### 2. 做只读盘点和修复 dry-run

按数据库、集合及知识库输出 schema/索引、模型与维度、记录数和下列分类计数；分页扫描、限速、可恢复，
默认不写数据、不输出正文。抽样结果不能冒充全量污染比例。

| 历史记录 | 建议处置 |
| --- | --- |
| q 字段有效，标准字段为已确认的历史占位零 | 按契约复制已有向量，无需重新调用 embedding |
| 只有标准字段有效 | 结合模型与 schema 确认后补 q 字段 |
| 两字段都有值且不一致 | 输出待人工/规则判定清单，不自动选一方 |
| 两者缺失或疑似全零 | 用任务状态、记录用途和来源判定；真正缺失才迁 NULL/重试 |
| 维度错误、NaN/Inf、模型不匹配 | 阻止迁移该批并记录原因 |

未来写入工具必须支持显式 apply、断点、幂等、写前比对和独立读回；已有主键不能按重复 insert 假定覆盖。
并发变更需停写或使用有边界的增量追赶，防止覆盖新生成的向量。
零向量本身不能证明它是补零产物。

### 3. 隔离验证 Milvus 3.0.2 + PyMilvus 3.0.2

官方 [3.0.2 发布说明](https://milvus.io/docs/release_notes.md#v302) 列出的 Python SDK 也是 3.0.2。
建立独立目录、端口与数据卷，不覆盖正在被其他任务使用的本机 beta 实例。
依赖变更需检查 Python 版本范围及 grpc/protobuf 约束，记录锁文件 diff；不要直接放宽全部依赖。

| 验证层 | 必须提供的证据 |
| --- | --- |
| 旧 schema 兼容 | 原集合备份在隔离环境恢复后，普通/dataflow 写入、读回、ANN、BM25、hybrid 均通过 |
| 新 nullable schema | 有值、None、省略字段混合批次；非 nullable 拒绝缺失；空数组/错误维度拒绝；NULL 不进入 dense 搜索 |
| 更新与恢复 | 元数据更新保留向量，补写 embedding 后可检索，清空向量符合业务状态；失败重试不丢行、不重复计数 |
| 真实消费路径 | 768 及非 768 维，RAPTOR、父子 chunk、graph、引用/结果输出、权限与 available 过滤 |
| 其他集合入口 | [基类建表](../../common/doc_store/milvus_conn_base.py)、[memory adapter](../../memory/utils/milvus_conn.py) 的独立 schema/生命周期；不要给全部向量字段统一放开 nullable |
| SDK 行为 | 内部 handler 调用、函数生成的 BM25 字段、索引状态、过滤/分页；query 排序能力开关仍需单独验证 |
| 质量与性能 | 固定检索集的召回/排序差异、P95、错误率、吞吐、索引体积和资源峰值；迁移前后采用同一参数 |

BM25 分支可能命中 dense 向量为空的记录，hybrid 不应假定每条命中都有 dense 向量。
是否允许这些记录进入业务检索由生成状态与可见性决定；不能把 NULL 跳过 dense 搜索等同于整行不可检索。
索引创建后的状态必须独立检查，不能只以当前 adapter 捕获异常后继续返回作为成功证据。
Python 修改执行 `make verify`；存储/检索修改另执行 `make integration` 与上述专项。

### 4. 分开演练服务升级与 schema 迁移

先保留字段和索引策略验证服务/SDK 升级，再启用 nullable schema，便于定位差异。
默认评估新集合迁移：保留旧集合、分批复制、校验后切换路由。当前集合名由
[index_name/index_name_one](../../core/nlp/search.py) 推导，不能假定已有 alias 切换能力；
实施前需设计读写、创建、删除都一致的路由，避免删错物理集合。

3.0 也支持新增 nullable 向量字段并回填，可作为数据量大时的备选，但这不等于能更改已有字段的 nullable。
选择哪条路径要结合磁盘余量、窗口、回退要求和消费者字段适配。
不要在第一轮同时更换 embedding 模型、ANN 索引算法、消息队列或启用 Storage V3。

### 5. 生产切换与恢复门槛

官方 [Standalone Compose 升级指南](https://milvus.io/docs/upgrade_milvus_standalone-docker.md)
验证的是 2.6.20 到 3.0.2，要求保留 etcd、对象存储、消息队列、卷和配置。
若生产为 2.5.x 或 Cluster，需按对应部署方式重新制定路径，不能套用该流程。
该专项指南明确不保证写入 3.0.2 后仅降级镜像即可恢复；恢复方案按升级前元数据和持久数据备份设计，
并先在隔离环境实际恢复。不能用一般发布说明中的兼容描述替代恢复演练。

切换前需留存：版本/image digest、schema/索引/functions、collection 路由、配置和依赖清单，
一致时间点的元数据/对象存储/WAL 备份与应用数据库/队列对账依据。
停写范围覆盖 API 写入、worker、dataflow、连接器及定时任务；清空或记录在途任务边界。
校验主键/记录数、向量与元数据抽样哈希、任务账本和检索基准，记录停止切换和恢复阈值。
恢复到旧状态时必须说明切换后新增写入如何重放；旧集合保留本身不保证这部分数据存在。

## 待补生产信息与本次验证范围

- 当前 server/SDK 精确版本、Standalone/Cluster 和部署管理方式。
- etcd、对象存储、MQ 类型/版本、存储格式、认证/TLS 和网络边界。
- 集合/分区数、总行数与向量维度、索引体积、增长速度、磁盘与内存余量。
- 可用停写窗口、RPO/RTO、备份及最近一次实际恢复结果。

本次只读查询本机 `127.0.0.1:19530` 的服务版本并核查代码/依赖/官方文档；
未扫描生产集合、未统计历史异常比例、未进行 3.0.2 服务或 SDK 验收、未进行备份恢复演练。
文档路径、diff 与实现一致性已检查；纯文档准备不触发全套 Python 门禁。
