from types import SimpleNamespace
from typing import Any

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from httplib2 import Response

from common.data_source.google_drive import connector as drive_module
from common.data_source.google_drive.constant import DRIVE_FOLDER_TYPE
from common.data_source.google_drive.doc_conversion import onyx_document_id_from_drive_file
from common.data_source.google_drive.file_retrieval import DriveFileFieldType
from common.data_source.google_drive.model import DriveRetrievalStage, StageCompletion
from common.data_source.interfaces import collect_slim_document_snapshot


def file(identifier: str) -> dict[str, Any]:
    return {"id": identifier, "mimeType": "text/plain", "name": identifier, "webViewLink": f"https://drive.google.com/file/d/{identifier}/view"}


class DriveAPI:
    def __init__(self, pages: list[dict[str, Any] | Exception]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []
        self.get_error: Exception | None = None

    def files(self) -> "DriveAPI":
        return self

    def drives(self) -> Any:
        return SimpleNamespace(list=lambda **kwargs: SimpleNamespace(execute=lambda: {"kind": "drive#driveList", "drives": []}))

    def list(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        def execute() -> dict[str, Any]:
            item = self.pages[int(kwargs.get("pageToken", "0"))]
            if isinstance(item, Exception):
                raise item
            return item

        return SimpleNamespace(execute=execute)

    def get(self, **kwargs: Any) -> Any:
        def execute() -> dict[str, Any]:
            if self.get_error:
                raise self.get_error
            return {"id": kwargs["fileId"], "mimeType": DRIVE_FOLDER_TYPE}

        return SimpleNamespace(execute=execute)


def make_connector(monkeypatch: pytest.MonkeyPatch, api: DriveAPI, **kwargs: Any) -> drive_module.GoogleDriveConnector:
    value = drive_module.GoogleDriveConnector(**(kwargs or {"include_my_drives": True, "include_files_shared_with_me": True, "include_shared_drives": True}))
    value._creds = Credentials("test-token")
    value._primary_admin_email = "user@example.test"
    monkeypatch.setattr(drive_module, "get_drive_service", lambda *args: api)
    return value


def test_identity_only_snapshot_exhausts_multiple_checkpoints_and_restores_content_state(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [file(str(i)) for i in range(5)]
    api = DriveAPI([{"kind": "drive#fileList", "files": [item], **({"nextPageToken": str(i + 1)} if i < 4 else {})} for i, item in enumerate(files)])
    value = make_connector(monkeypatch, api)
    value._retrieved_folder_and_drive_ids = {"previous-content-parent"}
    assert [doc.id for doc in collect_slim_document_snapshot(value)] == [onyx_document_id_from_drive_file(item) for item in files]
    assert len(api.calls) == 5
    assert value._retrieved_folder_and_drive_ids == {"previous-content-parent"}
    assert not value._deletion_snapshot
    for call in api.calls:
        assert call["corpora"] == "allDrives" and call["includeItemsFromAllDrives"]
        assert "permissions" not in call["fields"] and "owners" not in call["fields"]
        assert "modifiedTime" not in call["q"] and "createdTime" not in call["q"]


@pytest.mark.parametrize(
    "last",
    [{"kind": "drive#fileList", "files": [], "incompleteSearch": True}, HttpError(Response({"status": "403"}), b'{"error":{"message":"denied"}}'), {}, {"kind": "drive#fileList", "files": [file("")]}],
)
def test_partial_permission_incomplete_and_invalid_pages_cannot_publish(monkeypatch: pytest.MonkeyPatch, last: dict[str, Any] | Exception) -> None:
    api = DriveAPI([{"kind": "drive#fileList", "files": [file("existing")], "nextPageToken": "1"}, last])
    value = make_connector(monkeypatch, api)
    with pytest.raises((RuntimeError, ValueError, KeyError, HttpError)):
        collect_slim_document_snapshot(value)
    assert not value._deletion_snapshot and value._retrieved_folder_and_drive_ids == set()


def test_complete_empty_drive_is_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_connector(monkeypatch, DriveAPI([{"kind": "drive#fileList"}]))
    assert collect_slim_document_snapshot(value) == ()


def test_requested_folder_permission_failure_does_not_become_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    api = DriveAPI([])
    api.get_error = HttpError(Response({"status": "404"}), b'{"error":{"message":"folder hidden"}}')
    value = make_connector(monkeypatch, api, shared_folder_urls="https://drive.google.com/drive/folders/private")
    with pytest.raises(HttpError):
        collect_slim_document_snapshot(value)


def test_service_account_user_unauthorized_aborts_deletion_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    api = DriveAPI([])
    api.get_error = HttpError(Response({"status": "401"}), b'{"error":{"message":"user disabled"}}')
    value = make_connector(monkeypatch, api)
    checkpoint = value.build_dummy_checkpoint()
    checkpoint.completion_map["user@example.test"] = StageCompletion(stage=DriveRetrievalStage.START, completed_until=0)
    with pytest.raises(HttpError):
        list(value._impersonate_user_for_retrieval("user@example.test", DriveFileFieldType.DELETION, checkpoint, lambda user: None, []))


def test_selected_shared_drive_exhausts_pages_including_empty_final_page(monkeypatch: pytest.MonkeyPatch) -> None:
    api = DriveAPI([{"kind": "drive#fileList", "files": [file(str(i))], "nextPageToken": str(i + 1)} for i in range(3)] + [{"kind": "drive#fileList"}])
    value = make_connector(monkeypatch, api, shared_drive_urls="https://drive.google.com/drive/folders/team-drive")
    monkeypatch.setattr(value, "_get_all_drives_for_user", lambda email: {"team-drive"})
    assert len(collect_slim_document_snapshot(value)) == 3
    assert len(api.calls) == 4
    assert all(call.get("corpora") == "drive" and call.get("driveId") == "team-drive" for call in api.calls)


def test_successful_empty_requested_folder_is_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    api = DriveAPI([{"kind": "drive#fileList"}])
    value = make_connector(monkeypatch, api, shared_folder_urls="https://drive.google.com/drive/folders/folder")
    assert collect_slim_document_snapshot(value) == ()
    assert len(api.calls) == 2  # files plus recursive subfolder enumeration
    assert all("permissions" not in call["fields"] for call in api.calls)


def test_oauth_user_email_scope_does_not_silently_complete_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_connector(monkeypatch, DriveAPI([]), my_drive_emails="user@example.test")
    with pytest.raises(ValueError, match="service account"):
        collect_slim_document_snapshot(value)


@pytest.mark.parametrize("list_key,kind,identity", [("drives", "drive#driveList", "id"), ("users", "admin#directory#users", "primaryEmail")])
def test_discovery_collections_validate_kind_and_identity(list_key: str, kind: str, identity: str) -> None:
    from common.data_source.google_util.util import execute_paginated_retrieval

    for page in [{}, {"kind": kind, list_key: [None]}, {"kind": kind, list_key: [{identity: ""}]}]:
        with pytest.raises(ValueError):
            list(execute_paginated_retrieval(lambda **kwargs: SimpleNamespace(execute=lambda: page), list_key=list_key, fields="kind,nextPageToken", require_complete=True))
    assert list(execute_paginated_retrieval(lambda **kwargs: SimpleNamespace(execute=lambda: {"kind": kind}), list_key=list_key, fields="kind,nextPageToken", require_complete=True)) == []


def test_drive_discovery_requests_kind_in_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    api = DriveAPI([])
    value = make_connector(monkeypatch, api)
    value._deletion_snapshot = True
    calls: list[dict[str, Any]] = []

    def listing(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(execute=lambda: {"kind": "drive#driveList"})

    monkeypatch.setattr(api, "drives", lambda: SimpleNamespace(list=listing))
    assert value._get_all_drives_for_user(value.primary_admin_email) == set()
    assert "kind" in calls[0]["fields"]


def test_user_discovery_requests_kind_and_cannot_accept_malformed_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_connector(monkeypatch, DriveAPI([]))
    value._deletion_snapshot = True
    monkeypatch.setattr(drive_module, "OAuthCredentials", type("UnusedOAuth", (), {}))
    calls: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = [{"kind": "admin#directory#users"}, {}]

    def listing(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(execute=lambda: pages[len(calls) - 1])

    monkeypatch.setattr(drive_module, "get_admin_service", lambda **kwargs: SimpleNamespace(users=lambda: SimpleNamespace(list=listing)))
    with pytest.raises(ValueError):
        value._get_all_user_emails()
    assert len(calls) == 2 and all("kind" in call["fields"] for call in calls)


def test_missing_file_type_cannot_change_fallback_document_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    value = make_connector(monkeypatch, DriveAPI([{"kind": "drive#fileList", "files": [{"id": "native-document"}]}]))
    with pytest.raises(ValueError, match="identity"):
        collect_slim_document_snapshot(value)


@pytest.mark.parametrize("details", [None, {}, {"targetId": "target"}])
def test_incomplete_folder_shortcuts_abort_inventory(monkeypatch: pytest.MonkeyPatch, details: dict[str, Any] | None) -> None:
    from common.data_source.google_drive.constant import DRIVE_SHORTCUT_TYPE

    shortcut = {"id": "shortcut", "mimeType": DRIVE_SHORTCUT_TYPE, "name": "Shortcut", "shortcutDetails": details}
    value = make_connector(monkeypatch, DriveAPI([{"kind": "drive#fileList", "files": [shortcut]}]), shared_folder_urls="https://drive.google.com/drive/folders/folder")
    with pytest.raises(ValueError, match="shortcut"):
        collect_slim_document_snapshot(value)
