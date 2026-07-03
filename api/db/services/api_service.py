# services_conversation_sqlalchemy.py

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from sqlalchemy import Text as SAText
from sqlalchemy import (
    asc,
    func,
    select,
    update,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql import desc as sa_desc
from sqlalchemy.sql.elements import ColumnElement

from api.db.db_models import API4Conversation, APIToken, Dialog
from api.db.services.common_service import CommonService
from common.time_utils import current_timestamp, datetime_format


class APITokenService(CommonService):
    model = APIToken

    def __init__(self):
        super().__init__(APIToken)

    @classmethod
    def used(cls, db: Session, token: str) -> int:
        """
        等价：将 token 的 update_time / update_date 更新为当前时间。
        返回受影响行数。
        """
        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())
        stmt = (
            update(cls.model)
            .where(cls.model.token == token)
            .values(
                update_time=current_ts,
                update_date=current_date,
            )
        )
        res = db.execute(stmt)
        db.commit()
        return res.rowcount or 0

    @classmethod
    def delete_by_tenant_id(cls, db: Session, tenant_id: str) -> int:
        """根据tenant_id删除所有相关的API Token记录"""
        try:
            result = db.query(cls.model).filter(cls.model.tenant_id == tenant_id).delete(synchronize_session=False)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            raise e


