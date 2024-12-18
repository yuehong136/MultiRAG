from .user_service import UserService as UserService
import pathlib
import re


def duplicate_name(query_func, **kwargs):
    """
    递归地查找并生成不重复的文件名。

    该函数接受一个查询函数和一个关键字参数字典，用于查询数据库中是否存在相同的文件名。
    如果存在相同的文件名，通过添加序号的方式生成一个新的不重复的文件名。

    :param query_func: 用于查询数据库中是否存在相同文件名的函数。
    :param kwargs: 包含文件名和其他可能的查询条件的关键字参数字典。
    :return: 如果文件名已存在，则返回一个新的不重复的文件名；如果文件名不存在，则返回原文件名。
    """
    # 从关键字参数中提取文件名
    fnm = kwargs["name"]
    # 调用查询函数，根据关键字参数查询是否存在相同的文件名
    objs = query_func(**kwargs)
    # 如果查询结果为空，说明文件名不存在，直接返回原文件名
    if not objs:
        return fnm

    # 获取文件名的后缀
    ext = pathlib.Path(fnm).suffix  # .jpg
    # 去除文件名的后缀，为添加序号做准备
    nm = re.sub(r"%s$" % ext, "", fnm)
    # 正则匹配文件名末尾是否已经包含序号
    r = re.search(r"\(([0-9]+)\)$", nm)
    # 初始化序号为0
    c = 0
    # 如果文件名末尾已经包含序号，则提取并转换为整数
    if r:
        c = int(r.group(1))
        # 去除文件名末尾的序号
        nm = re.sub(r"\([0-9]+\)$", "", nm)
    # 序号加1，为新文件名添加序号
    c += 1
    # 生成新的文件名，格式为"原文件名(序号)"
    nm = f"{nm}({c})"
    # 如果原文件名有后缀，则将后缀添加到新文件名后面
    if ext:
        nm += f"{ext}"

    # 更新关键字参数中的文件名为新生成的文件名，用于递归调用
    kwargs["name"] = nm
    # 递归调用自身，检查新生成的文件名是否已存在，直到找到一个不重复的文件名
    return duplicate_name(query_func, **kwargs)
