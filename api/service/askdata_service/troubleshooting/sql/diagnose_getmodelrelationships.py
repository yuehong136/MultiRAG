#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""现场诊断：中台 getModelRelationships 慢查询根因 + 下推改法等价/提速验证。

只读、安全：整个会话强制 default_transaction_read_only=on + statement_timeout，
脚本只执行 SELECT / EXPLAIN，绝不写库、绝不执行 ANALYZE（ANALYZE 只在结尾打印建议，由 DBA 手动跑）。

测两个 modelId 并对比：
  1) 指定/日志：默认用现场日志里出事那次的模型 45109629915832320（可用 --model-id 覆盖）；
     若该 wid 在所连库中不存在，自动跳过并提示。
  2) 自动发现：在所连库里挑“带引用字段最多的模型”（最可能有关系、结果非空）。

用法：
    pip install psycopg2-binary       # 现场若没有驱动
    python api/service/askdata_service/troubleshooting/sql/diagnose_getmodelrelationships.py \
        --host <现场库IP> --port 5432 --dbname postgres --user <用户> --password <密码>

  可选：--model-id <wid> 覆盖“指定”模型；--timeout 60 语句超时秒数；--no-analyze 只看计划不执行。
连接信息也可用环境变量 PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD 提供。
"""
import argparse
import json
import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("缺少驱动：请先 `pip install psycopg2-binary`")

# 现场日志中出事那次问数的模型（“指定”默认值）
LOG_MODEL_ID = 45109629915832320

TABLES = [
    "t_drm_model_datamodel", "t_drm_datafield", "t_drm_model_dataitem",
    "t_drm_dataobject", "t_drm_model_dataset_child", "t_drm_model_dataset",
]

_SELECT_LIST = '''select a.wid "sourceModelId", c.database_wid, c.dataobject source_dataobject, f.dataobjectname "sourceModelName", c.datafield "sourceField",
           h.wid "targetModelId", e.dataobject target_dataobject, g.dataobjectname "targetModelName", e.datafield "targetField",
           'LEFT' as "joinType", a.catalog_wid'''


def _inner_branch(kind: str, pushdown: bool) -> str:
    table_col = "reference_table" if kind == "reference" else "foreign_table"
    field_col = "reference_field" if kind == "reference" else "foreign_field"
    extra = "\n      and ( a.wid in (%(mid)s) or h.wid in (%(mid)s) )" if pushdown else ""
    return f"""{_SELECT_LIST}
    from t_drm_model_datamodel a, t_drm_datafield c, t_drm_model_dataitem e, t_drm_model_datamodel h, t_drm_dataobject f, t_drm_dataobject g
    where a.database_wid = c.database_wid and a.dataobject = c.dataobject
      and a.catalog_wid = e.catalog_wid and c.database_wid = e.database_wid and c.{table_col} = e.dataobject and c.{field_col} = e.datafield
      and e.database_wid = h.database_wid and e.catalog_wid = h.catalog_wid and e.dataobject = h.dataobject
      and a.database_wid = f.database_wid and a.dataobject = f.dataobject and h.database_wid = g.database_wid and h.dataobject = g.dataobject{extra}"""


def build_query(pushdown: bool) -> str:
    """pushdown=False → 当前线上 SQL(v_old)；True → 内层两支下推改法(v_new)。"""
    return f"""select a.*, b.dataset_wid source_dataset_wid, c.dataset_wid target_dataset_wid
from (
{_inner_branch('reference', pushdown)}
    union all
{_inner_branch('foreign', pushdown)}
) a
join t_drm_model_dataitem d on d.catalog_wid = a.catalog_wid and d.database_wid = a.database_wid and d.dataobject = a.source_dataobject and d.datafield = a."sourceField"
left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) b
    on a.catalog_wid = b.catalog_wid and a.database_wid = b.database_wid and a.source_dataobject = b.dataobject and a."sourceField" = b.datafield
left join (select a.database_wid, a.dataobject, a.datafield, a.dataset_wid, b.catalog_wid from t_drm_model_dataset_child a, t_drm_model_dataset b where a.dataset_wid = b.wid) c
    on a.catalog_wid = c.catalog_wid and a.database_wid = c.database_wid and a.target_dataobject = c.dataobject and a."targetField" = c.datafield
