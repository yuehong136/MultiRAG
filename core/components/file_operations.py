import io

import pandas as pd

import logging


logger = logging.getLogger(__name__)




async def handle_upload_file(result, file, **kwargs):
    """
    上传文件
    """
    if file is None:
        logger.error(f"未提供文件进行上传")
    content = await file.read()
    try:
        data = pd.read_csv(io.StringIO(content.decode('utf-8')))
        return data
    except pd.errors.ParserError as e:
        logger.error(f"解析CSV文件时出错")