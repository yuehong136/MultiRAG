# 智能问数 · 语义层接口 30s 超时 — 根因与修复（可评审）

> 现场：2026-06-22，单问题"查询指导教师20032383的数据，列出所有字段"连问 7 次 **7/7 失败**，
> 每次都卡满 **30.2–30.8s** 后 `ApiRequestError: 所有 1 个模型ID的关系请求都失败了`。
> 两个不同 dataset 都中招；同一次请求里中台其它接口（模型详情 0.01s、业务术语 0.05s）全部秒回。
>
> **本文档含两案**（同一"引擎→中台语义层 30s 超时"失败族）：
> **第一案**（一~八节）= `getModelRelationships` 现场超时，真因 = `t_drm_dataobject` 缺索引；
> **第二案**（九节）= `getModelIndsAndDimsByModelId` 演示库偶发超时，真因 = 中台共享池争用（**非该 SQL**）。

---

## 结论速览（TL;DR，2026-06-22 最终）

- **症状**：智能问数语义层调用中台 `getModelRelationships` 卡 30~56s 超时（引擎 `ClientTimeout=30s`），整条问数失败；同一问题连问 7 次全挂。
- **真因（已在出事现场库实测确认）**：现场库 **`t_drm_dataobject` 缺索引**。该索引由各环境**人工维护、未纳入迁移脚本** → 飘移漏建。缺索引使该表在关系查询里被全表扫上万次（`EXPLAIN ANALYZE` 实测 56s，`Seq Scan on t_drm_dataobject … loops=13394`，过滤掉 8440 万行）。健康库（dev/demo）有此索引故 4~10ms。
- **修复（零改代码、零改原意、纯提速）**：补索引
  `CREATE INDEX index_drm_do ON t_drm_dataobject (database_wid, dataobject);`
  → 56s 降到毫秒级。跨环境拉齐用 [`sql/semantic_required_indexes.sql`](sql/semantic_required_indexes.sql)（幂等、按列覆盖判断只补缺失）。
- **中台 SQL 不用改**：下推改法在 PG 上是 no-op（优化器已自动应用该谓词），且救不了"缺索引"；仅作"计划稳定性保险"备用（详见第二、七节）。
- **引擎侧已做兜底（B，未提交）**：`askdata_service.py:421` gather 加 `return_exceptions=True`、模型关系失败降级 `[]`，使中台该接口抖动时问数仍能走完。
- **验证**：加索引后重跑诊断（[`sql/explain_getModelRelationships.sql`](sql/explain_getModelRelationships.sql) 区块3），看 `Seq Scan` 变 `Index Scan`、耗时降到毫秒。

### 遗留 / 待查
- ✅ **演示库（已有索引）仍偶发语义层超时 —— 已二查，见第九节**。结论：这次超时的是 `getModelIndsAndDimsByModelId`，且**实测该 SQL 在演示库仅 9.6ms / 2.8ms、PG 端 1000 连接仅用 139**，矛头转向**中台共享连接池/线程池争用**（非该 SQL、非 PG）。已落两道引擎防线（B 扩到 dims/metrics + 全局出站并发限流）。"按关键字四件套"等重查询仍是占池嫌疑，待中台侧池配置 + 真超时每问日志坐实。
- ⏳ **待中台侧确认**：HikariCP `maximum-pool-size` + 高峰 active/pending、Tomcat max-threads。
  - **定位法不变**：抓那次超时的 `ask_id`，开每问日志（见 [[project-askdata-logging-system]]）看卡在哪个接口的 `timer`、是否出现第九节那条"出站并发已达上限"的排队 WARNING。

---

## 一、根因（中台 SQL）

接口 `POST /api/drm/semanticOpenApi/getModelRelationships`：
- Controller `SemanticOpenApiController.java:164` → Service `SemanticOpenApiServiceImpl.java:391`（纯透传）→ DAO `SemanticOpenApiDao.java:69`
- 慢 SQL：`SemanticStratum/dao/mapper/SemanticOpenApiMapper.xml` 的 `<select id="getModelRelationships">`（约 495–562 行）
- **全中台仅此一个端点调用它**（爆炸半径 = 1），且引擎只传 `modelIds`、不传 `datasetIds`。

