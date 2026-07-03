import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def asana_module(monkeypatch):
    data_source_pkg = types.ModuleType("common.data_source")
    data_source_pkg.__path__ = [str(Path(__file__).parents[2] / "common" / "data_source")]
    monkeypatch.setitem(sys.modules, "common.data_source", data_source_pkg)

    config = types.ModuleType("common.data_source.config")
    config.CONTINUE_ON_CONNECTOR_FAILURE = True
    config.INDEX_BATCH_SIZE = 10
    config.DocumentSource = types.SimpleNamespace(ASANA="asana")
    monkeypatch.setitem(sys.modules, "common.data_source.config", config)

    interfaces = types.ModuleType("common.data_source.interfaces")
    interfaces.LoadConnector = type("LoadConnector", (), {})
    interfaces.PollConnector = type("PollConnector", (), {})
    monkeypatch.setitem(sys.modules, "common.data_source.interfaces", interfaces)

    models = types.ModuleType("common.data_source.models")
    models.Document = type("Document", (), {})
    models.GenerateDocumentsOutput = object
    models.SecondsSinceUnixEpoch = int
    monkeypatch.setitem(sys.modules, "common.data_source.models", models)

    utils = types.ModuleType("common.data_source.utils")
    utils.extract_size_bytes = lambda attachment: attachment.get("size")
    utils.get_file_ext = lambda filename: filename.rsplit(".", 1)[-1] if "." in filename else ""
    monkeypatch.setitem(sys.modules, "common.data_source.utils", utils)

    module_path = Path(__file__).parents[2] / "common" / "data_source" / "asana_connector.py"
    spec = importlib.util.spec_from_file_location("asana_connector_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeUsersAPI:
    def get_users(self, opts):
        return [
            {"gid": "user-1", "email": "workspace-1@example.com"},
            {"gid": "user-2", "email": "workspace-2@example.com"},
            {"gid": "user-3"},
        ]


class FakeProjectsAPI:
    def __init__(self):
        self.calls = []

    def get_project(self, project_gid, opts):
        self.calls.append((project_gid, opts))
        projects = {
            "project-1": {"privacy_setting": "private", "team": {"gid": "team-1"}},
            "project-2": {"privacy_setting": "public"},
        }
        return projects[project_gid]


class FakeProjectMembershipsAPI:
    def __init__(self):
        self.project_calls = []

    def get_project_membership(self, *args, **kwargs):
        raise AssertionError("project gid must not be passed to get_project_membership")

    def get_project_memberships_for_project(self, project_gid, opts):
        self.project_calls.append((project_gid, opts))
        memberships = {
            "project-1": [
                {"user": {"email": "project-1@example.com"}},
                {"user": None},
            ],
            "project-2": [
                {"user": {"email": "project-2@example.com"}},
                {},
            ],
        }
        return memberships[project_gid]


def make_asana_api(asana_module):
    api = asana_module.AsanaAPI.__new__(asana_module.AsanaAPI)
    api.users_api = FakeUsersAPI()
    api.project_api = FakeProjectsAPI()
    api.project_memberships_api = FakeProjectMembershipsAPI()
    return api


def test_get_accessible_emails_uses_project_memberships_for_project(asana_module):
    api = make_asana_api(asana_module)

    emails = api.get_accessible_emails(
        workspace_id="workspace-1",
        project_ids=[" project-1 ", "", "project-2"],
        team_id="team-1",
    )

    assert emails == {"project-1@example.com", "project-2@example.com"}
    assert [call[0] for call in api.project_api.calls] == ["project-1", "project-2"]
    assert [call[0] for call in api.project_memberships_api.project_calls] == ["project-1", "project-2"]


def test_get_accessible_emails_without_projects_returns_workspace_emails(asana_module):
    api = make_asana_api(asana_module)

    emails = api.get_accessible_emails(
        workspace_id="workspace-1",
        project_ids=None,
        team_id=None,
    )

    assert emails == {"workspace-1@example.com", "workspace-2@example.com"}
    assert api.project_api.calls == []
    assert api.project_memberships_api.project_calls == []


def test_connector_normalizes_project_ids_and_team_id(asana_module):
    connector = asana_module.AsanaConnector(
        asana_workspace_id="workspace-1",
        asana_project_ids=" project-1, ,project-2 ",
        asana_team_id=" team-1 ",
    )

    assert connector.project_ids_to_index == ["project-1", "project-2"]
    assert connector.asana_team_id == "team-1"
