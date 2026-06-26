-- =====================================================================
-- 诊断：getModelIndsAndDimsByModelId 30s 超时（演示/任意环境，PostgreSQL）
--
-- 背景：智能问数链路里，引擎(multrag)会对【每个模型】并发调用中台
--   getModelIndsAndDimsByModelId，该接口内部跑两条 select：
--     · getDimensionInfoByModelId（维度）
--     · getMetricInfoByModelId  （指标）
--   前端看到的“异步请求超时（30s）POST .../getModelIndsAndDimsByModelId”，
--   实际是【引擎 aiohttp ClientTimeout(total=30)】打中台超时后抛出的报错
--   （semantic_api_client.py:266），即 引擎→中台 这一跳慢，不是浏览器直连中台。
--
--   ⚠ 与 getModelRelationships 那次【不同】：这两条 SQL 都【不 join t_drm_dataobject】，
--      所以上次救场的 index_drm_do(t_drm_dataobject) 在这里【完全用不上】——这正好解释
--      “演示库有那个索引、却仍在这个接口超时”。本脚本用来在演示库实测定位真因。
--
-- 安全：全程只读。EXPLAIN ANALYZE 会真正执行 select（但只读不写），
--   已用 BEGIN READ ONLY + SET LOCAL statement_timeout='60s' 双保险，
--   慢就 60s 自动掐断，绝不长占连接、绝不写库。
-- 用法：DBeaver / Navicat / DataGrip / psql 均可。先跑 STEP0、STEP1，
--   把 STEP1 查出的 modelId 填进 STEP2/STEP3 的占位处，再分别执行。
-- =====================================================================


-- ========== STEP0 审计：这 7 张表现有哪些索引 + 统计是否新鲜 ==========
-- 重点看：t_drm_model_datadim / t_drm_model_dataind 有没有 (datamodel_wid) 打头的“种子”索引，
--        t_drm_datafield 有没有 (database_wid, dataobject[, datafield]) 索引。
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE tablename IN (
  't_drm_model_datadim','t_drm_model_dataind','t_drm_datafield',
  't_drm_model_datamodel','t_drm_model_dataitem',
  't_drm_model_dataset_child','t_drm_model_dataset','t_drm_model_catalog')
ORDER BY tablename, indexname;

SELECT relname,
       n_live_tup                         AS approx_rows,
       last_analyze, last_autoanalyze       -- 停留在很久以前 = 统计过期，优化器可能选坏计划
FROM pg_stat_user_tables
WHERE relname IN (
  't_drm_model_datadim','t_drm_model_dataind','t_drm_datafield',
  't_drm_model_datamodel','t_drm_model_dataitem',
  't_drm_model_dataset_child','t_drm_model_dataset','t_drm_model_catalog')
ORDER BY relname;


-- ========== STEP1 选“最重的模型”做最坏情况复现：维度/指标最多的模型 ==========
-- 这两条把模型按维度数、指标数排序，各取前 5。挑一个 dim_cnt（或 ind_cnt）最大的
-- modelId，记下来填到 STEP2/STEP3。（也可直接用现场日志里那次超时的 modelId。）
SELECT datamodel_wid AS model_id, count(*) AS dim_cnt
FROM t_drm_model_datadim
GROUP BY datamodel_wid ORDER BY dim_cnt DESC LIMIT 5;

SELECT datamodel_wid AS model_id, count(*) AS ind_cnt
FROM t_drm_model_dataind
GROUP BY datamodel_wid ORDER BY ind_cnt DESC LIMIT 5;


-- ========== STEP2 复现 + 定因：getDimensionInfoByModelId 的 EXPLAIN ANALYZE ==========
-- 把下面 11 处 45109629915832320 改成 STEP1 选出的 model_id（占位是上次现场那个 id）。
-- 关注计划里：t_drm_model_datadim 是 Index Scan 还是 Seq Scan；
--   派生表 c（t_drm_model_dataset_child × t_drm_model_dataset 的 string_agg/group by）是否被全表物化；
--   有没有 loops=上万 / Rows Removed by Join Filter 巨大 的节点。
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';