**语义**：求外键式模型关系——模型 `a` 的字段 `c`，其 `reference_table.reference_field`（或 `foreign_table.foreign_field`）
指向的表属于模型 `h`，于是产生一条 `a(source) → h(target)` 关系。UNION ALL 两支分别走 reference / foreign。

**为什么单 modelId 也稳定 30s**：SQL 结构是"先全量物化、最后才过滤"：
1. 内层 6 表逗号连接（隐式 CROSS JOIN），WHERE 里**无任何 modelId/database_wid 常量** → 算的是**全库**所有模型两两关系；
2. UNION ALL 把这套昂贵计算做**两遍**；
3. 外层再叠 3 层连接放大；
4. **modelId 过滤写在最外层派生列别名上**（`a."sourceModelId" in (...) or a."targetModelId" in (...)`），
   优化器**无法下推** → 无论传 1 个还是 N 个，都先算完整张全库图，再筛掉 99%。

对照同 Mapper 里秒回的兄弟接口：`getModelDetail` 是 `a.wid = #{modelId}`、`getMetrics/getDimensions` 是
`d.dataset_wid = #{dataset_wid}`——过滤都锚在 FROM/WHERE 的**单行种子**上。慢查询恰恰缺这个下推。

> 30s 是引擎客户端 `aiohttp ClientTimeout(total=30)`（`semantic_api_client.py:248`，无重试）触发；
> 客户端断开后 PG/VastBase 那条语句**很可能仍在后台继续跑**，反而加压、拖慢其它问数。

---

## 二、改法（语义等价，把外层过滤下推进内层两支）

**关键**：内层 `a.wid` 就是 `sourceModelId`、`h.wid` 就是 `targetModelId`，两个键都在内层手边。
把最外层那个过滤**原样下推**进两支 UNION ALL 的 WHERE 即可。

### Mapper before / after（最小改动）

在内层**两支** `union all` 的 WHERE 末尾各加一段（branch1 在原 `:505` 之后、branch2 在原 `:515` 之后）：

```xml
            <if test="modelIds != null and modelIds.size() > 0">
                and (
                    a.wid in (
                    <foreach collection="modelIds" item="modelId" separator=",">#{modelId}</foreach>
                    )
                    or
                    h.wid in (
                    <foreach collection="modelIds" item="modelId" separator=",">#{modelId}</foreach>
                    )
                )
            </if>
```

外层原有的 `<if test="modelIds...">`（`:547-561`）**保留即可**——内层已先收敛，外层变成冗余但零风险的二次过滤。
（也可删除外层那段；保留更稳，建议保留。）

### 为什么结果逐行一致（等价性论证）

一行最终能留下 ⟺ `sourceModelId∈M or targetModelId∈M` ⟺ `a.wid∈M or h.wid∈M`。
中间的 `join t_drm_model_dataitem d`（内连接，对"该模型子集"等量裁剪）与两个 `left join b/c`（左连接，从不丢行）
都不改变这个集合，因此把过滤从外层移到内层 **结果集逐行一致**，只是更早收敛。
（引擎侧本就会按 source/target+field 对关系去重：`semantic_api_client.py:1604-1612`，故集合等价即正确性判据。）

### 提速能否兑现 —— 必须真库 EXPLAIN 坐实

下推后内层有了 `a.wid in (...)`，优化器**能从主键 `t_drm_model_datamodel.wid` 种子展开**，而非先算全库图。
但有两点代码层无法确证，必须 EXPLAIN：
- **索引**：连接键 `database_wid / dataobject / datafield / catalog_wid / reference_*/foreign_* / wid` 上是否有索引
  （仓库内查不到这些表的建表/索引 DDL）；
- **跨两表 OR**：`a.wid in (...) or h.wid in (...)` 跨 `a`/`h` 两表，优化器**未必能对两侧都走索引**。

**若 EXPLAIN 显示该 OR 仍不走索引种子**，启用更稳的 **seed-split 兜底**：把内层拆成各自单边
（一支 `a.wid in (...)`、一支 `h.wid in (...)`，reference/foreign × source/target 共 4 支 union all），
每支各吃 `wid` 主键索引，重复行交由引擎去重收口。

