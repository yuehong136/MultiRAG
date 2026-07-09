"""core/svr/executor_metrics.py 单元测试（方案 §8-2）。

对默认 registry 直接断言样本值（模块级指标为进程单例，测试内只做相对断言，
不假设初值为零）。start_http_server 的真实监听由冒烟验收覆盖。
"""

import pytest
from prometheus_client import generate_latest

from common.exceptions import TaskCanceledException
from core.svr import executor_metrics


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(name, labels or {})
    return value if value is not None else 0.0


class TestQueueStats:
    def test_gauges_reflect_latest_values(self):
        executor_metrics.set_queue_stats(7, 3)

        assert _sample("multirag_task_executor_pending_tasks") == 7
        assert _sample("multirag_task_executor_lag_tasks") == 3

        executor_metrics.set_queue_stats(0, 0)

        assert _sample("multirag_task_executor_pending_tasks") == 0


class TestTaskTimer:
    def test_success_records_duration_and_done(self):
        before_done = _sample("multirag_task_executor_tasks_done_total", {"task_type": "raptor"})
        before_count = _sample("multirag_task_executor_task_duration_seconds_count", {"task_type": "raptor"})

        with executor_metrics.TaskTimer("raptor", "naive"):
            pass

        assert _sample("multirag_task_executor_tasks_done_total", {"task_type": "raptor"}) == before_done + 1
        assert _sample("multirag_task_executor_task_duration_seconds_count", {"task_type": "raptor"}) == before_count + 1

    def test_empty_task_type_normalizes_to_parse(self):
        before = _sample("multirag_task_executor_tasks_done_total", {"task_type": "parse"})

        with executor_metrics.TaskTimer("", "naive"):
            pass

        assert _sample("multirag_task_executor_tasks_done_total", {"task_type": "parse"}) == before + 1

    def test_failure_counts_by_parser_and_propagates(self):
        labels = {"task_type": "parse", "parser_id": "paper"}
        before = _sample("multirag_task_executor_tasks_failed_total", labels)

        with pytest.raises(ValueError):
            with executor_metrics.TaskTimer(None, "paper"):
                raise ValueError("boom")

        assert _sample("multirag_task_executor_tasks_failed_total", labels) == before + 1

    def test_cancel_counts_done_not_failed_and_propagates(self):
        done_before = _sample("multirag_task_executor_tasks_done_total", {"task_type": "graphrag"})
        failed_before = _sample("multirag_task_executor_tasks_failed_total", {"task_type": "graphrag", "parser_id": "unknown"})

        with pytest.raises(TaskCanceledException):
            with executor_metrics.TaskTimer("graphrag", None):
                raise TaskCanceledException("canceled")

        assert _sample("multirag_task_executor_tasks_done_total", {"task_type": "graphrag"}) == done_before + 1
        assert _sample("multirag_task_executor_tasks_failed_total", {"task_type": "graphrag", "parser_id": "unknown"}) == failed_before


class TestPortResolution:
    @pytest.mark.parametrize(("base", "no", "expected"), [(9464, "0", 9464), (9464, "3", 9467), (9464, "abc", 9464)])
    def test_resolve_port(self, base, no, expected):
        assert executor_metrics.resolve_port(base, no) == expected


class TestExposition:
    def test_metrics_appear_in_exposition(self):
        executor_metrics.set_queue_stats(1, 1)
        payload = generate_latest().decode()

        assert "multirag_task_executor_pending_tasks" in payload
        assert "multirag_task_executor_task_duration_seconds_bucket" in payload
