import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def display_results(data, **kwargs):
    """
    显示结果
    """
    logger.info("显示结果")
    return convert_to_serializable(data)


def convert_to_serializable(data):
    if isinstance(data, pd.DataFrame):
        return data.astype(str).to_dict(orient="records")  # 转换为字符串以确保兼容性
    elif isinstance(data, np.generic):
        return data.item()
    elif isinstance(data, dict):
        return {k: convert_to_serializable(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_to_serializable(i) for i in data]
    return data