---

## 三、验证脚本与上线闸门

脚本：[`sql/explain_getModelRelationships.sql`](sql/explain_getModelRelationships.sql)
（在**测试库或现场只读副本**上跑，把 `:mid` 设为现场真实 modelId，如 `45109629915832320`）。

**上线必须同时满足两闸：**
1. **等价闸**（脚本 STEP2）：`OLD_MINUS_NEW` 与 `NEW_MINUS_OLD` 两个差集**均为 0 行**；
2. **提速闸**（STEP3 计划 + STEP4 实测）：`v_new` 的计划从 `t_drm_model_datamodel.wid` 索引种子展开，
   实测耗时从 ≈30s 降到亚秒/秒级。

> ⚠ STEP4 的 `EXPLAIN(ANALYZE)` 会真正执行；对 `v_old` 跑一次≈30s 且加压，务必只在非生产副本上做。
> 若提速闸不过 → 先核查/补索引，或改用 seed-split，再复验。
> 索引建议（待按真实 schema 核对后由中台执行）：
> `t_drm_model_datamodel(wid)` 主键应已有；重点核 `t_drm_datafield(database_wid,dataobject)` 及
> `(reference_table,reference_field)/(foreign_table,foreign_field)`、`t_drm_model_dataitem(catalog_wid,database_wid,dataobject,datafield)`、
> `t_drm_dataobject(database_wid,dataobject)` 上的复合索引是否齐备。

---

## 四、引擎侧兜底（B，已实现，独立于 A）

`api/service/askdata_service/askdata_service.py`（约 421 行）：`asyncio.gather(...)` 改 `return_exceptions=True`，
**模型关系失败降级为 `[]` 并继续**（模型详情/业务术语仍是硬依赖，照常重抛）。
效果：中台该接口超时/抽风时，问数语义层**仍能走完**（不再让用户白等 30s 拿失败）。

> 关键约束：降级值必须是 `[]` 而非 `None`——下游 `table_config_generator.py:76` 对 `None` 会在 get-sql
> 阶段重新去打这个会超时的接口，等于把 30s 挪到下一步。已在代码注释中固化。
>
> 残留：B 让结果由"失败"变"成功降级"，但在 A 落地前用户**仍需等 ~30s**（gather 要等关系这一路超时）。
> 如需同时砍掉这段等待，可给关系调用单独设更短超时（`asyncio.wait_for` 包一层，失败即降级），按需追加。
>
> 另：`askdata_service.py:1304`/`825` 的宽表路径也直接调同一接口并 `raise`，同源超时风险仍在（本次范围外，已标记）。

---

## 五、同范式慢查询全量审计（`SemanticOpenApiMapper.xml`，35 个 select 全覆盖）

> getModelRelationships 不是孤例。整层查询有共同的"先全量物化、再晚过滤"范式。
> 风险分布：high=6 / medium=10 / low=2 / ok=17。**优先级 = 风险 × 引擎调用频度**——
> 一次问数里被按词/按模型**循环调几十次**的中等查询，累计杀伤比单次最慢的还大。

### 共性反范式（四类根因）
1. **逗号隐式 CROSS JOIN + 字符串键连接**：大量 `from a,b,c`，靠 `database_wid+dataobject+datafield` 等字符串列等值连接，缺单行种子。
2. **关键过滤是可选 `<if>`**：缺省路径退化为全量物化（权限图/目录树/关系图）。
3. **前导通配 ilike**：凡 `ilike concat('%',key,'%')` 都用不上索引、全表扫。
4. **无条件全表聚合派生表**：`dataset_child × dataset` 的 `group by/string_agg` 在 4 处被无过滤全表物化后才 join。

