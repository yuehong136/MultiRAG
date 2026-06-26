-- =====================================================================
-- 语义层「必建索引」清单（中台 PostgreSQL，各环境拉齐用）
--
-- 背景：智能问数语义层接口 getModelRelationships 等会 join t_drm_dataobject / t_drm_datafield。
--   这些索引各环境是【人工手建】、未纳入迁移脚本，导致飘移：某环境漏建
--   t_drm_dataobject(database_wid,dataobject) 索引时，getModelRelationships 会对该表全表扫
--   上万次 → 单次问数卡 30~56s（已在现场库实测复现）。
--   健康环境(秒回)都具备下面的索引；本清单把缺失的补齐。
--
-- 安全：幂等。STEP2 用 DO 块【按“是否已有覆盖该列的索引”判断】，
--   只在缺失时才建，不会因为各环境索引命名不同而重复建。先跑 STEP1 审计，再跑 STEP2。
-- 兼容：DBeaver / Navicat / DataGrip / psql 均可直接执行。
-- =====================================================================

-- ========== STEP1 审计：看本环境这几张表现有哪些索引 ==========
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('t_drm_dataobject','t_drm_datafield','t_drm_model_dataitem',
                    't_drm_model_datamodel','t_drm_model_dataset_child','t_drm_model_dataset',
                    't_drm_model_datadim','t_drm_model_dataind')
ORDER BY tablename, indexname;


-- ========== STEP2 必建：t_drm_dataobject(database_wid, dataobject) ==========
-- 这是关键缺口：getModelRelationships 两次 join 该表都按 (database_wid, dataobject)，
-- 缺它就会全表扫上万次 → 30~56s。仅当本环境没有“以 (database_wid, dataobject) 打头的索引”时才建。
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 't_drm_dataobject'
      AND indexdef ~* '\(\s*database_wid\s*,\s*dataobject'
  ) THEN
    EXECUTE 'CREATE INDEX index_drm_do ON t_drm_dataobject (database_wid, dataobject)';
    RAISE NOTICE '✅ 已创建 t_drm_dataobject(database_wid, dataobject) 索引';
  ELSE
    RAISE NOTICE '✓ t_drm_dataobject 已有 (database_wid, dataobject) 覆盖索引，跳过';
  END IF;
END $$;


-- ========== STEP3（建议）：t_drm_datafield 至少要有 (database_wid, dataobject) 打头的索引 ==========
-- 多数环境已有（2 列或 3 列均可）；仅当完全没有时才补一个 3 列版（与健康库对齐、覆盖更全）。
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 't_drm_datafield'
      AND indexdef ~* '\(\s*database_wid\s*,\s*dataobject'
  ) THEN
    EXECUTE 'CREATE INDEX index_drm_dd_full ON t_drm_datafield (database_wid, dataobject, datafield)';
    RAISE NOTICE '✅ 已创建 t_drm_datafield(database_wid, dataobject, datafield) 索引';
  ELSE
    RAISE NOTICE '✓ t_drm_datafield 已有 (database_wid, dataobject...) 覆盖索引，跳过';
  END IF;
END $$;


-- ========== STEP3b 必建：getModelIndsAndDims 的“种子”索引（datamodel_wid） ==========
-- 背景：getModelIndsAndDimsByModelId 内的两条 select（getDimensionInfoByModelId /
--   getMetricInfoByModelId）都靠 `WHERE a.datamodel_wid = #{modelId}` 锚定单模型。
--   这两张表【不 join t_drm_dataobject】，所以 STEP2 的 index_drm_do 帮不上它们——
--   它们要的是各自 (datamodel_wid) 上的种子索引。缺了就全表扫该表去捞单模型的维度/指标。
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 't_drm_model_datadim'
      AND indexdef ~* '\(\s*datamodel_wid'
  ) THEN
    EXECUTE 'CREATE INDEX index_drm_datadim_model ON t_drm_model_datadim (datamodel_wid)';
    RAISE NOTICE '✅ 已创建 t_drm_model_datadim(datamodel_wid) 索引';
  ELSE
    RAISE NOTICE '✓ t_drm_model_datadim 已有 (datamodel_wid...) 覆盖索引，跳过';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 't_drm_model_dataind'
      AND indexdef ~* '\(\s*datamodel_wid'
  ) THEN
    EXECUTE 'CREATE INDEX index_drm_dataind_model ON t_drm_model_dataind (datamodel_wid)';
    RAISE NOTICE '✅ 已创建 t_drm_model_dataind(datamodel_wid) 索引';
  ELSE
    RAISE NOTICE '✓ t_drm_model_dataind 已有 (datamodel_wid...) 覆盖索引，跳过';
  END IF;
END $$;


-- ========== STEP4 复核：再跑一次 STEP1，确认上述表都已有对应索引 ==========
-- （或直接重跑 getModelRelationships / getModelIndsAndDims 的 EXPLAIN ANALYZE，确认耗时降到毫秒级）
-- 提示：仅靠加索引仍未达毫秒级时，先对这几张元数据表 ANALYZE 刷新统计（零改原意），再复测；
--   getModelIndsAndDims 的两条 select 另有“派生表 c 全量物化”结构性开销，详见
--   explain_getModelIndsAndDims.sql 的读图速查。

-- 备注：
-- · 上面的 DO 块用普通 CREATE INDEX（建索引时对该表写操作有短暂阻塞）。
--   这几张元数据表都不大（数千~十几万行），通常亚秒~数秒完成。
-- · 若某环境表特别大或非常繁忙、想完全不锁写：改用在【事务外】单独执行
--     CREATE INDEX CONCURRENTLY index_drm_do ON t_drm_dataobject (database_wid, dataobject);
--   （CONCURRENTLY 不能放在 DO 块/事务里）。
-- · 因索引为人工维护，建议把本清单纳入“新环境装库标准动作”，避免再次漏建导致问数超时。