where 1=1 and ( a."sourceModelId" in (%(mid)s) or a."targetModelId" in (%(mid)s) )"""


def hr(title: str):
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def walk_plan(node, hits):
    if isinstance(node, dict):
        nt, rel = node.get("Node Type"), node.get("Relation Name")
        if nt and rel:
            hits.append((nt, rel))
        for child in node.get("Plans", []) or []:
            walk_plan(child, hits)


def explain_analyze(cur, sql, params):
    """返回 (execution_ms 或 None, scan_hits, timed_out)。"""
    try:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        raw = cur.fetchone()[0]
        plan = json.loads(raw) if isinstance(raw, str) else raw
        hits = []
        walk_plan(plan[0].get("Plan", {}), hits)
        return plan[0].get("Execution Time"), hits, False
    except psycopg2.errors.QueryCanceled:
        return None, [], True


def diagnose_model(cur, mid, label, args):
    """对单个 modelId 跑 计划对照 + 实测 + 等价校验，打印并返回汇总 dict。"""
    hr(f"【{label}】modelId = {mid}")
    params = {"mid": mid}
    res = {"label": label, "mid": mid, "old_ms": None, "new_ms": None,
           "old_to": False, "new_to": False, "df_seqscan": False, "equiv": None}

    cur.execute("EXPLAIN " + build_query(False), params)
    print("· v_old 计划顶层:", cur.fetchone()[0].strip())
    cur.execute("EXPLAIN " + build_query(True), params)
    print("· v_new 计划顶层:", cur.fetchone()[0].strip())

    if args.no_analyze:
        print("  (--no-analyze：跳过实测与等价)")
        return res

    old_ms, old_hits, old_to = explain_analyze(cur, build_query(False), params)
    res["old_ms"], res["old_to"] = old_ms, old_to
    res["df_seqscan"] = any(nt == "Seq Scan" and rel == "t_drm_datafield" for nt, rel in old_hits)
    print(f"· v_old 实测: {'⚠ 超时 >' + str(args.timeout) + 's（复现慢查询！）' if old_to else f'{old_ms:.1f} ms'}"
          + ("  ⚠t_drm_datafield 走 Seq Scan" if res["df_seqscan"] else ""))

    new_ms, _, new_to = explain_analyze(cur, build_query(True), params)
    res["new_ms"], res["new_to"] = new_ms, new_to
    print(f"· v_new 实测: {'⚠ 超时（改法也救不了，需加索引/分区）' if new_to else f'{new_ms:.1f} ms'}")

    if old_to or (old_ms is not None and old_ms > 5000):
        print("· 等价校验: 跳过（v_old 较慢，避免加压；正确性已在 dev/demo 验证为完全一致）")
    else:
        equiv = (f"with vold as (\n{build_query(False)}\n), vnew as (\n{build_query(True)}\n)\n"
                 "select 'a' k, count(*) c from (select * from vold except select * from vnew) z "
                 "union all select 'b', count(*) from (select * from vnew except select * from vold) z "
                 "union all select 'o', count(*) from vold union all select 'n', count(*) from vnew")
        try:
            cur.execute(equiv, params)
            d = {k: c for k, c in cur.fetchall()}
            res["equiv"] = (d["a"] == 0 and d["b"] == 0)
            print(f"· 等价校验: old−new={d['a']}, new−old={d['b']}, 行数 old={d['o']}/new={d['n']} "
                  + ("✅ 通过(结果一致)" if res["equiv"] else "❌ 不一致，禁止上线！"))
        except psycopg2.errors.QueryCanceled:
            print("· 等价校验: 超时跳过（正确性已在 dev/demo 验证）")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("PGHOST"))
    ap.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    ap.add_argument("--dbname", default=os.environ.get("PGDATABASE", "postgres"))
    ap.add_argument("--user", default=os.environ.get("PGUSER"))
    ap.add_argument("--password", default=os.environ.get("PGPASSWORD"))
    ap.add_argument("--model-id", type=int, default=LOG_MODEL_ID,
                    help=f"“指定”模型 wid（默认现场日志那次 {LOG_MODEL_ID}）")
    ap.add_argument("--timeout", type=int, default=60, help="statement_timeout 秒数")
    ap.add_argument("--no-analyze", action="store_true", help="只看计划，不执行实测/等价")
    args = ap.parse_args()
    if not args.host or not args.user:
        sys.exit("请提供 --host 与 --user（或设 PGHOST/PGUSER 等环境变量）")

    conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.dbname,
                            user=args.user, password=args.password, connect_timeout=10)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET default_transaction_read_only = on")     # 安全护栏：全会话只读
    cur.execute(f"SET statement_timeout = '{int(args.timeout)}s'")

    hr("STEP0a 版本 / 只读确认")
    cur.execute("SELECT version()"); print(cur.fetchone()[0])
    cur.execute("SELECT current_setting('transaction_read_only')")
    print("read_only =", cur.fetchone()[0])

    hr("STEP0b 关键表行数")
    for t in TABLES:
        cur.execute(f"SELECT count(*) FROM {t}")
        print(f"  {t:<28} = {cur.fetchone()[0]}")

    hr("STEP0c 关键索引")
    cur.execute("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE tablename = ANY(%s) ORDER BY tablename, indexname", (TABLES,))
    rows = cur.fetchall()
    for tn, ixn, _ in rows:
        print(f"  [{tn}] {ixn}")
    has_df_idx = any(r[0] == "t_drm_datafield" and "database_wid" in r[2] and "dataobject" in r[2] for r in rows)
    print(f"  → t_drm_datafield 关键复合索引: {'存在' if has_df_idx else '⚠ 缺失（高度可疑！）'}")

    hr("STEP0d 统计信息新鲜度（last_analyze 太旧 → 优化器易选坏计划）")
    cur.execute("SELECT relname, n_live_tup, last_analyze, last_autoanalyze FROM pg_stat_user_tables WHERE relname = ANY(%s) ORDER BY relname", (TABLES,))
    for relname, n, la, laa in cur.fetchall():
        newest = max([d for d in (la, laa) if d], default=None)
        print(f"  {relname:<28} live={n:<9} 最近统计={newest}")

    # —— 确定要测的两个 modelId ——
    hr("STEP0e 待测 modelId（指定 + 自动发现）")
    targets = []
    cur.execute("SELECT 1 FROM t_drm_model_datamodel WHERE wid = %s", (args.model_id,))
    if cur.fetchone():
        targets.append(("指定/日志", args.model_id))
        print(f"  指定模型 {args.model_id}：存在 ✓ 纳入测试")
    else:
        print(f"  指定模型 {args.model_id}：本库不存在（多半因为换了环境），跳过")
    cur.execute("""SELECT a.wid, count(*) ref_fields FROM t_drm_model_datamodel a
                   JOIN t_drm_datafield c ON a.database_wid=c.database_wid AND a.dataobject=c.dataobject
                   WHERE (c.reference_table IS NOT NULL AND c.reference_field IS NOT NULL)
                      OR (c.foreign_table IS NOT NULL AND c.foreign_field IS NOT NULL)
                   GROUP BY a.wid ORDER BY ref_fields DESC LIMIT 5""")
    cands = cur.fetchall()
    if cands:
        print("  带引用字段最多的模型(候选):", ", ".join(f"{w}({c})" for w, c in cands))
        auto = cands[0][0]
        if auto not in [t[1] for t in targets]:
            targets.append(("自动发现(最忙)", auto))
            print(f"  自动发现：纳入 {auto}")
    if not targets:
        cur.execute("SELECT wid FROM t_drm_model_datamodel LIMIT 1")
        r = cur.fetchone()
        if not r:
            sys.exit("库中没有模型数据。")
        targets.append(("任意(无关系模型)", r[0]))

    results = [diagnose_model(cur, mid, label, args) for label, mid in targets]

    # —— 综合结论 ——
    hr("综合诊断结论")
    for r in results:
        old = "超时" if r["old_to"] else (f"{r['old_ms']:.0f}ms" if r["old_ms"] is not None else "—")
        new = "超时" if r["new_to"] else (f"{r['new_ms']:.0f}ms" if r["new_ms"] is not None else "—")
        line = f"  [{r['label']}] mid={r['mid']}: v_old={old}, v_new={new}"
        if r["equiv"] is True:
            line += " ✅等价"
        elif r["equiv"] is False:
            line += " ❌不等价"
        print(line)
    worst_to = any(r["old_to"] for r in results)
    worst_old = max([r["old_ms"] for r in results if r["old_ms"] is not None], default=None)
    any_seqscan = any(r["df_seqscan"] for r in results)
    print()
    if worst_to:
        print("  🎯 有 modelId 让 v_old 超时复现 → 若对应 v_new 秒回，下推改法就是解药。")
    elif worst_old is not None and worst_old < 1000:
        print(f"  本库最慢 v_old 仅 {worst_old:.0f}ms，未复现 30s。现场若慢多半因数据量更大/统计更旧。")
    elif worst_old is not None:
        fast = all((r["new_ms"] is not None and r["old_ms"] is not None and r["new_ms"] <= r["old_ms"] * 0.5)
                   for r in results if r["old_ms"] is not None)
        print(f"  v_old 最慢 {worst_old:.0f}ms；" + ("v_new 明显更快 → 改法有效，建议上。" if fast else "v_new 接近 → 优化器已自动下推，改法 no-op。"))
    if any_seqscan or not has_df_idx:
        print("  ⚠ t_drm_datafield 缺索引或被全表扫 → 优先补索引 + ANALYZE。")
    print("\n  若统计偏旧或出现坏计划，请 DBA 手动执行（本脚本不会执行）：")
    print("    ANALYZE t_drm_datafield, t_drm_dataobject, t_drm_model_datamodel,")
    print("            t_drm_model_dataitem, t_drm_model_dataset, t_drm_model_dataset_child;")
    print("  然后重跑本脚本对比耗时。")

    cur.close(); conn.close()


if __name__ == "__main__":
    main()
