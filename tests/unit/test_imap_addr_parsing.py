"""RFC address handling and privacy for singular IMAP sender selection."""

import logging

import pytest

from common.data_source.imap_connector import _parse_addrs, _parse_singular_addr


@pytest.mark.parametrize(
    ("header", "addresses"),
    [
        ("", []),
        ("user@example.com", [("", "user@example.com")]),
        ("Alice <alice@example.com>", [("Alice", "alice@example.com")]),
        ('"Schlüter, Sabine" <sabine@example.com>', [("Schlüter, Sabine", "sabine@example.com")]),
        ('"Wilkens, Michael" <m@example.com>, "Müller, Hans" <h@example.com>', [("Wilkens, Michael", "m@example.com"), ("Müller, Hans", "h@example.com")]),
    ],
)
def test_address_parsing(header: str, addresses: list[tuple[str, str]], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert _parse_addrs(header) == addresses
        assert _parse_singular_addr(header) == (addresses[0] if addresses else ("Unknown", "unknown@example.com"))
    if len(addresses) > 1:
        assert len(caplog.records) == 1
        assert caplog.records[0].message == "Multiple addresses in header expected to be singular; using first. parsed_count=2"
        assert header not in caplog.text
        for name, address in addresses:
            assert name not in caplog.text and address not in caplog.text
    else:
        assert not caplog.records
