"""钉板：_rerank_window 的窗口不变量。

窗口值同时充当后端块大小（global_offset // window）与页内切片模数
（global_offset % window），必须是 page_size 的整数倍，否则跨块深分页
会静默丢结果、返回短页。
"""

from core.nlp.search import Dealer


def test_window_is_exact_multiple_of_page_size():
    for page_size in [2, 3, 6, 10, 16, 30, 64, 100]:
        for top in [0, 5, 10, 64, 1024]:
            window = Dealer._rerank_window(page_size, top)
            assert window % page_size == 0, f"page_size={page_size}, top={top}, window={window}"
            assert window >= page_size


def test_no_reranker_keeps_historical_window():
    assert Dealer._rerank_window(10) == 70
    assert Dealer._rerank_window(30) == 90
    assert Dealer._rerank_window(64) == 64


def test_reranker_bounds_window_by_top_rounded_to_page():
    # top=10, page_size=6 → 12（top 向上取整到页倍数），而非 64 截断
    assert Dealer._rerank_window(6, 10) == 12
    # 大 top 不缩窗口：保持 ~64 候选池的页对齐值
    assert Dealer._rerank_window(10, 1024) == 70
    assert Dealer._rerank_window(30, 1024) == 90


def test_page_size_one_or_less():
    assert Dealer._rerank_window(1) == 30
    assert Dealer._rerank_window(1, 10) == 10
    assert Dealer._rerank_window(1, 1024) == 30


def test_cross_block_pagination_no_gaps_no_short_pages():
    # 模拟 retrieval 的块取数 + 页内切片：候选连续编号，逐页翻完必须
    # 每个候选恰好出现一次、顺序不乱、无短页（除末页）。
    page_size, top, total = 10, 1024, 200
    window = Dealer._rerank_window(page_size, top)
    seen: list[int] = []
    for page in range(1, total // page_size + 1):
        global_offset = (page - 1) * page_size
        block_start = (global_offset // window) * window
        block = list(range(block_start, min(block_start + window, total)))
        begin = global_offset % window
        page_items = block[begin : begin + page_size]
        assert len(page_items) == page_size
        seen.extend(page_items)
    assert seen == list(range(total))
