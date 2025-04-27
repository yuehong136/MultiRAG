# coding=utf-8
"""
@project: multirag
@Author：龙
@file： core.settings.py
@date：2024/7/9 9:00
@desc:
"""
import logging
import os
from api.utils import get_base_config, decrypt_database_config
from api.utils.file_utils import get_project_base_directory

# Server
RAG_CONF_PATH = os.path.join(get_project_base_directory(), "configs")

ES = get_base_config("es", {})
# INFINITY = get_base_config("infinity", {"uri": "infinity:23817"})
MILVUS = get_base_config("milvus", {})
AZURE = get_base_config("azure", {})
S3 = get_base_config("s3", {})
MINIO = decrypt_database_config(name="minio")
OSS = get_base_config("oss", {})
try:
    REDIS = decrypt_database_config(name="redis")
except Exception as e:
    REDIS = {}
    pass
DOC_MAXIMUM_SIZE = int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))

SVR_QUEUE_NAME = "multi_rag_svr_queue"
SVR_CONSUMER_GROUP_NAME = "multi_rag_svr_task_broker"
PAGERANK_FLD = "pagerank_fea"
TAG_FLD = "tag_feas"
# SVR_QUEUE_RETENTION = 60*60
# SVR_QUEUE_MAX_LEN = 1024
# SVR_CONSUMER_NAME = "multi_rag_svr_consumer"
# SVR_CONSUMER_GROUP_NAME = "multi_rag_svr_consumer_group"


def print_rag_settings():
    logging.info(f"MAX_CONTENT_LENGTH: {DOC_MAXIMUM_SIZE}")
    logging.info(f"MAX_FILE_COUNT_PER_USER: {int(os.environ.get('MAX_FILE_NUM_PER_USER', 0))}")


def get_svr_queue_name(priority: int) -> str:
    if priority == 0:
        return SVR_QUEUE_NAME
    return f"{SVR_QUEUE_NAME}_{priority}"

def get_svr_queue_names():
    return [get_svr_queue_name(priority) for priority in [1, 0]]
# import logging
# from api.utils.log_utils import LoggerFactory, getLogger
# SUBPROCESS_STD_LOG_NAME = "std.log"
# # Logger
# LoggerFactory.set_directory(
#     os.path.join(
#         get_project_base_directory(),
#         "logs",
#         "rag"))
# # {CRITICAL: 50, FATAL:50, ERROR:40, WARNING:30, WARN:30, INFO:20, DEBUG:10, NOTSET:0}
# LoggerFactory.LEVEL = 30
#
# es_logger = getLogger("es")
# milvus_logger = getLogger("milvus")
# minio_logger = getLogger("minio")
# s3_logger = getLogger("s3")
# azure_logger = getLogger("azure")
# cron_logger = getLogger("cron_logger")
# cron_logger.setLevel(20)
# chunk_logger = getLogger("chunk_logger")
# database_logger = getLogger("database")
# aiforbi_logger = getLogger("aiforbi")
# ai_translate_logger = getLogger("aitranslate")
#
# formatter = logging.Formatter("%(asctime)-15s %(levelname)-8s (%(process)d) %(message)s")
# for logger in [milvus_logger, minio_logger, s3_logger, azure_logger, cron_logger, chunk_logger, database_logger, aiforbi_logger, ai_translate_logger]:
#     logger.setLevel(logging.INFO)
#     for handler in logger.handlers:
#         handler.setFormatter(fmt=formatter)

