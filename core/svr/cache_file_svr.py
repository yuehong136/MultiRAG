import time
import traceback

from api.db.database import SessionLocal
from api.db.services.task_service import TaskService
from core.settings import cron_logger
from core.utils.storage_factory import STORAGE_IMPL
from core.utils.redis_conn import REDIS_CONN


def collect(db):
    """
    收集当前正在进行的任务的文档位置信息。

    从任务服务中获取正在进行的任务的文档名称列表。如果列表为空，等待一段时间后返回。

    返回:
        - 如果有任务，则返回文档位置列表；如果没有任务，则返回None。
    """
    # 从任务服务中获取当前正在进行的任务的文档名称列表
    doc_locations = TaskService.get_ongoing_doc_name(db)
    # 输出文档位置信息
    print(doc_locations)
    # 如果列表为空，等待1秒后返回
    if len(doc_locations) == 0:
        time.sleep(1)
        return
    return doc_locations

def main(db):
    """
    主函数，负责执行任务的收集和缓存处理。

    首先收集当前正在进行的任务的文档位置信息，然后对每个任务尝试从MinIO获取文件内容，并将其缓存到Redis中。
    """
    # 收集当前正在进行的任务的文档位置信息
    locations = collect(db)
    # 如果没有收集到任务信息，则直接返回
    if not locations:
        return
    # 输出任务数量
    print("TASKS:", len(locations))
    # 遍历每个任务的文档位置信息
    for kb_id, loc in locations:
        try:
            # 检查Redis连接是否存活
            if REDIS_CONN.is_alive():
                try:
                    # 构建任务的Redis键
                    key = "{}/{}".format(kb_id, loc)
                    if REDIS_CONN.exist(key):
                        continue
                    # file_bin = MINIO.get(kb_id, loc)
                    file_bin = STORAGE_IMPL.get(kb_id, loc)
                    REDIS_CONN.transaction(key, file_bin, 12 * 60)
                    # 记录缓存操作的日志
                    cron_logger.info("CACHE: {}".format(loc))
                except Exception as e:
                    # 输出异常堆栈信息
                    traceback.print_stack(e)
        except Exception as e:
            # 输出异常堆栈信息
            traceback.print_stack(e)



if __name__ == "__main__":
    while True:
        db = SessionLocal()
        main(db)
        db.colse()
        time.sleep(1)