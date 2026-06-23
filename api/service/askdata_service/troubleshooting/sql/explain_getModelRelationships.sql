-- =====================================================================
-- getModelRelationships 现场诊断 + 下推改法等价/提速验证（纯 SQL）
-- 可在 DBeaver / Navicat / DataGrip / psql 等任意工具直接执行，无需 Python/pip。
--
-- 用法：连上现场库后，把下面【按区块】依次选中执行。
-- 安全：全程只读；重查询用 BEGIN READ ONLY + SET LOCAL statement_timeout 自我兜底，
--       任何写/DDL 会被拒、查询超 60s 会自动中断，不会卡死现场库。
--
-- 关于 modelId：区块3-5 默认用现场日志里出事那次的 45109629915832320。
--   想再测“最忙的模型”：把区块3-5 中的 45109629915832320 整体【查找替换】成
--   区块2 查出的第一行 wid，再把区块3-5 跑一遍即可（一次替换、零其他改动）。
-- =====================================================================


-- ========== 区块1：数据量 / 索引 / 统计新鲜度（轻量，先跑）==========
-- 1a 行数（看现场是否远大于演示库的 ~30 模型 / 十几万 datafield）
SELECT 't_drm_model_datamodel' AS tbl, count(*) AS rows FROM t_drm_model_datamodel
UNION ALL SELECT 't_drm_datafield',           count(*) FROM t_drm_datafield
UNION ALL SELECT 't_drm_model_dataitem',      count(*) FROM t_drm_model_dataitem
UNION ALL SELECT 't_drm_dataobject',          count(*) FROM t_drm_dataobject
UNION ALL SELECT 't_drm_model_dataset_child', count(*) FROM t_drm_model_dataset_child
UNION ALL SELECT 't_drm_model_dataset',       count(*) FROM t_drm_model_dataset;

-- 1b 索引（关键看 t_drm_datafield 有没有 (database_wid,dataobject,datafield) 复合索引）
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE tablename IN ('t_drm_model_datamodel','t_drm_datafield','t_drm_model_dataitem','t_drm_dataobject','t_drm_model_dataset_child','t_drm_model_dataset')
ORDER BY tablename, indexname;

-- 1c 统计信息新鲜度（last_analyze/last_autoanalyze 太旧 → 优化器易选坏计划）
SELECT relname, n_live_tup, last_analyze, last_autoanalyze
FROM pg_stat_user_tables
WHERE relname IN ('t_drm_model_datamodel','t_drm_datafield','t_drm_model_dataitem','t_drm_dataobject','t_drm_model_dataset_child','t_drm_model_dataset')
ORDER BY relname;


-- ========== 区块2：日志模型是否存在 + 候选“最忙模型” ==========
SELECT exists(select 1 from t_drm_model_datamodel where wid = 45109629915832320) AS log_model_exists;

SELECT a.wid AS busiest_model_wid, count(*) AS ref_fields
FROM t_drm_model_datamodel a
JOIN t_drm_datafield c ON a.database_wid = c.database_wid AND a.dataobject = c.dataobject
WHERE (c.reference_table IS NOT NULL AND c.reference_field IS NOT NULL)
   OR (c.foreign_table  IS NOT NULL AND c.foreign_field  IS NOT NULL)
GROUP BY a.wid ORDER BY ref_fields DESC LIMIT 5;


-- ========== 区块3：v_old 真实计划 + 耗时（会执行；只读 + 60s 超时自兜底）==========
-- 关注：Execution Time 多少？是否出现 "Seq Scan on t_drm_datafield"？
--       计划里 a.wid=... OR h.wid=... 这个过滤是在最内层（已下推）还是最外层（未下推）？
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';
EXPLAIN (ANALYZE, BUFFERS)
select a.*, b.dataset_wid source_dataset_wid, c.dataset_wid target_dataset_wid
from (
    select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
           h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
           'LEFT' as "joinType", a.catalog_wid
    from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
    where a.database_wid = c.database_wid and a.dataobject = c.dataobject
      and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.reference_table = e.dataobject and c.reference_field = e.datafield
      and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
      and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
    union all
    select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
           h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
           'LEFT' as "joinType", a.catalog_wid
    from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
    where a.database_wid = c.database_wid and a.dataobject = c.dataobject
      and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.foreign_table = e.dataobject and c.foreign_field = e.datafield
      and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
      and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
) a
join t_drm_model_dataitem d on d.catalog_wid = a.catalog_wid and d.database_wid = a.database_wid and d.dataobject = a.source_dataobject and d.datafield = a."sourceField"
left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) b
    on a.catalog_wid = b.catalog_wid and a.database_wid = b.database_wid and a.source_dataobject = b.dataobject and a."sourceField" = b.datafield
left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) c
    on a.catalog_wid = c.catalog_wid and a.database_wid = c.database_wid and a.target_dataobject = c.dataobject and a."targetField" = c.datafield
where 1=1 and ( a."sourceModelId" in (45109629915832320) or a."targetModelId" in (45109629915832320) );
COMMIT;