### 🔴 第一优先 · 按关键字检索四件套（high × 每问 `分词数×分页数` 次）
| select | 风险 | 引擎方法 / 频度 | 行号 | 推荐改法 |
|---|---|---|---|---|
| `getDimensionInfoByKeyword` | 🔴high | `get_dimension_info_by_keyword_async` · 每词×页 | 129-228 | 下推 dataset/catalog 到最内 datadim 基表 + `pg_trgm` GIN 索引让 ilike 走索引 |
| `getMetricInfoByKeyword` | 🔴high | `get_metric_info_by_keyword_async` · 每词×页 | 229-304 | 同上（指标版） |
| `searchDimensionByKeyword`（引擎 `getDimensionByDimensionValue`） | 🔴high | `get_dimension_by_dimension_value_async` · 每词×页 | 312-345 | **最易爆**（值表最大）：下推 datasetWids 到 datadim_value 种子 + trgm 索引 |
| `getBussinessTermInfo` | 🟠medium | `get_business_term_info_async` · 每词×页 | 346-379 | 下推 catalogWids 到 term 基表 + trgm |

### 🔴 第二优先 · 单条最重、已暴露超时
| select | 风险 | 引擎方法 / 频度 | 行号 | 推荐改法 |
|---|---|---|---|---|
| `getModelRelationships` | 🔴high | `get_model_relationships_async` · 每模型 | 495-562 | 见本文档第二节（下推 / seed-split），可考虑物化关系图缓存 |

### 🟠 第三优先 · 按模型/维度循环的 medium
| select | 风险 | 引擎方法 / 频度 | 行号 | 推荐改法 |
|---|---|---|---|---|
| `getDimensionInfoByModelId` + `getMetricInfoByModelId`（合为 `getModelIndsAndDimsByModelId`，**一次引擎调用触发两条**） | 🟠medium×2 | `get_model_inds_and_dims_by_model_id_async` · 每模型 | 563-609 / 610-645 | 最内 `datamodel_wid=#{id}` 已早过滤；坏在右侧 `string_agg` 派生表无过滤全表聚合 → 把 modelId/datasetWids 下推进该聚合子查询 |
| `getDimensionInfoById` | 🟠medium | `get_dimension_info_by_id_async` · 每维度（批5） | 416-466 | 同上：下推到聚合派生表 |
| `getDimensionValues` | 🟡low | `get_dimension_values_async` · 每维度×页 | 486-494 | 单维度已锚定，优先级低；值大时给 dimlias/dimcode 上 trgm |

### 🟠 每问一次的 medium（并发低、顺手治）
- `getHighUserSemanticPermissions` / `getLowUserSemanticPermissions`（698-797）：两路 union all × 5~6 表逗号 join，datasetWids/modelWids 可选、缺省即全量物化权限图。引擎 `get_user_semantic_permissions_async`，每问一次 + get-sql 权限分支一次。

### ⚪ 中台有、引擎**不调用**（治理优先级最后）
`getDomainList`、`getDatasetListById`、`getDatasetListByName`、`getModelList`、`getSemanticWords`——均不在引擎 `api_paths`。
> ⚠ **附带发现的真实 SQL bug**：`getModelList`（57-82）的 `<if>` 拼出了**两个 `where`**（第 60、62 行），
> `key` 非空时第二个 where 会冲掉 join 条件。虽不在问数链路，建议中台顺手修。

---

## 六、改法范式 ×「会不会改原意」对照（每条治理通用）

| 改法 | 适用 | 改原意? | 怎么做 / 注意 |
|---|---|---|---|
| **谓词下推**：把 dataset/model/catalog 的 `=`/`in` 条件从最外层派生别名，推进最内层【基表】WHERE，让 join/ilike 只在预筛小集合上跑 | 几乎所有 high/medium | **否** | 条件引用的列在那层必须已存在；**保留完整布尔结构**（别把 `OR` 砍成单边、别把 `LEFT JOIN` 的过滤从 `ON` 挪到 `WHERE` 变内连接） |
| **`pg_trgm` GIN 索引**让 `ilike '%词%'` 走索引 | 四件套 + 值检索的前导通配 | **否** | `CREATE EXTENSION pg_trgm; CREATE INDEX ... USING gin(col gin_trgm_ops)`。**匹配结果完全不变**，是治前导通配最干净的办法，不用动 `%词%` 语义 |
| **物化/缓存少变的图**（关系图、权限图） | getModelRelationships、权限 | **否** | 仅"失效策略"风险（看到旧数据），非查询逻辑风险；改元数据时失效重建 |
| ⚠ **改 ilike 匹配语义**（换前缀/分词/全文） | 仅当下推+trgm 都不够 | **会** | 非必要不做；做则必须确保复现同一批匹配 |
| ⚠ INNER↔LEFT、UNION↔UNION ALL/DISTINCT、聚合前后挪过滤 | 视具体查询 | **会/可能** | 这几类是"手滑改原意"高发区，逐条用等价闸把关 |

