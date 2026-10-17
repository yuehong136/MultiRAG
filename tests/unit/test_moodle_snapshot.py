from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source.exceptions import ConnectorValidationError
from common.data_source.moodle_connector import MoodleConnector
from common.markdown_utils import html_to_markdown
from tests.support.moodle_source import MoodleSource


def connector(mode: str = "complete") -> tuple[MoodleConnector, MoodleSource]:
    source = MoodleSource(mode)
    result = MoodleConnector("https://moodle.invalid", batch_size=1)
    result.moodle_client = source  # type: ignore[assignment]
    return result, source


def flatten(batches: Any) -> list[Any]:
    return [item for batch in batches for item in batch]


def test_all_courses_same_ids_and_no_slim_download(monkeypatch: pytest.MonkeyPatch) -> None:
    current, source = connector()
    monkeypatch.setattr("common.data_source.moodle_connector.rl_requests.get", lambda *args, **kwargs: pytest.fail("unexpected download"))
    slim = flatten(current.retrieve_all_slim_docs_perm_sync())
    full = flatten(current.load_from_state())
    assert [item.id for item in slim] == [item.id for item in full] == ["moodle_assign_11", "moodle_quiz_12"]
    assert b"First Last" in full[0].blob
    assert {params["courseid"] for name, params in source.calls if name == "core_course_get_contents"} == {1, 2}


@pytest.mark.parametrize("mode", ["listing-error", "null", "course-error", "malformed"])
@pytest.mark.parametrize("method", ["retrieve_all_slim_docs_perm_sync", "load_from_state"])
def test_incomplete_never_success(mode: str, method: str) -> None:
    current, _ = connector(mode)
    with pytest.raises(ConnectorValidationError) as caught:
        list(getattr(current, method)())
    assert "synthetic-secret" not in str(caught.value)


def test_successful_empty() -> None:
    current, _ = connector("empty")
    assert list(current.load_from_state()) == list(current.retrieve_all_slim_docs_perm_sync()) == []


def test_all_supported_eligibility_and_same_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    current, source = connector()
    content = {"filename": "index.html", "fileurl": "https://moodle.invalid/file", "filesize": 4}
    source.modules = [
        {"id": 1, "name": "Resource", "modname": "resource", "contents": [content]},
        {"id": 2, "name": "Page", "modname": "page", "contents": [content]},
        {"id": 3, "name": "Book", "modname": "book", "contents": [content]},
        {"id": 4, "name": "Forum", "modname": "forum", "instance": 40},
        {"id": 5, "name": "Empty", "modname": "assign", "description": ""},
        {"id": 6, "name": "Empty", "modname": "page", "contents": []},
        {"id": 7, "name": "URL", "modname": "url"},
        {"id": 8, "name": "Empty", "modname": "resource", "contents": [{**content, "filesize": 0}]},
    ]
    downloads: list[str] = []

    def get(url: str, **kwargs: Any) -> Any:
        downloads.append(url)
        return SimpleNamespace(content=b"body", raise_for_status=lambda: None)

    monkeypatch.setattr("common.data_source.moodle_connector.rl_requests.get", get)
    slim = flatten(current.retrieve_all_slim_docs_perm_sync())
    assert not downloads
    full = flatten(current.load_from_state())
    assert [item.id for item in full] == [item.id for item in slim] == ["moodle_resource_1", "moodle_page_2", "moodle_book_3", "moodle_forum_4"]
    assert len(downloads) == 3
    assert all(params == {"forumid": 40, "page": -1, "perpage": 0} for name, params in source.calls if name.startswith("mod_forum"))


@pytest.mark.parametrize("modname", ["resource", "page", "book"])
def test_file_failure_propagates(modname: str, monkeypatch: pytest.MonkeyPatch) -> None:
    current, source = connector()
    source.modules = [{"id": 1, "name": "File", "modname": modname, "contents": [{"filename": "index.html", "fileurl": "https://moodle.invalid/file"}]}]

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("https://moodle.invalid/?token=synthetic-secret")

    monkeypatch.setattr("common.data_source.moodle_connector.rl_requests.get", fail)
    assert flatten(current.retrieve_all_slim_docs_perm_sync())
    with pytest.raises(ConnectorValidationError, match="module read failed") as caught:
        flatten(current.load_from_state())
    assert "synthetic-secret" not in str(caught.value)


def test_forum_warning_not_authoritative() -> None:
    current, source = connector("forum-warning")
    source.modules = [{"id": 1, "modname": "forum", "instance": 40}]
    with pytest.raises(ConnectorValidationError, match="incomplete"):
        flatten(current.retrieve_all_slim_docs_perm_sync())


def test_poll_boundary_and_forum_update() -> None:
    current, source = connector()
    assert not flatten(current.poll_source(100, 200))
    assert len(flatten(current.poll_source(99, 100))) == 2
    assert not flatten(current.poll_source(0, 99))
    source.modules = [{"id": 1, "name": "Forum", "modname": "forum", "instance": 40, "timemodified": 50}]
    assert len(flatten(current.poll_source(100, 150))) == 1
    assert not flatten(current.poll_source(150, 200))


@pytest.mark.parametrize("html", ["First<b> </b>Last", "First<em> </em>Last", "First<code> </code>Last"])
def test_styled_space(html: str) -> None:
    assert html_to_markdown(html) == "First Last"


@pytest.mark.parametrize("batch_size", [0, -1, True, "10"])
def test_invalid_batch(batch_size: Any) -> None:
    with pytest.raises(ConnectorValidationError):
        MoodleConnector("https://moodle.invalid", batch_size=batch_size)


def test_tokens_replaced_and_external_download_rejected() -> None:
    current, _ = connector()
    assert current._add_token_to_url("https://moodle.invalid/file?token=old&x=a#f") == "https://moodle.invalid/file?x=a&token=synthetic-secret#f"
    with pytest.raises(ConnectorValidationError):
        current._add_token_to_url("https://external.invalid/file")
