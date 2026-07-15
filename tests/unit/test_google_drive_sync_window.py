"""Google Drive 增量同步窗口：时间过滤子句与 start 回拨缓冲。"""

from common.data_source.google_drive.connector import GoogleDriveConnector
from common.data_source.google_drive.file_retrieval import generate_time_range_filter


def test_time_range_filter_matches_created_time_alongside_modified_time():
    clause = generate_time_range_filter(start=1_700_000_000)
    assert "modifiedTime > '" in clause
    assert "or createdTime >= '" in clause
    assert clause.count("(") == clause.count(")") == 1


def test_time_range_filter_end_only_unchanged():
    clause = generate_time_range_filter(end=1_700_000_000)
    assert "createdTime" not in clause
    assert "modifiedTime <= '" in clause


def _connector(buffer_seconds: int) -> GoogleDriveConnector:
    return GoogleDriveConnector(include_my_drives=True, time_buffer_seconds=buffer_seconds)


def test_adjust_start_subtracts_buffer_and_clamps_at_zero():
    connector = _connector(3600)
    assert connector._adjust_start_for_query(10_000) == 6_400
    assert connector._adjust_start_for_query(100) == 0.0


def test_adjust_start_passthrough_cases():
    connector = _connector(3600)
    assert connector._adjust_start_for_query(None) is None
    assert connector._adjust_start_for_query(0) == 0
    assert _connector(0)._adjust_start_for_query(10_000) == 10_000
    # 负值缓冲在构造时被钳为 0
    assert _connector(-5)._adjust_start_for_query(10_000) == 10_000
