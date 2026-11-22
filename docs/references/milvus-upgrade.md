# Milvus 升级与恢复

本仓默认组合为 **Milvus 3.0.6 + PyMilvus 3.0.3**。SDK 的
[3.0.3 发布说明](https://github.com/milvus-io/pymilvus/releases/tag/v3.0.3)
明确推荐这一组合；服务版本来自
[3.0.6 正式发布](https://github.com/milvus-io/milvus/releases/tag/v3.0.6)。
升级 SDK、声明镜像、启动隔离实例和切换业务服务是不同动作，需要分别记录结果。

## 升级路径与边界

官方 [Standalone Compose 升级指南](https://milvus.io/docs/upgrade_milvus_standalone-docker.md)
支持从 2.6.x 升级到 3.0，已验证的基线是官方 2.6.20 Compose；2026-11-15 页面目标仍为
3.0.5，不能把该页面当作 2.6.22→3.0.6 已完成验收。该路径没有要求先升级到 2.6.26。
其他部署方式、消息队列或更早源版本应使用对应官方路径重新验证。

升级时保留原 etcd、对象存储、Woodpecker/其他既有 MQ、数据卷、bucket/rootPath 和自定义配置。
本仓 Standalone 当前依赖为 etcd 3.5.25 和 MinIO RELEASE.2024-12-18T13-15-44Z；
服务升级不顺带更换这些依赖、MQ 或启用 Storage V3/LoonFFI，也不修改集合字段、embedding 模型或索引策略。
存储格式与 schema 变更另见 [数据能力准备](milvus-3-upgrade-readiness.md)。

官方 3.0.6 Compose 仍固定 etcd 3.5.25 和 MinIO RELEASE.2024-12-18T13-15-44Z，
并未选用各依赖独立发布的最新版本。镜像来源需另行区分：

| 组件 | 本仓现有部署来源 | 官方 3.0.6 Compose |
| --- | --- | --- |
| etcd | `quay.io/coreos/etcd:v3.5.25` | 相同 |
| MinIO | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | `milvusdb/minio:RELEASE.2024-12-18T13-15-44Z` |

相同 release 标签不保证跨仓库镜像内容相同；已核对的 ARM64 配置 digest 不同。
升级现有 Milvus 时保留原对象存储，镜像来源替换另作数据、S3 接口和恢复验证。

**3.0.6 镜像以 `milvus:milvus` 用户运行。**
其 [官方 Compose](https://github.com/milvus-io/milvus/releases/download/v3.0.6/milvus-standalone-docker-compose.yaml)
增加一次性 volume-init：按镜像内 `milvus` 用户的 UID/GID 调整 `/var/lib/milvus` 所有权并写 marker。
旧 root-owned 卷不能只改镜像后假定可写。先在克隆卷验证该步骤；从其他所有者的备份恢复后，
清除对应 `.milvus-volume-owner-*` marker 并重新初始化。不要对共享目录盲目递归 chown。

不能用更换回旧镜像作为恢复方案。官方明确提示 3.0 写入后的状态可能无法由 2.6 读取；
恢复应使用升级前同一停写边界的 metadata、对象数据和 WAL/本地持久数据。
切换后新增数据需另有可重放来源，恢复旧快照不会保留它们。

## SDK 与应用检查

[pyproject.toml](../../pyproject.toml) 使用 `pymilvus>=3.0.3,<3.1.0`，锁文件固定 3.0.3。
本次解析保留 grpcio 1.67.1 / protobuf 5.29.6；PyMilvus 不再默认安装 Milvus Lite 和 ujson，
不把它们作为远程服务端运行所需依赖。
PyMilvus 3.0.3 对 ORM API 发出 3.1 移除警告；本仓 adapter 仍有 ORM/内部 handler 调用，
因此未来 3.1 需要独立迁移，不能放宽上界后直接重锁。

镜像版本与服务器返回串分别留存。本次官方 3.0.6 镜像返回
`3.0-20261110-63100f8f5b4`，不能只用完整字符串的严格语义版本解析决定能力。
原生 Query ORDER BY 需要真实服务支持；SDK 升级不能替代服务升级。
schema 中 VARCHAR 时间必须保持可排序格式，显式唯一末尾排序键用于打破相同时间的并列。

### 应用已采用的原生能力

| 路径 | 当前行为 | 兼容与并发边界 |
| --- | --- | --- |
| 最近记忆分页 | 已确认的 3.x 连接使用双字段 `order_by` 和 seek 条件 | 旧服务保留有界完整读取；游标变化检测见 [Memory 查询](memory-query.md) |
| 知识库标量排序 | 单集合、普通实体投影且比较语义一致时，服务端排序后只读当前页；另用相同筛选的 `count(*)` 返回匹配总数 | 正数页大小，`offset + limit <= 16384`；计数与页是两次 Strong 读取，不提供原子快照 |
| 完整记忆写入 | 一次官方 `upsert` 替换完整行，核对确认数量，取消原来的预先删除 | 校验或写入失败不会由应用预删旧行；响应丢失仍可能已经提交，必须读回核对 |
| 记忆状态、遗忘字段 | 已确认的 3.x 连接只提交主键及变更字段，使用 `partial_update=True` | 不重写正文、向量或其他元数据；旧服务和其他字段更新保留完整行读后 upsert 路径 |

知识库的多集合合并、聚合、编码位置字段、不兼容的数值存储以及 nullable 降序仍用原有有界排序路径。
Milvus nullable 降序将 NULL 放在前面，与当前逻辑比较器的 NULL 末尾语义不同，不能直接替换。
旧排序路径仍有 10,000 行上限。已进入原生路径后的元数据、计数和查询错误直接传播，不能悄悄降级成局部结果。
官方接口及限制见 [Query API](https://milvus.io/api-reference/pymilvus/v3.0.x/MilvusClient/Vector/query.md)。

记忆局部更新采用官方 [merge upsert](https://milvus.io/docs/upsert-entities.md)，当前仅开放适合现有接口的状态和遗忘字段。
这不是面向调用者的 revision/CAS 合同，也没有新增正文修订、历史版本或级联撤回能力。
完整行读后 upsert 仍可能覆盖并发更新，或在读取后发生删除时重新写入旧行；需要版本约束的业务不能据此假定安全发布。
写入关闭 SDK 对未知 RPC 结果的自动重试及限流重试，不设置会覆盖重试次数的 SDK timeout 预算；确认失败不自动重放。
SDK 对明确的 schema/数据校验拒绝仍可能刷新 schema 后重试，不能将上述设置理解为所有失败都只发送一次 RPC。

在业务写入前验证：旧集合 schema、functions、indexes、分区、动态字段和权限；
插入/更新/删除、Strong 独立读回、ANN、BM25、hybrid、错误保留与应用真实消费者。
本机小样本读回不等于全量数据校验、负载测试或生产验收。

## 隔离冷备演练

[隔离 Compose](../../docker/docker-compose-milvus-rehearsal.yml) 不使用业务容器名或 external volume，
只绑定 loopback 高端口。一次只运行一套新增 Milvus，默认限制 1 GiB、2 CPU；
etcd/MinIO 也有内存上限。检查健康、RSS、OOM/退出码和日志，资源不足明确失败，不调整既有业务服务。

使用独立环境中的 SDK 2.5.11 建立源数据，再用按项目锁安装的 SDK 3.0.3 验证目标。
命令中的 `old_python` / `new_python` 指向这两个环境；升级时不要改写仍在运行其他任务的 `.venv`。

```bash
docker compose -f docker/docker-compose-milvus-rehearsal.yml -p multirag-milvus-upgrade-source up -d
curl --fail http://127.0.0.1:29091/healthz
"$old_python" scripts/milvus_upgrade_probe.py seed \
  --uri http://127.0.0.1:29530 --prefix upgrade_rehearsal_example \
  --server-version 2.6.22 --receipt "$evidence_dir/baseline.json"
docker compose -f docker/docker-compose-milvus-rehearsal.yml -p multirag-milvus-upgrade-source stop --timeout 60
```

必须等待 stop 命令完成，并确认三个源容器全部退出。运行中的 etcd、MinIO 和 WAL 分别复制，
不能得到可主张一致性的冷备；业务数据还需要先停止全部写入方并处理在途任务。

[冷复制 helper](../../scripts/milvus_clone_stopped_volumes.py) 仅接受自有演练 project，
拒绝正在使用的源卷和已存在的目标卷；源卷只读挂载，逐文件哈希在复制前后比对。
它不替代停写管理，也不支持业务卷或并发操作同一组演练资源。

```bash
"$old_python" scripts/milvus_clone_stopped_volumes.py \
  --source-project multirag-milvus-upgrade-source \
  --target-project multirag-milvus-upgrade-target \
  --receipt "$evidence_dir/cold-clone.json"
REHEARSAL_MILVUS_IMAGE=milvusdb/milvus:v3.0.6 \
  docker compose -f docker/docker-compose-milvus-rehearsal.yml -p multirag-milvus-upgrade-target run --rm volume-init
REHEARSAL_MILVUS_IMAGE=milvusdb/milvus:v3.0.6 \
  docker compose -f docker/docker-compose-milvus-rehearsal.yml -p multirag-milvus-upgrade-target up -d
curl --fail http://127.0.0.1:29091/healthz
"$new_python" scripts/milvus_upgrade_probe.py verify \
  --uri http://127.0.0.1:29530 --prefix upgrade_rehearsal_example \
  --server-version 3.0-20261110-63100f8f5b4 --native-order \
  --baseline "$evidence_dir/baseline.json" --receipt "$evidence_dir/upgraded.json"
```

探针覆盖旧 message schema、source=0、精确空/空白 subject、忘记/禁用行、更新与删除后的数据，
以及 BM25 function、JSON、动态字段、dense/keyword/hybrid 检索。比较完整小样本行与 schema 的哈希。
之后运行 `write` 模式并等其成功结束，再重启自有 Milvus，以新的 receipt 作为 baseline 读回。
写入异常时先独立核对提交状态，不盲目重放 insert/upsert，也不与重启并发。

恢复演练使用原始停写源卷**再克隆**为新的 recovery project，启动 2.6.22 并比对原 baseline。
不要让升级后的卷重新连接 2.6.22，不要恢复覆盖仍有新写入的目标。
测试资源清理只作用于自有 project，保留源备份和报告直到恢复验证完成。

## 同版本 Milvus 整栈冷恢复

复用[三存储同恢复点的登记命令](memory-compaction.md#三存储同恢复点演练)。在登记参数中增加 `--input tests/manual/memory_milvus_cold_volumes.py` 和
`--input scripts/milvus_upgrade_probe.py`，在末行模块命令后增加 `--milvus-cold`。
此路径使用已安装的 Milvus 3.0.6、etcd 和 MinIO 镜像，不需要下载 Milvus Backup。
源及目标都使用独占 Compose 项目和回环端口。生效的 PostgreSQL 逻辑备份与 Valkey RDB 路径沿用同恢复点演练。

1. 停写排空后，先验证真实镜像摘要、卷归属、挂载、etcd 前缀、对象存储 bucket 和前缀、Woodpecker 后端。当前隔离镜像包含四个持久挂载，其中 MinIO 使用 `/minio_data`，镜像同时声明了 `/data`，两处都纳入检查。各检查点先停止 Milvus，再停止 MinIO 和 etcd。服务退出码非零会阻断封存。
2. 封存每个已停止的卷，保留目录、文件及数字所有权和权限。目标只向归属已核实的空卷解包，复核卷集合、内外层哈希、文件内容和元数据，再依次启动存储依赖和 Milvus。源清退后才能使用归档。缺卷与损坏归档在还原前拒绝，三份完好但 Milvus 来自后一个检查点的归档由恢复后的状态准入拒绝。
3. 同点还原通过后，复用固定 Web、现有 worker 重投以及 Milvus 稠密、BM25、混合检索和排序探针。在独立探针集合执行插入、upsert 和删除，再重启 Milvus 及其依赖并读回持久结果。记录封存、存储启动与应用放行的耗时。演练使用合成数据，部署恢复目标应以对应环境和数据量的证据为准。

本地实测使用 Milvus `3.0-20261110-63100f8f5b4`、PostgreSQL 16.14、Valkey 8.1.5 和固定 Web `b355c64f`。
匹配目标从创建到准入耗时约 21.80 秒，受控应用启动后的首次固定 Web 回看耗时约 11.48 秒。四项负例均在应用读取和 worker 重投前阻断。
匹配目标在重投前后各完成 15 次固定 Web 读取，已撤回的来源原文均返回 404。两条待确认消息在真实 worker 入口处理后归零，模型调用为零。
探针重启后读回新插入、修改和删除结果。三个测试项目、卷、回环监听及归档辅助容器完成清退和独立读回。

默认逻辑还原与物理卷还原有独立验收记录。仓库目标为 RDB 时按相同 Valkey 镜像及配置引用已测的 RDB 证据。目标启用 AOF 或双模式时，
部署前须核对有效配置并验证完整 AOF 目录、重写后的增量分段以及负例准入。
[Milvus 3.0.6 Woodpecker 配置](https://github.com/milvus-io/milvus/blob/63100f8f5b4b675a12fe78fc9673b005ee318731/configs/milvus.yaml#L316) ·
[Milvus WAL 恢复测试](https://github.com/milvus-io/milvus/blob/8bcbb0770b2356d4e7842fe97100a5f0f7215902/internal/streamingnode/server/wal/recovery/recovery_idempotency_test.go#L25) ·
[Woodpecker 对象存储恢复测试](https://github.com/zilliztech/woodpecker/blob/74d188d6514dd6ba1a85aa53ba15e9910e6b0645/server/storage/objectstorage/writer_recover_empty_test.go#L37) ·
[etcd 损坏快照测试](https://github.com/etcd-io/etcd/blob/f872eb465ed3d2d056ec16742a1a2f8e322a5751/tests/e2e/ctl_v3_snapshot_test.go#L73) ·
[Valkey 持久化](https://valkey.io/topics/persistence/)

## 逻辑备份与恢复

另保留一个独立备份副本。推荐校验 release checksum 的
[milvus-backup 0.6.2](https://github.com/zilliztech/milvus-backup/releases/tag/v0.6.2)。
其 [兼容矩阵](https://github.com/zilliztech/milvus-backup/blob/32f084e841ebed20bb4289806d744002404b41db/README.md#compatibility)
支持 2.6 备份恢复到 2.6 或 3.0，3.0 备份不能恢复到 2.6。
使用工具的 v2 配置，确认 source Milvus 的实际 bucket/rootPath、management 和对象存储地址。

2.6 源显式采用 `create --format binlog --strategy bulk_flush`，备份目的地可为独立本地目录/对象存储。
该操作会 flush 源并暂停/恢复 GC，并非纯只读扫描；本机演练只作用于 scratch 源。
正常主实例恢复与 CDC secondary restore 是不同合同；后者还要求 index extra/full metadata 等条件，
不能忽略工具的限制警告。

按 [备份 FAQ](https://github.com/zilliztech/milvus-backup/blob/32f084e841ebed20bb4289806d744002404b41db/docs/FAQ.md)，
备份保存原始 insert/delete binlogs 和 index metadata，实际索引文件不备份，恢复后需重建并验证。
恢复到一个空的独立 3.0.6 实例，等待 import 和索引结束，再运行同一探针对照 baseline。
`restore --strategy merged_seg` 使用 binlog 恢复路径；这不等于完整 etcd/WAL 冷备恢复。

3.0 源默认变为 snapshot backup。其 `restore_snap` 要求备份和目标使用同一存储服务，
另一套自建 MinIO 不算同一服务；3.0.4+ 的显式 `import_snap` 可以跨服务读取，但只支持其要求的
Storage V3 snapshot。不要在本次 2.6 binlog 演练中混用这些路径。

## 业务切换清单

1. 记录精确 image digest、SDK lock、配置/secret 引用、数据库/集合/分区、schema/functions/indexes、
   行数、主键/向量/元数据校验和、存储与内存余量。来源凭据不写入报告。
2. 停止 API、worker、connector、dataflow 和定时任务写入，记录队列和 SQL 账本边界；
   flush 成功后停止 Milvus，再停止相关持久依赖并制作一致冷备。备份独立于待升级卷存放。
3. 在副本按上述路径完成读写、重启、逻辑备份恢复和旧版本冷备恢复，估计实际 RTO。
   明确升级后增量重放方式、失败阈值和切回条件。
4. 正式窗口只执行已经演练的 Milvus image/owner-init 变更，保留既有依赖、MQ 和配置；
   业务保持停写，先验证旧数据和真实消费者，通过后再开放写入。
5. 恢复时关闭目标写入，保留失败现场，从升级前完整副本恢复并核对账本；
   不执行 `down -v` 删除旧备份，不用镜像降级代替数据恢复。

本仓准备与本机隔离演练不授权远程生产部署，也不证明真实业务全量数据已经迁移。
