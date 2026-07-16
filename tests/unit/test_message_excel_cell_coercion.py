from agent.component.message import Message


def _parse(lines: list[str]):
    return Message._parse_markdown_table_lines(object.__new__(Message), lines)


def test_numeric_cells_coerced_to_native_types():
    df = _parse(["| name | count | ratio |", "| foo | 1,234 | 0.5 |", "| bar | -7 | 1.5e-3 |"])
    assert df["count"].tolist() == [1234, -7]
    assert df["ratio"].tolist() == [0.5, 0.0015]
    assert df["name"].tolist() == ["foo", "bar"]


def test_leading_zeros_and_non_numeric_stay_text():
    df = _parse(["| code | note |", "| 00123 | v1.2.3 |"])
    assert df["code"].tolist() == ["00123"]
    assert df["note"].tolist() == ["v1.2.3"]


def test_headers_are_not_coerced():
    df = _parse(["| 2024 | 2025 |", "| 1 | 2 |"])
    assert list(df.columns) == ["2024", "2025"]
    assert df["2024"].tolist() == [1]