class API4ConversationService(CommonService):
    model = API4Conversation
    _append_message_fields = {
        "name",
        "exp_user_id",
        "message",
        "reference",
        "tokens",
        "source",
        "dsl",
        "duration",
        "thumb_up",
        "errors",
    }
    _non_nullable_append_fields = {"tokens", "duration", "thumb_up"}
    _json_append_fields = {"message", "reference", "dsl"}

    def __init__(self):
        super().__init__(API4Conversation)

    # ---------- 辅助：字段选择 ----------
    @classmethod
    def _all_columns(cls) -> list[ColumnElement]:
        return list(cls.model.__table__.columns)

    @classmethod
    def _columns_excluding(cls, *names: str) -> list[ColumnElement]:
        excl = set(names)
        return [c for c in cls._all_columns() if c.name not in excl]

    # ---------- 列表分页 ----------
    @classmethod
    def get_list(
        cls,
        db: Session,
        dialog_id: str,
        tenant_id: str,  # 保留参数，若后续要按租户隔离可加 where 条件
        page_number: int,
        items_per_page: int,
        orderby: str,
        desc: bool,
        id: str | None = None,
        user_id: str | None = None,
        include_dsl: bool = True,
        keywords: str = "",
        from_date: str | None = None,
        to_date: str | None = None,
        exp_user_id: str | None = None,
    ) -> tuple[int, list[dict]]:
        """
        返回： (count, sessions_as_dicts)
        - include_dsl=False：会排除 dsl 列，降低负载
        - keywords: 对 message 文本做不区分大小写包含（如 message 是 JSON/Text 可按需调整）
        - from_date/to_date: 对 create_date 过滤（字符串或 iso/datetime 均可）
        """
        # 列选择
        fields = cls._all_columns() if include_dsl else cls._columns_excluding("dsl")

        base = select(*fields).where(cls.model.dialog_id == dialog_id)

        if id:
            base = base.where(cls.model.id == id)
        if user_id:
            base = base.where(cls.model.user_id == user_id)

        if keywords:
            # message 若为 JSON/Text，这里统一转 text 处理再 lower
            base = base.where(func.lower(func.cast(cls.model.message, SAText)).contains(keywords.lower()))

        # 处理时间过滤 —— 兼容字符串 / datetime
        def _to_dt(v: str | datetime | None) -> datetime | None:
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            # 字符串解析：尽量宽松，常见 10位/19位
            try:
                if len(v) == 10:
                    return datetime.strptime(v, "%Y-%m-%d")
                if len(v) == 19:
                    return datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
                # 尝试 ISO
                return datetime.fromisoformat(v)
            except Exception:
                # 兜底：直接返回 None（不加过滤）
                return None

        _from = _to_dt(from_date)
        _to = _to_dt(to_date)
        if _from:
            base = base.where(cls.model.create_date >= _from)
        if _to:
            base = base.where(cls.model.create_date <= _to)
        if exp_user_id:
            base = base.where(cls.model.exp_user_id == exp_user_id)

        # 排序
        order_col = getattr(cls.model, orderby)
        base = base.order_by(sa_desc(order_col) if desc else asc(order_col))

        # 统计总数
        total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

        # 分页
        stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)

        rows = db.execute(stmt).mappings().all()
        return total, [dict(r) for r in rows]

    @classmethod
    def get_names(cls, db: Session, dialog_id: str, exp_user_id: str) -> list[dict]:
        stmt = (
            select(cls.model.id, cls.model.name)
            .where(
                cls.model.dialog_id == dialog_id,
                cls.model.exp_user_id == exp_user_id,
            )
            .order_by(sa_desc(cls.model.create_date))
        )
        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    # ---------- 追加消息 & 自增轮次 ----------
    @classmethod
    def append_message(cls, db: Session, id: str, conversation: dict) -> int:
        """
        等价逻辑：
        1) 用最新的 conversation（包含 message/reference/dsl/errors 等）覆盖
        2) round = round + 1
        返回 1/0
        """
        obj = db.get(cls.model, id)
        if not obj:
            return 0

        # Only persist conversation payload fields. Avoid writing transient
        # SQLAlchemy defaults such as tokens=None over non-null database values.
        for k, v in conversation.items():
            if k not in cls._append_message_fields or not hasattr(obj, k):
                continue
            if k in cls._non_nullable_append_fields and v is None:
                continue
            if k in cls._json_append_fields and isinstance(v, (dict, list)):
                v = deepcopy(v)
            setattr(obj, k, v)

        # 轮次自增（处理 None）
        obj.round = (obj.round or 0) + 1

        db.add(obj)
        db.commit()
        return 1

    # ---------- 统计 ----------
    @classmethod
    def stats(
        cls,
        db: Session,
        tenant_id: str,
        from_date: str,
        to_date: str,
        source: str | None = None,
    ) -> list[dict]:
        """
        输出字段：
        - dt（按天聚合）
        - pv: 会话数
        - uv: 去重 user_id 数
        - tokens: SUM(tokens)
        - duration: SUM(duration)
        - round: AVG(round)
        - thumb_up: SUM(thumb_up)

        Postgres: date_trunc('day', create_date)
        MySQL:    DATE(create_date)
        这里做了一个简单的方言判断，以 Postgres 为优先。
        """
        # 结束日补时分秒（兼容传入仅日期）
        if len(to_date) == 10:
            to_date += " 23:59:59"

        # 解析时间
        def _parse_dt(s: str) -> datetime:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if len(s) == 19 else datetime.strptime(s, "%Y-%m-%d")

        dt_from = _parse_dt(from_date)
        dt_to = _parse_dt(to_date)

        # 方言选择
        dialect_name = db.get_bind().dialect.name.lower()
        if "postg" in dialect_name:
            # Postgres
            dt_expr = func.date_trunc("day", cls.model.create_date).label("dt")
        elif "mysql" in dialect_name:
            # MySQL
            dt_expr = func.date(cls.model.create_date).label("dt")
        else:
            # 其它方言：退化为 CAST(date)
            dt_expr = func.date(cls.model.create_date).label("dt")

        # join Dialog 保证租户隔离
        base = (
            select(
                dt_expr,
                func.count(cls.model.id).label("pv"),
                func.count(func.distinct(cls.model.user_id)).label("uv"),
                func.sum(cls.model.tokens).label("tokens"),
                func.sum(cls.model.duration).label("duration"),
                func.avg(cls.model.round).label("round"),
                func.sum(cls.model.thumb_up).label("thumb_up"),
            )
            .select_from(cls.model)
            .join(Dialog, (cls.model.dialog_id == Dialog.id) & (Dialog.tenant_id == tenant_id))
            .where(cls.model.create_date >= dt_from, cls.model.create_date <= dt_to)
        )

        if source is not None:
            base = base.where(cls.model.source == source)

        base = base.group_by(dt_expr).order_by(dt_expr.asc())

        rows = db.execute(base).mappings().all()
        # 将 dt 统一格式化为 "YYYY-MM-DD"
        out = []
        for r in rows:
            d = dict(r)
            dtv = d.get("dt")
            if isinstance(dtv, datetime):
                d["dt"] = dtv.strftime("%Y-%m-%d")
            out.append(d)
        return out

    @classmethod
    def delete_by_dialog_ids(cls, db: Session, dialog_ids: list[str]) -> int:
        """根据对话ID列表删除所有相关的API会话记录"""
        try:
            result = db.query(cls.model).filter(cls.model.dialog_id.in_(dialog_ids)).delete(synchronize_session=False)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            raise e
