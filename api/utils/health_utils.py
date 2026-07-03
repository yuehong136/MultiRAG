import json
import os
from datetime import datetime
from timeit import default_timer as timer

import requests
from sqlalchemy import text

from api.db.db_models import engine, get_pool_status
from common import settings
from core.utils.es_conn import ESConnection
from core.utils.infinity_conn import InfinityConnection
from core.utils.milvus_conn import MilvusConnection
from core.utils.ob_conn import OBConnection
from core.utils.redis_conn import REDIS_CONN


def _ok_nok(ok: bool) -> str:
    return "ok" if ok else "nok"


def is_health_result_ok(result: object) -> bool:
    """
    Normalize health() return values from different backends.

    Notes:
    - Some storage backends return strict booleans.
    - Some return dicts with status/alive fields.
    - Some return non-bool values (e.g., SDK response/None) and only raise on failure.
      Keep historical behavior for those by treating them as success.
    """
    if isinstance(result, bool):
        return result

    if isinstance(result, dict):
        status = result.get("status")
        if isinstance(status, str):
            lowered = status.lower()
            if lowered in {"green", "ok", "alive", "healthy", "up"}:
                return True
            if lowered in {"red", "nok", "timeout", "unhealthy", "down", "error", "failed"}:
                return False

        alive = result.get("alive")
        if isinstance(alive, bool):
            return alive

        ok = result.get("ok")
        if isinstance(ok, bool):
            return ok

    return True


def check_db() -> tuple[bool, dict]:
    """检查数据库连接是否正常"""
    st = timer()
    try:
        # lightweight probe; works for MySQL/Postgres
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, {"elapsed": f"{(timer() - st) * 1000.0:.1f}"}
    except Exception as e:
        return False, {"elapsed": f"{(timer() - st) * 1000.0:.1f}", "error": str(e)}


def check_db_pool() -> tuple[bool, dict]:
    """
    检查数据库连接池状态

    Returns:
        tuple[bool, dict]: (是否健康, 连接池状态信息)
            - 使用率 > 90%: 不健康
            - 使用率 > 80%: 警告但仍然健康
            - 使用率 <= 80%: 健康
    """
    st = timer()
    try:
        pool_status = get_pool_status()
        elapsed = f"{(timer() - st) * 1000.0:.1f}"

        # 判断连接池健康状态
        usage_rate = pool_status.get('usage_rate', 0)
        is_healthy = usage_rate <= 90  # 使用率超过90%认为不健康

        return is_healthy, {
            "elapsed": elapsed,
            "pool_size": pool_status.get('pool_size'),
            "checked_out": pool_status.get('checked_out'),
            "checked_in": pool_status.get('checked_in'),
            "usage_rate": f"{usage_rate}%",
            "status": pool_status.get('status')
        }
    except Exception as e:
        return False, {
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
            "error": str(e)
        }


def check_redis() -> tuple[bool, dict]:
    st = timer()
    try:
        ok = bool(REDIS_CONN.health())
        return ok, {"elapsed": f"{(timer() - st) * 1000.0:.1f}"}
    except Exception as e:
        return False, {"elapsed": f"{(timer() - st) * 1000.0:.1f}", "error": str(e)}


def check_doc_engine() -> tuple[bool, dict]:
    st = timer()
    try:
        meta = settings.docStoreConn.health()
        # treat any successful call as ok
        return True, {"elapsed": f"{(timer() - st) * 1000.0:.1f}", **(meta or {})}
    except Exception as e:
        return False, {"elapsed": f"{(timer() - st) * 1000.0:.1f}", "error": str(e)}


def check_storage() -> tuple[bool, dict]:
    st = timer()
    try:
        health_result = settings.STORAGE_IMPL.health()
        ok = is_health_result_ok(health_result)
        meta = {"elapsed": f"{(timer() - st) * 1000.0:.1f}"}

        if isinstance(health_result, dict):
            meta["health"] = health_result
        if not ok:
            meta["error"] = f"storage health returned unhealthy result: {health_result!r}"

        return ok, meta
    except Exception as e:
        return False, {"elapsed": f"{(timer() - st) * 1000.0:.1f}", "error": str(e)}

def get_es_cluster_stats() -> dict:
    doc_engine = os.getenv('DOC_ENGINE', 'milvus')
    if doc_engine != 'elasticsearch':
        raise Exception("Elasticsearch is not in use.")
    try:
        return {
            "status": "alive",
            "message": ESConnection().get_cluster_stats()
        }
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
        }


