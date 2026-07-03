def process_data(result, method, **kwargs):
    """
    数据处理
    """
    if result is None:
        raise ValueError("没有数据可处理")
    if method == "数值列求和":
        result["Sum"] = result.select_dtypes(include="number").sum(axis=1)
    elif method == "数值列求平均":
        result["Mean"] = result.select_dtypes(include="number").mean(axis=1)
    return result