EXPLAIN (ANALYZE, BUFFERS)
select a.*,g.semanticsformat,c.dataset_wid from (
        select a.*,d.catalogname  "domainName",d.wid "domainId"
        from (
                 select a.wid                                 "dimensionId",
                        a.dimname                             "dimensionName",
                        a.dimname_en                          "dimensionEnName",
                        string_to_array(a.dimname_alias, ',') "synonyms",
                        c.fieldtype                           "dataType",
                        a.dimnamedesc                         "description",
                        a.dimtype,
                        a.is_label,
                        b.catalog_wid,
                        c.fieldtype                                              "dataType",
                        null                                                     "datasets",
                        b.wid                                                    "modelId",
                        a.status,
                        case
                            when c.reference_table is not null and c.reference_field is not null
                                then c.reference_table || '.' || c.reference_field
                            else null end                                        "physicalColumn",
                        case when dimtype = 'enumerate' then true else false end "isEnum",
                        null                                                     "enumValues",
                        b.database_wid,b.dataobject,a.dimname_en
                 from t_drm_model_datadim a,
                      t_drm_model_datamodel b,
                      t_drm_datafield c
                 where a.datamodel_wid = 45109629915832320   -- <<< 替换成 STEP1 的 model_id
                   and a.datamodel_wid = b.wid
                   and b.database_wid = c.database_wid
                   and b.dataobject = c.dataobject
                   and a.dimname_en = c.datafield
             ) a
                 left join t_drm_model_catalog d
                           on a.catalog_wid = d.wid
    ) a
        left join t_drm_model_dataitem g
                  on  a."domainId" = g.catalog_wid and a.database_wid = g.database_wid and a.dataobject = g.dataobject and a."dimensionEnName" = g.datafield
        left join
    (
        select a.database_wid ,a.dataobject ,a.datafield ,b.catalog_wid ,string_agg(a.dataset_wid::varchar,',') dataset_wid
        from      t_drm_model_dataset_child a,t_drm_model_dataset b
        where a.dataset_wid = b.wid
        group by a.database_wid ,a.dataobject ,a.datafield ,b.catalog_wid
    ) c
    on  a.database_wid = c.database_wid  and a.dataobject = c.dataobject and a.dimname_en = c.datafield and a."domainId" = c.catalog_wid;

COMMIT;


-- ========== STEP3 复现 + 定因：getMetricInfoByModelId 的 EXPLAIN ANALYZE ==========
-- 同样把 45109629915832320 改成 STEP1 的 model_id（这条锚在 t_drm_model_dataind）。
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';

EXPLAIN (ANALYZE, BUFFERS)
select a.*,g.semanticsformat,c.dataset_wid from (
    select a.*,d.catalogname  "domainName",d.wid  "domainId"
    from (
             select a.wid "metricId",a.indname "metricName",a.indname_en "metricEnName",a.drillingdim "drillDownDimensions",
                    a.dataformat  "formatting" ,string_to_array(a.indname_alias, ',') "synonyms",a.ind_expression "expression",c.fieldtype  "dataType" ,
                    a.indnamedesc "description",b.catalog_wid,
                    c.fieldtype                                              "dataType",
                    b.wid                                                    "modelId",
                    a.status,
                    case
                        when c.reference_table is not null and c.reference_field is not null
                            then c.reference_table || '.' || c.reference_field
                        else null end                                        "physicalColumn",
                    false "isEnum",
                    null                                                     "enumValues",
                    b.database_wid,b.dataobject,a.indname_en
             from t_drm_model_dataind  a,t_drm_model_datamodel b ,t_drm_datafield c
             where a.datamodel_wid = 45109629915832320       -- <<< 替换成 STEP1 的 model_id
               and a.datamodel_wid = b.wid
               and b.database_wid = c.database_wid and b.dataobject = c.dataobject and a.indname_en = c.datafield
         ) a
             left join t_drm_model_catalog d
                       on a.catalog_wid = d.wid
    ) a
    left join t_drm_model_dataitem g
         on  a."domainId" = g.catalog_wid and a.database_wid = g.database_wid and a.dataobject = g.dataobject and a."metricEnName" = g.datafield
    left join
    (
        select a.database_wid ,a.dataobject ,a.datafield ,b.catalog_wid ,string_agg(a.dataset_wid::varchar,',') dataset_wid
        from      t_drm_model_dataset_child a,t_drm_model_dataset b
        where a.dataset_wid = b.wid
        group by a.database_wid ,a.dataobject ,a.datafield ,b.catalog_wid
    ) c
        on  a.database_wid = c.database_wid  and a.dataobject = c.dataobject and a.indname_en = c.datafield and a."domainId" = c.catalog_wid;

COMMIT;


-- ========== 读图速查 ==========
-- · t_drm_model_datadim / t_drm_model_dataind 出现 Seq Scan 且 actual rows 很大
--     → 缺 (datamodel_wid) 种子索引（见 semantic_required_indexes.sql 新增段）。
-- · 派生表 c 那支（含 string_agg / HashAggregate over t_drm_model_dataset_child）耗时占大头
--     → 该子查询无 modelId 过滤、每次全量物化；先 ANALYZE，再考虑给 dataset_child 补索引；
--        仍重则需中台把它改成相关子查询/按 (database_wid,dataobject) 收窄（改原意风险，需评估）。
-- · 任何节点 loops 上万 + Rows Removed by Join Filter 上千万 → 坏的 Nested Loop，
--     多半是统计过期，先在【业务低峰】对这 7 张表 ANALYZE（只刷统计、不锁表、零改原意）再复测。
-- · Execution Time 远小于 30s（如几十~几百 ms）→ 单条不慢，超时来自【并发风暴】：
--     引擎对一次问题里的每个模型并发打这个接口，模型多时 N 条重查询同时压库 → 互相拖慢。
--     用每问日志看那次超时问题命中了几个模型，定位是否并发放大。
