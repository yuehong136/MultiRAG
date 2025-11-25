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
from api.utils.configs import get_base_config, decrypt_database_config
from api.utils.file_utils import get_project_base_directory
from api.utils.common import pip_install_torch

# Server
RAG_CONF_PATH = os.path.join(get_project_base_directory(), "configs")

# Get storage type and document engine from system environment variables
STORAGE_IMPL_TYPE = os.getenv('STORAGE_IMPL', 'MINIO')
DOC_ENGINE = os.getenv('DOC_ENGINE', 'milvus')

ES = {}
MILVUS = {}
INFINITY = {}
AZURE = {}
S3 = {}
MINIO = {}
OSS = {}
OS = {}

# Initialize the selected configuration data based on environment variables to solve the problem of initialization errors due to lack of configuration
if DOC_ENGINE == 'elasticsearch':
    ES = get_base_config("es", {})
elif DOC_ENGINE == 'opensearch':
    OS = get_base_config("os", {})
elif DOC_ENGINE == 'milvus':
    MILVUS = get_base_config("milvus", {})
elif DOC_ENGINE == 'infinity':
    INFINITY = get_base_config("infinity", {"uri": "infinity:23817"})

if STORAGE_IMPL_TYPE in ['AZURE_SPN', 'AZURE_SAS']:
    AZURE = get_base_config("azure", {})
elif STORAGE_IMPL_TYPE == 'AWS_S3':
    S3 = get_base_config("s3", {})
elif STORAGE_IMPL_TYPE == 'MINIO':
    MINIO = decrypt_database_config(name="minio")
elif STORAGE_IMPL_TYPE == 'OSS':
    OSS = get_base_config("oss", {})

try:
    REDIS = decrypt_database_config(name="redis")
except Exception:
    try:
        REDIS = get_base_config("redis", {})
    except Exception:
        REDIS = {}
DOC_MAXIMUM_SIZE = int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))
DOC_BULK_SIZE = int(os.environ.get("DOC_BULK_SIZE", 4))
EMBEDDING_BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", 16))
SVR_QUEUE_NAME = "multi_rag_svr_queue"
SVR_CONSUMER_GROUP_NAME = "multi_rag_svr_task_broker"
PAGERANK_FLD = "pagerank_fea"
TAG_FLD = "tag_feas"

PARALLEL_DEVICES = 0
try:
    pip_install_torch()
    import torch.cuda
    PARALLEL_DEVICES = torch.cuda.device_count()
    logging.info(f"found {PARALLEL_DEVICES} gpus")
except Exception:
    logging.info("can't import package 'torch'")

def print_rag_settings():
    logging.info(f"MAX_CONTENT_LENGTH: {DOC_MAXIMUM_SIZE}")
    logging.info(f"MAX_FILE_COUNT_PER_USER: {int(os.environ.get('MAX_FILE_NUM_PER_USER', 0))}")


def get_svr_queue_name(priority: int) -> str:
    if priority == 0:
        return SVR_QUEUE_NAME
    return f"{SVR_QUEUE_NAME}_{priority}"

def get_svr_queue_names():
    return [get_svr_queue_name(priority) for priority in [1, 0]]
