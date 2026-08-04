"""建表顺序必须是外键拓扑序，不能是类名字母序。

回归背景：`init_database_tables` 逐张 `Table.create()`，而 `inspect.getmembers`
按类名字母序返回。Channel 四表里 ChannelBinding / ChannelSecret /
ChannelRuntimeStatus 都外键引用 ChatChannel，字母序上 "Chann..." < "Chat..."，
父表排在三个子表之后 —— 空库首次初始化时三张子表全部 UndefinedTable 失败。
这条路径只在空库首启建表，已有库走 skip，所以本地长期不暴露、CI 每次新库必然复现。
"""

from api.db.db_models import Base, BaseModel, models_in_fk_creation_order


def test_parent_tables_are_created_before_their_children() -> None:
    """任何被外键引用的表，都必须排在引用它的表之前。"""
    order = {model.__tablename__: index for index, model in enumerate(models_in_fk_creation_order())}

    for table in Base.metadata.sorted_tables:
        child = order.get(table.name)
        if child is None:
            continue
        for fk in table.foreign_keys:
            parent = order.get(fk.column.table.name)
            if parent is None:
                continue
            assert parent < child, f"{table.name} 外键引用 {fk.column.table.name}，但父表被排在了后面（{parent} >= {child}）"


def test_channel_parent_precedes_the_three_dependent_tables() -> None:
    """钉住具体的回归现场：字母序会把 t_ai_chat_channels 排到最后。"""
    order = {model.__tablename__: index for index, model in enumerate(models_in_fk_creation_order())}
    parent = order["t_ai_chat_channels"]

    for dependent in ("t_ai_channel_bindings", "t_ai_channel_secrets"):
        assert parent < order[dependent], f"t_ai_chat_channels 必须先于 {dependent} 建"

    # runtime_status 引用的是 bindings，形成两级链条
    assert order["t_ai_channel_bindings"] < order["t_ai_channel_runtime_status"]


def test_every_mapped_model_is_returned() -> None:
    """排序不得丢模型：数量要和字母序那版完全一致。"""
    import inspect
    import sys

    module = sys.modules["api.db.db_models"]
    expected = {obj for _, obj in inspect.getmembers(module, inspect.isclass) if obj is not BaseModel and issubclass(obj, BaseModel)}

    assert set(models_in_fk_creation_order()) == expected
