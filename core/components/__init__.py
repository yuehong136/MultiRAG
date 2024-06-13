import core.components.file_operations as file_ops
import core.components.data_processing as data_ops
import core.components.sql_operations as sql_ops
import core.components.nl2sql as nl2sql
import core.components.display_operations as display_ops
from core.components.display_operations import convert_to_serializable

def get_action_handlers():
    """
    获取所有action的映射关系
    """
    handlers = {}

    for module in [file_ops, data_ops, sql_ops, nl2sql, display_ops]:
        for name in dir(module):
            obj = getattr(module, name)
            if callable(obj) and obj.__doc__:
                handlers[obj.__doc__.strip()] = obj

    return handlers