**治理纪律（铁律）**：每个要改的查询，改完都跑一遍 `EXCEPT` 双向等价校验
（参照 `sql/explain_getModelRelationships.sql` 的 STEP2，构造 `v_old`/`v_new` 比对差集必须均为 0 行）
+ `EXPLAIN(ANALYZE)` 提速闸，**两闸都过再上线，不靠肉眼拍板**。
主力三招（下推 / trgm / 缓存）天然不改原意；带 ⚠ 的改法这里基本用不到。

---

## 七、实测验证结论（2026-06-22，连了两个 PG 库，只读）⭐ 重要，修正第二节判断

在开发库（PG 13.7）和演示库（PG 13.3）上实测，**结论修正了"下推就是解药"的推断**：

| | 开发库 PG13.7 | 演示库 PG13.3 |
|---|---|---|
| 关键表规模 | 43 模型 / 22.7万 datafield | 32 模型 / 12.4万 datafield |
| 索引 `datafield_index`/`dataobject_index` | ✅ 都在 | ✅ 都在 |
| **v_old 真实耗时** | **4ms** | **10ms（返回1537行）** |
| **优化器是否自动下推 modelId** | ✅ 是（计划里 `Join Filter: a.wid=:mid OR h.wid=:mid` 在内层） | ✅ 是 |
| **等价校验（EXCEPT 双向）** | ✅ 0 / 0 | ✅ 0 / 0（1537=1537） |

**关键发现：**
1. **改法结果等价已坐实**（两库、含 1537 行非平凡用例，差集全 0）——"不改原意"从论证升级为实证。
2. **但在 PG13 上，PG 优化器自己就把 modelId 过滤下推到内层了**，所以手工下推（第二节）在健康的 PG 上**是 no-op**，两库都 4-10ms、根本不慢。
3. **两个可达环境都复现不出 30s**（都太小、都健康、都有索引、都自动下推）。

**真凶重新定位（现场库与这两库的差异，prod 已确认也是 PG）：**
- 🥇 **统计信息过期 → 优化器放弃下推、改走"先物化全库图再晚过滤"坏计划**。演示库已见部分表 `last_autoanalyze` 停在 2025-09；现场库数据 churn 大、autovacuum 跟不上时，统计一旧就可能翻坏计划 → 30s。
- 🥈 **现场库数据量远大于这两库**（真实客户，可能数百模型/上百万 datafield），规模本身顶过 30s 或更易选坏计划。

**因此推荐动作（现场库，由便宜到贵）：**
1. **先 `ANALYZE` 那 6 张表**刷新统计 —— 若真因是统计过期，零改代码即修复：
   ```sql
   ANALYZE t_drm_datafield, t_drm_dataobject, t_drm_model_datamodel,
           t_drm_model_dataitem, t_drm_model_dataset, t_drm_model_dataset_child;
   ```
2. 用 `sql/explain_getModelRelationships.sql`（已改为只读 CTE 版，含 STEP0 诊断）在现场库跑一遍，看 STEP1 是否仍是全库 Seq Scan/巨量 rows、未下推。
3. 仍慢 → 上本文第二节那版**已证等价的下推改法**作为"**计划稳定性保险**"（强制内层下推，不受统计/优化器选择影响），并核数据量/分区。

> 一句话：下推改法**正确且安全可备用**，但**不是 PG 上的必然解药**；现场 30s 更可能是**统计过期导致的坏计划**——**先 ANALYZE + 抓现场 EXPLAIN ANALYZE** 才能最终定因。引擎侧 B 兜底无论如何继续保留。