-- ========== 区块4：v_new 计划（下推改法，不执行，零负载）==========
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';
EXPLAIN
select a.*, b.dataset_wid source_dataset_wid, c.dataset_wid target_dataset_wid
from (
    select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
           h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
           'LEFT' as "joinType", a.catalog_wid
    from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
    where a.database_wid = c.database_wid and a.dataobject = c.dataobject
      and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.reference_table = e.dataobject and c.reference_field = e.datafield
      and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
      and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
      and ( a.wid in (45109629915832320) or h.wid in (45109629915832320) )
    union all
    select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
           h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
           'LEFT' as "joinType", a.catalog_wid
    from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
    where a.database_wid = c.database_wid and a.dataobject = c.dataobject
      and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.foreign_table = e.dataobject and c.foreign_field = e.datafield
      and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
      and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
      and ( a.wid in (45109629915832320) or h.wid in (45109629915832320) )
) a
join t_drm_model_dataitem d on d.catalog_wid = a.catalog_wid and d.database_wid = a.database_wid and d.dataobject = a.source_dataobject and d.datafield = a."sourceField"
left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) b
    on a.catalog_wid = b.catalog_wid and a.database_wid = b.database_wid and a.source_dataobject = b.dataobject and a."sourceField" = b.datafield
left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) c
    on a.catalog_wid = c.catalog_wid and a.database_wid = c.database_wid and a.target_dataobject = c.dataobject and a."targetField" = c.datafield
where 1=1 and ( a."sourceModelId" in (45109629915832320) or a."targetModelId" in (45109629915832320) );
COMMIT;


-- ========== 区块5（可选，较重）：等价校验，两差集必须都为 0 ==========
-- 注：会完整执行 v_old，现场若 v_old 很慢这步也会慢/超时；正确性已在开发/演示库验证一致，可跳过。
BEGIN READ ONLY;
SET LOCAL statement_timeout = '120s';
with vold as (
  select a.*, b.dataset_wid source_dataset_wid, c.dataset_wid target_dataset_wid
  from (
      select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
             h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
             'LEFT' as "joinType", a.catalog_wid
      from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
      where a.database_wid = c.database_wid and a.dataobject = c.dataobject
        and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.reference_table = e.dataobject and c.reference_field = e.datafield
        and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
        and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
      union all
      select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
             h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
             'LEFT' as "joinType", a.catalog_wid
      from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
      where a.database_wid = c.database_wid and a.dataobject = c.dataobject
        and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.foreign_table = e.dataobject and c.foreign_field = e.datafield
        and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
        and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
  ) a
  join t_drm_model_dataitem d on d.catalog_wid = a.catalog_wid and d.database_wid = a.database_wid and d.dataobject = a.source_dataobject and d.datafield = a."sourceField"
  left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) b
      on a.catalog_wid = b.catalog_wid and a.database_wid = b.database_wid and a.source_dataobject = b.dataobject and a."sourceField" = b.datafield
  left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) c
      on a.catalog_wid = c.catalog_wid and a.database_wid = c.database_wid and a.target_dataobject = c.dataobject and a."targetField" = c.datafield
  where 1=1 and ( a."sourceModelId" in (45109629915832320) or a."targetModelId" in (45109629915832320) )
),
vnew as (
  select a.*, b.dataset_wid source_dataset_wid, c.dataset_wid target_dataset_wid
  from (
      select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
             h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
             'LEFT' as "joinType", a.catalog_wid
      from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
      where a.database_wid = c.database_wid and a.dataobject = c.dataobject
        and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.reference_table = e.dataobject and c.reference_field = e.datafield
        and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
        and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
        and ( a.wid in (45109629915832320) or h.wid in (45109629915832320) )
      union all
      select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
             h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
             'LEFT' as "joinType", a.catalog_wid
      from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
      where a.database_wid = c.database_wid and a.dataobject = c.dataobject
        and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.foreign_table = e.dataobject and c.foreign_field = e.datafield
        and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
        and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject
        and ( a.wid in (45109629915832320) or h.wid in (45109629915832320) )
  ) a
  join t_drm_model_dataitem d on d.catalog_wid = a.catalog_wid and d.database_wid = a.database_wid and d.dataobject = a.source_dataobject and d.datafield = a."sourceField"
  left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) b
      on a.catalog_wid = b.catalog_wid and a.database_wid = b.database_wid and a.source_dataobject = b.dataobject and a."sourceField" = b.datafield
  left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) c
      on a.catalog_wid = c.catalog_wid and a.database_wid = c.database_wid and a.target_dataobject = c.dataobject and a."targetField" = c.datafield
  where 1=1 and ( a."sourceModelId" in (45109629915832320) or a."targetModelId" in (45109629915832320) )
)
select 'old_minus_new(应为0)' AS tag, count(*) AS c FROM (select * from vold except select * from vnew) z
union all select 'new_minus_old(应为0)', count(*) FROM (select * from vnew except select * from vold) z
union all select 'old_rows', count(*) FROM vold
union all select 'new_rows', count(*) FROM vnew;
COMMIT;


-- ========== 现场若慢、且统计偏旧：请 DBA 手动执行（非破坏，仅刷新统计；本脚本不含它）==========
-- ANALYZE t_drm_datafield, t_drm_dataobject, t_drm_model_datamodel,
--         t_drm_model_dataitem, t_drm_model_dataset, t_drm_model_dataset_child;
-- 跑完 ANALYZE 后，重跑区块3 看 Execution Time 是否大幅下降。