def get_infinity_status():
    doc_engine = os.getenv('DOC_ENGINE', 'milvus')
    if doc_engine != 'infinity':
        raise Exception("Infinity is not in use.")
    try:
        return {
            "status": "alive",
            "message": InfinityConnection().health()
        }
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
        }


def get_oceanbase_status() -> dict:
    doc_engine = os.getenv('DOC_ENGINE', 'milvus')
    if doc_engine != 'oceanbase':
        raise Exception("OceanBase is not in use.")
    try:
        ob_conn = OBConnection()
        health_info = ob_conn.health()
        performance_metrics = ob_conn.get_performance_metrics()
        status = "alive" if health_info.get("status") == "healthy" else "timeout"
        return {
            "status": status,
            "message": {
                "health": health_info,
                "performance": performance_metrics
            }
        }
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
        }


def check_oceanbase_health() -> dict:
    doc_engine = os.getenv('DOC_ENGINE', 'milvus')
    if doc_engine != 'oceanbase':
        return {
            "status": "not_configured",
            "details": {
                "connection": "not_configured",
                "message": "OceanBase is not configured as the document engine"
            }
        }
    try:
        ob_conn = OBConnection()
        health_info = ob_conn.health()
        performance_metrics = ob_conn.get_performance_metrics()
        connection_status = performance_metrics.get("connection", "unknown")

        if connection_status == "disconnected" or health_info.get("status") != "healthy":
            return {
                "status": "unhealthy",
                "details": {
                    "connection": connection_status,
                    "latency_ms": performance_metrics.get("latency_ms", 0),
                    "storage_used": performance_metrics.get("storage_used", "N/A"),
                    "storage_total": performance_metrics.get("storage_total", "N/A"),
                    "query_per_second": performance_metrics.get("query_per_second", 0),
                    "slow_queries": performance_metrics.get("slow_queries", 0),
                    "active_connections": performance_metrics.get("active_connections", 0),
                    "max_connections": performance_metrics.get("max_connections", 0),
                    "uri": health_info.get("uri", "unknown"),
                    "version": health_info.get("version_comment", "unknown"),
                    "error": health_info.get("error", performance_metrics.get("error"))
                }
            }

        is_healthy = (
            connection_status == "connected" and
            performance_metrics.get("latency_ms", float('inf')) < 1000
        )
        return {
            "status": "healthy" if is_healthy else "degraded",
            "details": {
                "connection": performance_metrics.get("connection", "unknown"),
                "latency_ms": performance_metrics.get("latency_ms", 0),
                "storage_used": performance_metrics.get("storage_used", "N/A"),
                "storage_total": performance_metrics.get("storage_total", "N/A"),
                "query_per_second": performance_metrics.get("query_per_second", 0),
                "slow_queries": performance_metrics.get("slow_queries", 0),
                "active_connections": performance_metrics.get("active_connections", 0),
                "max_connections": performance_metrics.get("max_connections", 0),
                "uri": health_info.get("uri", "unknown"),
                "version": health_info.get("version_comment", "unknown")
            }
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "details": {
                "connection": "disconnected",
                "error": str(e)
            }
        }


def get_vastbase_status() -> dict:
    """
    获取 VastBase 向量数据库状态信息

    Returns:
        dict: 包含 alive 状态和详细统计信息的字典
    """
    from core.utils.vastbase_conn import VastBaseConnection
    start_time = timer()
    doc_engine = os.getenv('DOC_ENGINE', 'milvus')
    if doc_engine != 'vastbase':
        return {
            "status": "timeout",
            "message": f"VastBase is not in use. Current DOC_ENGINE: {doc_engine}",
            "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
        }
    try:
        conn = VastBaseConnection()
        health = conn.health()
        elapsed = f"{(timer() - start_time) * 1000.0:.1f} ms"

        if health.get('status') == 'green':
            return {
                "status": "alive",
                "message": {
                    "host": conn.host,
                    "port": conn.port,
                    "database": conn.database,
                    "schema": conn.schema,
                    "max_connections": conn.max_connections,
                    "health_status": health.get('status'),
                },
                "elapsed": elapsed
            }
        else:
            return {
                "status": "timeout",
                "message": f"VastBase status: {health.get('status')}. Error: {health.get('error')}",
                "elapsed": elapsed
            }
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
            "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
        }


def get_milvus_cluster_stats() -> dict:
    """
    获取 Milvus 集群统计信息

    Returns:
        dict: 包含 alive 状态和详细统计信息的字典
    """
    start_time = timer()
    doc_engine = os.getenv('DOC_ENGINE', 'milvus')
    if doc_engine != 'milvus':
        return {
            "status": "timeout",
            "message": f"Milvus is not in use. Current DOC_ENGINE: {doc_engine}",
            "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
        }
    try:
        stats = MilvusConnection().get_cluster_stats()
        elapsed = f"{(timer() - start_time) * 1000.0:.1f} ms"
        return {
            "status": "alive",
            "message": stats,
            "elapsed": elapsed
        }
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
            "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
        }


