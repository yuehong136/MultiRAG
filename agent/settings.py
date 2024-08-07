# coding=utf-8
"""
@project: multirag
@Author：龙
@file： settings.py
@date：2024/8/7 10:33
@desc:
"""
import os

from api.utils.file_utils import get_project_base_directory
from api.utils.log_utils import LoggerFactory, getLogger

DEBUG = 0
LoggerFactory.set_directory(
    os.path.join(
        get_project_base_directory(),
        "logs",
        "flow"))
# {CRITICAL: 50, FATAL:50, ERROR:40, WARNING:30, WARN:30, INFO:20, DEBUG:10, NOTSET:0}
LoggerFactory.LEVEL = 30

flow_logger = getLogger("flow")
database_logger = getLogger("database")
FLOAT_ZERO = 1e-8
PARAM_MAXDEPTH = 5