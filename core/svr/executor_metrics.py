"""task_executor 的 Prometheus 指标（方案 §8-2）。

指标定义与 HTTP 端点集中在本模块，task_executor 内只保留单行调用点：
- ``start_metrics_server(consumer_no)``：main() 启动时调用一次；
- ``set_queue_stats(pending, lag)``：report_status 心跳循环回填；
- ``TaskTimer``：handle_task 中包裹 do_handle_task，收尾自动记时延/完成/失败。

端口 = ``task_executor.metrics_port``（service_conf，默认 9464）+ worker 序号，
多 worker 同机不撞口；9091 被 milvus healthz 占用，不要配到那里。
"""

import logging
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from common.exceptions import TaskCanceledException

logger = logging.getLogger(__name__)

PENDING_TASKS = Gauge("multirag_task_executor_pending_tasks", "Redis Streams 中当前 pending（已投递未 ack）的任务数")
LAG_TASKS = Gauge("multirag_task_executor_lag_tasks", "Redis Streams consumer group 的 lag（未投递给消费者的积压数）")
TASK_DURATION = Histogram(
    "multirag_task_executor_task_duration_seconds",
    "单个任务的处理时延（成功任务）",
    labelnames=("task_type",),
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)
TASKS_DONE = Counter("multirag_task_executor_tasks_done_total", "处理完成的任务数（含取消）", labelnames=("task_type",))
TASKS_FAILED = Counter("multirag_task_executor_tasks_failed_total", "处理失败的任务数", labelnames=("task_type", "parser_id"))


def _normalize_task_type(task_type: str | None) -> str:
    """空 task_type 是普通解析任务，规整为 'parse' 便于聚合。"""
    return task_type or "parse"


def resolve_port(base_port: int, consumer_no: str) -> int:
    """metrics 监听口 = 基准口 + worker 序号；非数字序号（自定义消费者名）不偏移。"""
    try:
        return base_port + int(consumer_no)
    except ValueError:
        return base_port


def start_metrics_server(consumer_no: str) -> None:
    """按 service_conf 的 task_executor 段启动 /metrics HTTP 端点（每进程一次）。"""
    from common.app_config import get_app_config

    conf = get_app_config().task_executor
    if not conf.metrics_enabled:
        return
    port = resolve_port(conf.metrics_port, consumer_no)
    try:
        start_http_server(port)
    except OSError as e:
        # 指标端点不可用不应阻断任务处理（例如端口被占），记错继续跑
        logger.error("metrics server failed to bind port %s: %s", port, e)
    else:
        logger.info("metrics server listening on :%s/metrics", port)


def set_queue_stats(pending: int, lag: int) -> None:
    PENDING_TASKS.set(pending)
    LAG_TASKS.set(lag)


class TaskTimer:
    """handle_task 中包裹 do_handle_task 的计时上下文。

    成功记时延 + done；取消（TaskCanceledException）只记 done，与调用方
    把取消归入 DONE_TASKS 的语义一致；其余异常只记失败。异常一律向外传播。
    """

    def __init__(self, task_type: str | None, parser_id: str | None):
        self.task_type = _normalize_task_type(task_type)
        self.parser_id = parser_id or "unknown"
        self._start = 0.0

    def __enter__(self) -> "TaskTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        if exc_type is None:
            TASK_DURATION.labels(task_type=self.task_type).observe(time.perf_counter() - self._start)
            TASKS_DONE.labels(task_type=self.task_type).inc()
        elif issubclass(exc_type, TaskCanceledException):
            TASKS_DONE.labels(task_type=self.task_type).inc()
        else:
            TASKS_FAILED.labels(task_type=self.task_type, parser_id=self.parser_id).inc()
