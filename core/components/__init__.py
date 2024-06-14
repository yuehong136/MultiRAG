import os

import core.components.file_operations as file_ops
import core.components.data_processing as data_ops
import core.components.sql_operations as sql_ops
import core.components.nl2sql as nl2sql
import core.components.display_operations as display_ops
import core.components.llm as llm_ops
from core.components.display_operations import convert_to_serializable

def get_action_handlers():
    """
    获取所有操作处理函数的映射。

    该函数遍历指定模块，寻找所有可调用对象，并将其文档字符串作为键，
    对象本身作为值，存储在字典中。这样做的目的是为了提供一个操作名称到具体
    实现函数的映射，方便后续根据操作名称来执行相应的操作。

    Returns:
        dict: 包含所有操作处理函数的字典，键为操作名称（即函数的文档字符串），
              值为对应的操作处理函数。
    """
    # 初始化一个空字典，用于存储操作处理函数的映射
    handlers = {}
    core_components_path = os.path.abspath(os.path.dirname(__file__))

    # 遍历包含操作处理函数的模块列表
    for module in [file_ops, data_ops, sql_ops, nl2sql, display_ops, llm_ops]:
        # 遍历模块中的所有属性名称
        for name in dir(module):
            # 通过属性名称获取属性对象
            obj = getattr(module, name)
            # 检查对象是否为可调用对象，并且具有文档字符串
            if callable(obj) and obj.__module__.startswith("core.components"):
                obj_module_file = os.path.abspath(module.__file__)
                if obj_module_file.startswith(core_components_path) and obj.__doc__:
                    handlers[obj.__doc__.strip()] = obj

    # 返回操作处理函数的映射字典
    return handlers

if __name__ == "__main__":
    handlers = get_action_handlers()
    print(handlers)  # 输出包含所有自定义操作处理函数的字典