def check_milvus_alive() -> dict:
    """
    检查 Milvus 服务器是否存活（轻量级检查）

    Returns:
        dict: 包含 alive 状态和响应时间的字典
    """
    start_time = timer()
    try:
        # 轻量级健康检查
        health = MilvusConnection().health()
        elapsed = f"{(timer() - start_time) * 1000.0:.1f} ms"

        if health.get('status') == 'green':
            return {
                'status': "alive",
                'message': f"Milvus {health.get('server_version')} is healthy. Elapsed: {elapsed}",
                'elapsed': elapsed,
                'version': health.get('server_version'),
                'server_type': health.get('type')
            }
        else:
            return {
                'status': "timeout",
                'message': f"Milvus status: {health.get('status')}. Elapsed: {elapsed}",
                'elapsed': elapsed
            }
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
            "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
        }


def get_database_status() -> dict:
    """
    获取数据库状态信息（进程列表）

    支持 PostgreSQL 和 MySQL
    - PostgreSQL: 查询 pg_stat_activity 视图
    - MySQL: 执行 SHOW PROCESSLIST 命令

    Returns:
        dict: 包含 alive 状态和进程列表信息的字典
    """
    start_time = timer()
    try:
        with engine.connect() as connection:
            # 检测数据库类型
            db_dialect = engine.dialect.name.lower()

            if db_dialect == 'postgresql':
                # PostgreSQL: 使用 pg_stat_activity 视图
                query = text("""
                    SELECT
                        pid as id,
                        usename as user,
                        client_addr as host,
                        datname as database,
                        state,
                        query_start,
                        state_change,
                        wait_event_type,
                        wait_event,
                        query
                    FROM pg_stat_activity
                    WHERE pid <> pg_backend_pid()
                    ORDER BY query_start DESC
                    LIMIT 100
                """)
                result = connection.execute(query)
                rows = result.fetchall()
                columns = result.keys()

                # 转换为字典列表
                process_list = []
                for row in rows:
                    process_info = dict(zip(columns, row))
                    # 格式化时间字段
                    if process_info.get('query_start'):
                        process_info['query_start'] = str(process_info['query_start'])
                    if process_info.get('state_change'):
                        process_info['state_change'] = str(process_info['state_change'])
                    # 转换 IP 地址
                    if process_info.get('host'):
                        process_info['host'] = str(process_info['host'])
                    process_list.append(process_info)

                return {
                    "status": "alive",
                    "database_type": "postgresql",
                    "process_count": len(process_list),
                    "message": process_list,
                    "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
                }

            elif db_dialect == 'mysql':
                # MySQL: 使用 SHOW PROCESSLIST
                query = text("SHOW PROCESSLIST")
                result = connection.execute(query)
                rows = result.fetchall()
                columns = result.keys()

                # 转换为字典列表
                process_list = [dict(zip(columns, row)) for row in rows]

                return {
                    "status": "alive",
                    "database_type": "mysql",
                    "process_count": len(process_list),
                    "message": process_list,
                    "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
                }
            else:
                # 不支持的数据库类型
                return {
                    "status": "timeout",
                    "database_type": db_dialect,
                    "message": f"Unsupported database type: {db_dialect}",
                    "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
                }

    except Exception as e:
        return {
            "alive": False,
            "message": f"error: {e!s}",
            "elapsed": f"{(timer() - start_time) * 1000.0:.1f} ms"
        }


def _minio_scheme_and_verify():
    """
    Determine URL scheme (http/https) and SSL verify flag for MinIO health check.
    Uses MINIO.secure for scheme and MINIO.verify for certificate verification
    (e.g. self-signed certs when verify is False).
    """
    secure = settings.MINIO.get("secure", False)
    if isinstance(secure, str):
        secure = secure.lower() in ("true", "1", "yes")
    scheme = "https" if secure else "http"
    verify = settings.MINIO.get("verify", True)
    if isinstance(verify, str):
        verify = verify.lower() not in ("false", "0", "no")
    elif isinstance(verify, bool):
        pass
    else:
        verify = bool(verify)
    return scheme, verify