---

## 八、现场实锤：真因 = `t_drm_dataobject` 缺索引（2026-06-22 在出事库确认）⭐ 最终结论

连上**出事的现场库**（PG，`log_model_exists=t`，日志模型 45109629915832320 存在）跑区块3 `EXPLAIN ANALYZE`，**Execution Time = 56,107 ms**，复现。计划证据：

- 最烧时间的节点：`Nested Loop`（reference 支 28.1s + foreign 支 28.0s = 56s），
  `Rows Removed by Join Filter: 84,407,947`（8440 万行），内层
  `Seq Scan on t_drm_dataobject f (rows=4189, loops=13394)`、`Buffers: shared hit=4,406,626`。
- 即：`t_drm_dataobject` 被全表扫 **13394 次**，制造 8440 万行中间结果再过滤。

**根因**：现场库 `t_drm_dataobject` **没有任何索引**（健康库 dev/demo 有 `(database_wid, dataobject)`，故 4~10ms）。
查询 join 它两次（`f`/`g`），无索引 → 重复全表扫。旁证：现场数据更小（5.8万 datafield）、统计今早刚 autoanalyze → 排除数据量/统计。
计划里 `a.wid=… OR h.wid=…`（区块3 第23行）在 8440 万行 join filter 阶段才生效、**没当种子**（跨两表 OR 推不动）→ **下推改 SQL 救不了，真解药是补索引**。

**索引对照：**

| 表 | 健康库(秒回) | 出事现场库(56s) |
|---|---|---|
| `t_drm_datafield` | `(database_wid,dataobject,datafield)` | `(database_wid,dataobject)` + `(wid)` |
| `t_drm_dataobject` | `(database_wid,dataobject)` ✅ | **无索引** ❌ |

**修复（零改代码、零改原意、纯提速）：**
```sql
CREATE INDEX CONCURRENTLY index_drm_do ON t_drm_dataobject (database_wid, dataobject);
-- 可选对齐：CREATE INDEX CONCURRENTLY index_drm_dd_full ON t_drm_datafield (database_wid, dataobject, datafield);
```
加完重跑区块3，预期 56000ms → 几十 ms，`Seq Scan on t_drm_dataobject` 变 `Index Scan`。

**最终结论：**
- ✅ 真因 = 缺索引；修复 = 补 `t_drm_dataobject(database_wid,dataobject)` 索引。
- ❌ 中台 SQL **无需改**（下推在 PG 上是 no-op，且救不了缺索引这个问题）；第二~七节的下推改法仅作"计划稳定性保险"备用，本次不必上。
- ✅ 引擎侧 B（关系失败降级）继续保留当兜底。

---

## 九、第二案：`getModelIndsAndDimsByModelId` 演示库偶发超时（2026-06-22 二查）⭐ 与第一案同失败族、不同真因

> 缘起：演示库（已有 `t_drm_dataobject` 索引）仍偶发"语义层超时"，且前端报的是
> **"异步请求超时（30s）POST .../getModelIndsAndDimsByModelId"**（不是关系接口）。

### 9.1 症状澄清：是"引擎→中台"，不是浏览器直连中台
三仓核查（前端 `datav/drm/front` 全仓源码 + 编译产物 `dist`）：对 `getModelIndsAndDimsByModelId` **零引用**，
前端自己的语义接口走的是另一前缀 `/api/drm/**semanticStratum**/...`。那句"异步请求超时（30s）"由**引擎** aiohttp
抛出（`semantic_api_client.py:266` `f"异步请求超时({timeout}s): {method} {url}"`，文本里嵌的就是它正在打的中台 URL），
冒泡回问数对话框显示。
∴ 与第一案 `getModelRelationships` **同一失败族**（同一 `semantic_api_client`、同一 `ClientTimeout(total=30)`、同样 引擎→中台 一跳），只是换了端点。

