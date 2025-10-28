from timeit import default_timer as timer

from sqlalchemy import text

from api import settings
from api.db.db_models import engine, get_pool_status
from core.utils.redis_conn import REDIS_CONN
from core.utils.storage_factory import STORAGE_IMPL


def _ok_nok(ok: bool) -> str:
    return "ok" if ok else "nok"


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
        STORAGE_IMPL.health()
        return True, {"elapsed": f"{(timer() - st) * 1000.0:.1f}"}
    except Exception as e:
        return False, {"elapsed": f"{(timer() - st) * 1000.0:.1f}", "error": str(e)}


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