def check_minio_alive():
    """
    Check MinIO service liveness via /minio/health/live.
    Uses http or https and optional certificate verification based on
    MINIO.secure and MINIO.verify configuration.
    """
    start_time = timer()
    try:
        scheme, verify = _minio_scheme_and_verify()
        url = f"{scheme}://{settings.MINIO['host']}/minio/health/live"
        response = requests.get(url, timeout=10, verify=verify)
        if response.status_code == 200:
            return {'status': "alive", "message": f"Confirm elapsed: {(timer() - start_time) * 1000.0:.1f} ms."}
        return {'status': "timeout", "message": f"Confirm elapsed: {(timer() - start_time) * 1000.0:.1f} ms."}
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
        }


def get_redis_info():
    try:
        return {
            "status": "alive",
            "message": REDIS_CONN.info()
        }
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
        }


def check_multirag_server_alive():
    start_time = timer()
    try:
        url = f'http://{settings.HOST_IP}:{settings.HOST_PORT}/api/v1/system/ping'
        if '0.0.0.0' in url:
            url = url.replace('0.0.0.0', '127.0.0.1')
        response = requests.get(url)
        if response.status_code == 200:
            return {'status': "alive", "message": f"Confirm elapsed: {(timer() - start_time) * 1000.0:.1f} ms."}
        else:
            return {'status': "timeout", "message": f"Confirm elapsed: {(timer() - start_time) * 1000.0:.1f} ms."}
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}",
        }


def check_task_executor_alive():
    task_executor_heartbeats = {}
    try:
        task_executors = REDIS_CONN.smembers("TASKEXE")
        now = datetime.now().timestamp()
        for task_executor_id in task_executors:
            heartbeats = REDIS_CONN.zrangebyscore(task_executor_id, now - 60 * 30, now)
            heartbeats = [json.loads(heartbeat) for heartbeat in heartbeats]
            task_executor_heartbeats[task_executor_id] = heartbeats
        if task_executor_heartbeats:
            status = "alive" if any(task_executor_heartbeats.values()) else "timeout"
            return {"status": status, "message": task_executor_heartbeats}
        else:
            return {"status": "timeout", "message": "Not found any task executor."}
    except Exception as e:
        return {
            "status": "timeout",
            "message": f"error: {e!s}"
        }


def check_chat() -> tuple[bool, dict]:
    st = timer()
    try:
        cfg = getattr(settings, "CHAT_CFG", None)
        ok = bool(cfg and cfg.get("factory"))
        return ok, {"elapsed": f"{(timer() - st) * 1000.0:.1f}"}
    except Exception as e:
        return False, {"elapsed": f"{(timer() - st) * 1000.0:.1f}", "error": str(e)}


def run_health_checks() -> tuple[dict, bool]:
    """
    运行所有健康检查

    Returns:
        tuple[dict, bool]: (健康检查结果, 是否全部正常)
            - 结果包含各个组件的状态 (ok/nok)
            - 如果关键组件（db, chat）都正常，则返回 True
    """
    result: dict[str, str | dict] = {}

    # 关键组件检查
    db_ok, db_meta = check_db()
    chat_ok, chat_meta = check_chat()

    result["db"] = _ok_nok(db_ok)
    if not db_ok:
        result.setdefault("_meta", {})["db"] = db_meta

    result["chat"] = _ok_nok(chat_ok)
    if not chat_ok:
        result.setdefault("_meta", {})["chat"] = chat_meta

    # 数据库连接池检查（新增）
    try:
        pool_ok, pool_meta = check_db_pool()
        result["db_pool"] = _ok_nok(pool_ok)
        # 无论是否健康，都显示连接池状态信息
        result.setdefault("_meta", {})["db_pool"] = pool_meta
    except Exception:
        result["db_pool"] = "nok"

    # 可选组件检查（不影响整体健康状态）
    try:
        redis_ok, redis_meta = check_redis()
        result["redis"] = _ok_nok(redis_ok)
        if not redis_ok:
            result.setdefault("_meta", {})["redis"] = redis_meta
    except Exception:
        result["redis"] = "nok"

    try:
        doc_ok, doc_meta = check_doc_engine()
        result["doc_engine"] = _ok_nok(doc_ok)
        if not doc_ok:
            result.setdefault("_meta", {})["doc_engine"] = doc_meta
    except Exception:
        result["doc_engine"] = "nok"

    try:
        sto_ok, sto_meta = check_storage()
        result["storage"] = _ok_nok(sto_ok)
        if not sto_ok:
            result.setdefault("_meta", {})["storage"] = sto_meta
    except Exception:
        result["storage"] = "nok"

    # 整体健康状态：只要关键组件（db, chat）正常即可
    all_ok = (result.get("db") == "ok") and (result.get("chat") == "ok")
    result["status"] = "ok" if all_ok else "nok"
    return result, all_ok