### 9.2 该端点的两条 SQL（结构）
`getModelIndsAndDimsByModelId`（`SemanticOpenApiServiceImpl.java:409`）= 两条 DAO 串行：
`getDimensionInfoByModelId`（Mapper 563-609）+ `getMetricInfoByModelId`（610-645）；引擎对**每个模型并发**调一次。
- 两条都**锚 `WHERE a.datamodel_wid = #{modelId}`**（不像 relationships 无种子），结构本不该慢；
- 但各带一个**无 modelId 过滤的派生表 `c`**（`t_drm_model_dataset_child × t_drm_model_dataset` 的 `string_agg + group by`，每次调用全量物化）+ 多列字符串键 join；
- 🔑 **两条都不 join `t_drm_dataobject`** → 第一案的 `index_drm_do` 对它们**完全无效**，这正好解释"演示库有那索引、却仍在这个接口超时"。

### 9.3 实测：演示库这条 SQL **就是快**（亲连演示库 PG13.3，只读 `default_transaction_read_only=on`+`statement_timeout`）

| 项 | 实测 |
|---|---|
| `getDimensionInfoByModelId`（最重模型，18 维） | **9.6ms** |
| `getMetricInfoByModelId` | **2.8ms** |
| 计划 | 健康：`Index Scan using datafield_index`；派生表 c = `HashAggregate over 3671 行 ≈5ms`（**恒定、与模型无关**）；**无任何能到 30s 的节点** |
| 元数据表规模 | datadim=134 / dataind=11 / dataitem=293 / datamodel=32 / dataset=10 / dataset_child=3561；仅 `datafield`=124509（大，但有 `(database_wid,dataobject,datafield)` 复合索引） |
| PG 连接 | `max_connections`=**1000**，当前仅 **139**（133 idle / 1 active / **0 idle-in-txn**），无长查询、无锁 |

> 注：`datadim`/`dataind` 虽**无索引**，但只有 134/11 行，全表扫也是微秒级；统计虽停在 2025-09 也不影响这么小的表。
> 诊断脚本：[`sql/explain_getModelIndsAndDims.sql`](sql/explain_getModelIndsAndDims.sql)（STEP0 索引/统计审计 + STEP1 自动挑最重模型 + STEP2/3 只读 EXPLAIN ANALYZE）。

∴ **演示库这个 30s 超时，与这条 SQL 无关、与 PG 也无关。** 一个 3~10ms 的查询却"有时候"卡满 30s。

### 9.4 真因定位：中台**共享池争用**（非 SQL、非 PG）
- 🥇 **中台 HikariCP 连接池 / Tomcat 线程被真正的重端点占满**（`getModelRelationships`、按关键字四件套的前导 `%ilike`），
  这个快端点排队拿不到连接/线程 >30s → 引擎 30s 超时。
  ⚠ 关键：**PG 有 990 空闲槽 ≠ 中台连接池有空闲**——HikariCP 池上限通常才 10~20，PG 侧 idle 连接正是被它握着。
- 🥈 **引擎自伤式并发风暴**：每问对**每个模型并发**打 dims/metrics + 关系 + 关键字四件套（×词×页），
  且 client **每次调用都新建 `aiohttp.ClientSession`**（`semantic_api_client.py:241`，无 keepalive、无并发上限），几个问题并发就能把中台池压垮。

> 诚实边界：实测在**空闲时刻**，看不到高峰那一瞬；但"孤立测快 + PG 海量余量"与"高峰偶发 30s"**完全自洽**——正是争用型故障的典型画像。

**两案就此打通**：第一案给 relationships 加索引（让重端点跑得快、快放连接），**同时间接缓解第二案这种"快端点被饿死"**——它们共用中台同一个池。

### 9.5 修复：两道引擎防线（均已实现、未提交）

**防线一 · 引擎 B 扩到 dims/metrics（软降级）**
第四节的 B 当初只覆盖 `model_relations`。dims/metrics 是**第二个隐藏的硬依赖**，藏在三处**裸 gather** 后面：
- `askdata_service.py:538`（Phase1 预拉，**原会重抛 → 直接杀掉整个问题**，最高优先）
- `model_dataset_resolver.py:117`（`_ensure_dims_and_metrics`，规范注释在此）
- `table_config_generator.py:125`（关联模型，原来一个慢模型连累整批）

