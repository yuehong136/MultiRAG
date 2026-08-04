"""实体消解的 merge 阶段必须完全串行。

回归钉板：merge 协程改的是同一张 networkx 图，``_merge_graph_nodes`` 遍历邻居的
过程中还会 await——一旦放并发，另一个协程就可能在让出点改动邻接结构，触发
``RuntimeError: dictionary keys changed during iteration``。候选消解阶段的
semaphore 只限并发数（5）不互斥，套在 merge 上等于没锁。
"""

import asyncio

import networkx as nx
import pytest

from core.graphrag.entity_resolution import EntityResolution


class _UnusedLLM:
    """满足 GraphRAGCompletionLLM 契约；merge 与候选消解都打了桩，不该被碰到。"""

    llm_name = "unused"
    max_length = 1024

    async def async_chat(self, system, history, gen_conf=None, **kwargs):  # pragma: no cover - 被碰到就是测试写错了
        raise AssertionError("entity resolution should not call the LLM in this test")


@pytest.fixture
def graph():
    g = nx.Graph()
    for name in ("alice1", "alice2", "bob1", "bob2"):
        g.add_node(name, entity_type="PERSON")
    return g


@pytest.fixture
def merge_probe(monkeypatch):
    """把候选消解替成固定结果，并记录 merge 的并发峰值。"""
    monkeypatch.setattr(EntityResolution, "is_similarity", lambda self, a, b: True)

    async def _resolve_candidate(self, candidate_batch, result_set, result_lock, task_id=""):
        async with result_lock:
            # 两条互不相连的边 → 两个连通分量 → 两个并行的 merge 任务
            result_set.update({("alice1", "alice2"), ("bob1", "bob2")})

    monkeypatch.setattr(EntityResolution, "_resolve_candidate", _resolve_candidate)

    state = {"active": 0, "peak": 0, "merged": []}

    async def _merge_graph_nodes(self, graph, nodes, change, task_id=""):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        state["merged"].append(sorted(nodes))
        # 让出事件循环：没有互斥的话，另一个 merge 会在这里挤进来
        await asyncio.sleep(0)
        state["active"] -= 1

    monkeypatch.setattr(EntityResolution, "_merge_graph_nodes", _merge_graph_nodes)
    return state


async def test_merge_phase_runs_one_at_a_time(graph, merge_probe):
    resolver = EntityResolution(_UnusedLLM())

    await resolver(graph, {"alice1", "bob1"}, callback=lambda **kwargs: None)

    assert len(merge_probe["merged"]) == 2, "两个连通分量应各触发一次 merge"
    assert merge_probe["peak"] == 1, f"merge 阶段出现并发（峰值 {merge_probe['peak']}），共享图会被并发改坏"
