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
from common.file_utils import get_project_base_directory
from common.misc_utils import pip_install_torch

# Server
RAG_CONF_PATH = os.path.join(get_project_base_directory(), "configs")

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