三处 gather 全部加 `return_exceptions=True` + 逐元素降级。
⚠ **降级值必须是 `{"dimensions": [], "metrics": []}`（dict），绝不能是 `None`**：
`table_config_generator._build_semantic_fields_info` / `_build_semantic_fields_info_for_aggr`（约 420/442/457/488/510/528 行）
直接 `model["dimsAndMetrics"]["dimensions"]` **裸取、无 `_resolve_table` 守卫**，`None` 会 `TypeError`。
（与第一案"必须 `[]` 不能 `None`"**同形、成因不同**：那次 `None` 触发下游重打超时接口；这次 `None` 触发裸取崩溃。）
顺手把 `_resolve_table` docstring（~632）从"空返回赋成 None"更正为"统一降为空 dict、不再出现 None"。每模型用**新字面量**，防 `dim['possibleValues']` 写入串改共享对象。

**防线二 · 出站并发限流（#2，本次新增，治本式削峰）**
在**唯一出站收口** `semantic_api_client._make_async_request` 加一道**进程级全局** `asyncio.Semaphore`，
把"引擎→中台"总并发钳在 `SEMANTIC_API_MAX_CONCURRENCY`（默认 **8**，同名环境变量可调）：
- **常规问题不受影响**（命中少数模型、在途数 ≤ 上限时仍全并行）；
- **扇出风暴被削平**为"至多 N 个在途、其余排队"，不再自伤式压垮中台池；
- **覆盖全部语义层调用**（dims/metrics、关系、四件套…都走这一收口，无需逐处改）；
- **自带现场信号**：某次请求在限流处排队 `>0.5s` 即打 `WARNING`（落每问日志），一眼区分"被自己并发挤队" vs "中台真慢"；
- **事件循环安全**：按运行中 loop 懒加载（多 loop 测试不报 "different loop"）。
- 已单测验证：50 并发 → 峰值 = 配置上限；env 覆盖生效；跨两个 `asyncio.run` loop 不崩。

> B 与限流是绝配：**限流**从源头减少把中台挤爆的概率，**B 的软降级**兜住万一仍被挤掉的那次调用——不再杀掉整个问题。
> 注意：限流不消除"中台真慢"时的单次 30s 等待（那要靠中台侧扩池/把重端点优化快）；它消除的是"引擎自己制造的并发把池打爆"。

### 9.6 "原 `getModelRelationships` 改造还有无意义" —— 定论
| 当初产物 | 对第二案的意义 |
|---|---|
| (A) relationships SQL 下推改写 | ❌ 与本案无关、健康 PG 上 no-op；仅"计划稳定性保险"备用，优先级最低 |
| (B) `t_drm_dataobject` 索引 | ⚠ 第一案真药要留；对 dims/metrics 是**红鲱鱼**（根本不碰该表） |
| (C) 引擎 B 软降级 | ✅✅ 只是没覆盖 dims/metrics → **已扩过去，价值反而更大** |
| (D) 索引拉齐清单 | ✅ 方法论正确；已加 `datadim/dataind(datamodel_wid)` 段（属**跨环境卫生 / 超大数据量防御，非本案解药**——演示无索引也才 9.6ms） |
| (E) 每问日志 + EXPLAIN 实测纪律 | ✅ 正是诊断本案的现成工具 |

**一句话**：那条 `getModelRelationships` 的**具体 SQL 改写**基本没意义了；但它催生的**整套打法**（索引拉齐清单、引擎软降级模式、每问日志、EXPLAIN 实测纪律）依旧是诊断/加固本案的主力。

### 9.7 待确认 / 后续
- **需中台侧**（坐实 🥇）：HikariCP `maximum-pool-size` + 高峰 `active/pending`、Tomcat `max-threads`；抓一次**真超时那条问题的每问日志**，看命中几个模型（=并发数）、是否出现 9.5 那条"出站并发已达上限"的排队 WARNING。
- **可选续作**（拿到上面数据后再定）：#3 把"每调用新建 session"改成**共享 session + `TCPConnector(limit=K)`**（连接复用 + 天然限并发，一举两得）；#4 每调用加更短 `asyncio.wait_for`，卡住即快速失败放槽。
