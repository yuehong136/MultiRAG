from common.string_utils import split_and_sanitize_terms, truncate_utf8_bytes


def test_split_and_sanitize_terms_supports_chinese_delimiters() -> None:
    raw = "多轮对话管理，对话系统；自然语言处理,上下文理解\n对话状态跟踪"
    terms = split_and_sanitize_terms(raw, max_term_bytes=256, max_terms=20)
    assert terms == ["多轮对话管理", "对话系统", "自然语言处理", "上下文理解", "对话状态跟踪"]


def test_truncate_utf8_bytes_works_for_multibyte_text() -> None:
    text = "你" * 200
    out = truncate_utf8_bytes(text, 256)
    assert len(out.encode("utf-8")) <= 256
    assert out


def test_split_and_sanitize_terms_dedup_and_max_terms() -> None:
    raw = "A, A, B, C, D"
    terms = split_and_sanitize_terms(raw, max_terms=3)
    assert terms == ["A", "B", "C"]
