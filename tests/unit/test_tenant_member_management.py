import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy.orm import Session


class _ExprField:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return (self.name, other)


def _load_tenant_module(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]

    api_pkg = ModuleType("api")
    api_pkg.__path__ = [str(repo_root / "api")]
    monkeypatch.setitem(sys.modules, "api", api_pkg)

    apps_mod = ModuleType("api.apps")
    apps_mod.__path__ = [str(repo_root / "api" / "apps")]
    apps_mod.manager = lambda: None
    monkeypatch.setitem(sys.modules, "api.apps", apps_mod)

    db_mod = ModuleType("api.db")
    db_mod.UserTenantRole = SimpleNamespace(
        OWNER="owner",
        ADMIN="admin",
        NORMAL="normal",
        INVITE="invite",
    )
    monkeypatch.setitem(sys.modules, "api.db", db_mod)

    db_models_mod = ModuleType("api.db.db_models")
    db_models_mod.UserTenant = type(
        "UserTenant",
        (),
        {
            "id": _ExprField("id"),
            "tenant_id": _ExprField("tenant_id"),
            "user_id": _ExprField("user_id"),
        },
    )
    db_models_mod.TenantLLM = type("TenantLLM", (), {"tenant_id": _ExprField("tenant_id")})
    db_models_mod.Tenant = type("Tenant", (), {"id": _ExprField("id")})
    db_models_mod.File = type("File", (), {"tenant_id": _ExprField("tenant_id")})
    db_models_mod.User = type("User", (), {"id": _ExprField("id")})
    db_models_mod.get_db = lambda: None
    monkeypatch.setitem(sys.modules, "api.db.db_models", db_models_mod)

    services_pkg = ModuleType("api.db.services")
    services_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "api.db.services", services_pkg)

    file_service_mod = ModuleType("api.db.services.file_service")

    class _FileService:
        @staticmethod
        def filter_delete(*_args, **_kwargs):
            return True

    file_service_mod.FileService = _FileService
    monkeypatch.setitem(sys.modules, "api.db.services.file_service", file_service_mod)

    tenant_llm_service_mod = ModuleType("api.db.services.tenant_llm_service")

    class _TenantLLMService:
        @staticmethod
        def filter_delete(*_args, **_kwargs):
            return True

    tenant_llm_service_mod.TenantLLMService = _TenantLLMService
    monkeypatch.setitem(sys.modules, "api.db.services.tenant_llm_service", tenant_llm_service_mod)

    user_service_mod = ModuleType("api.db.services.user_service")

    class _UserTenantService:
        @staticmethod
        def get_tenants_by_user_id(*_args, **_kwargs):
            return []

        @staticmethod
        def get_by_tenant_id(*_args, **_kwargs):
            return []

        @staticmethod
        def get_membership(*_args, **_kwargs):
            return None

        @staticmethod
        def can_manage_members(role):
            return role in {"owner", "admin"}

        @staticmethod
        def can_manage_roles(role):
            return role == "owner"

        @staticmethod
        def save(*_args, **_kwargs):
            return True

        @staticmethod
        def filter_delete(*_args, **_kwargs):
            return True

        @staticmethod
        def filter_update(*_args, **_kwargs):
            return True

    class _UserService:
        @staticmethod
        def query(*_args, **_kwargs):
            return []

        @staticmethod
        def get_by_id(*_args, **_kwargs):
            return None

        @staticmethod
        def filter_delete(*_args, **_kwargs):
            return True

    class _TenantService:
        @staticmethod
        def filter_delete(*_args, **_kwargs):
            return True

    user_service_mod.UserTenantService = _UserTenantService
    user_service_mod.UserService = _UserService
    user_service_mod.TenantService = _TenantService
    monkeypatch.setitem(sys.modules, "api.db.services.user_service", user_service_mod)

    api_utils_mod = ModuleType("api.utils.api_utils")

    class _StubBusinessError(Exception):
        """镜像 api.utils.api_utils.BusinessError（生产环境由全局 handler 转 json）。"""

        def __init__(self, retmsg, retcode=None, data=False):
            super().__init__(retmsg)
            self.retmsg = retmsg
            self.retcode = retcode
            self.data = data

    api_utils_mod.BusinessError = _StubBusinessError
    api_utils_mod.get_json_result = lambda retcode=0, retmsg="success", data=None: {
        "retcode": retcode,
        "retmsg": retmsg,
        "data": data,
    }
    api_utils_mod.get_data_error_result = lambda retcode=102, retmsg="Sorry! Data missing!": {
        "retcode": retcode,
        "retmsg": retmsg,
    }
    api_utils_mod.server_error_response = lambda exc: {
        "retcode": 500,
        "retmsg": repr(exc),
        "data": False,
    }
    monkeypatch.setitem(sys.modules, "api.utils.api_utils", api_utils_mod)

    web_utils_mod = ModuleType("api.utils.web_utils")

    async def _send_invite_email(**_kwargs):
        return True

    web_utils_mod.send_invite_email = _send_invite_email
    monkeypatch.setitem(sys.modules, "api.utils.web_utils", web_utils_mod)

    common_pkg = ModuleType("common")
    common_pkg.__path__ = [str(repo_root / "common")]
    monkeypatch.setitem(sys.modules, "common", common_pkg)

    constants_mod = ModuleType("common.constants")
    constants_mod.RetCode = SimpleNamespace(
        SUCCESS=0,
        AUTHENTICATION_ERROR=401,
        SERVER_ERROR=500,
        DATA_ERROR=102,
    )
    constants_mod.StatusEnum = SimpleNamespace(VALID=SimpleNamespace(value=1))
    monkeypatch.setitem(sys.modules, "common.constants", constants_mod)

    misc_utils_mod = ModuleType("common.misc_utils")
    misc_utils_mod.get_uuid = lambda: "uuid-1"
    monkeypatch.setitem(sys.modules, "common.misc_utils", misc_utils_mod)

    time_utils_mod = ModuleType("common.time_utils")
    time_utils_mod.delta_seconds = lambda _value: 0
    monkeypatch.setitem(sys.modules, "common.time_utils", time_utils_mod)

    settings_mod = ModuleType("common.settings")
    settings_mod.MAIL_FRONTEND_URL = "https://frontend.example/invite"
    monkeypatch.setitem(sys.modules, "common.settings", settings_mod)
    common_pkg.settings = settings_mod

    sys.modules.pop("test_tenant_member_management_module", None)
    module_path = repo_root / "api" / "apps" / "tenant_app.py"
    spec = importlib.util.spec_from_file_location("test_tenant_member_management_module", module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "test_tenant_member_management_module", module)
    spec.loader.exec_module(module)
    return module


def _membership(module, user_id: str, role: str, tenant_id: str = "tenant-1", membership_id: str = "membership-1"):
    return SimpleNamespace(id=membership_id, tenant_id=tenant_id, user_id=user_id, role=role)


def _user(user_id: str, email: str, nickname: str):
    return SimpleNamespace(id=user_id, email=email, nickname=nickname, avatar="avatar-url")


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeScalarSession(Session):
    def __init__(self, scalar_value):
        super().__init__()
        self.scalar_value = scalar_value
        self.last_stmt = None

    def execute(self, stmt):
        self.last_stmt = stmt
        return _ScalarResult(self.scalar_value)


def test_user_list_allows_admin_and_denies_normal(monkeypatch):
    module = _load_tenant_module(monkeypatch)

    admin_user = SimpleNamespace(id="admin-1", email="admin@example.com")
    normal_user = SimpleNamespace(id="normal-1", email="normal@example.com")

    monkeypatch.setattr(
        module.UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: _membership(
            module,
            user_id=user_id,
            role=module.UserTenantRole.ADMIN if user_id == admin_user.id else module.UserTenantRole.NORMAL,
            tenant_id=tenant_id,
        ),
    )
    monkeypatch.setattr(
        module.UserTenantService,
        "get_by_tenant_id",
        lambda _db, _tenant_id: [{"user_id": "member-1", "update_date": "2024-01-01 00:00:00"}],
    )
    monkeypatch.setattr(module, "delta_seconds", lambda _value: 42)

    # 拒绝：normal 角色过不了 require_member_manager 依赖（生产环境由全局 handler 转 json）
    normal_membership = module.UserTenantService.get_membership(None, tenant_id="tenant-1", user_id=normal_user.id)
    with pytest.raises(module.BusinessError) as denied:
        module.require_member_manager(normal_membership)
    assert denied.value.retcode == module.RetCode.AUTHENTICATION_ERROR
    assert denied.value.retmsg == "No authorization."

    # 放行：admin 通过依赖后路由正常返回
    admin_membership = module.require_member_manager(module.UserTenantService.get_membership(None, tenant_id="tenant-1", user_id=admin_user.id))
    ok_res = module.user_list("tenant-1", db=None, _membership=admin_membership)
    assert ok_res["retcode"] == 0, ok_res
    assert ok_res["data"][0]["delta_seconds"] == 42, ok_res


def test_single_invite_allows_admin_and_is_best_effort_on_email(monkeypatch):
    module = _load_tenant_module(monkeypatch)

    actor = SimpleNamespace(id="admin-1", email="admin@example.com")
    invitee = _user("invitee-1", "invitee@example.com", "Invitee")
    inviter = _user("admin-1", "admin@example.com", "Admin Inviter")
    saved = []

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == actor.id:
            return _membership(module, user_id=actor.id, role=module.UserTenantRole.ADMIN, tenant_id=tenant_id)
        if user_id == invitee.id:
            return None
        return None

    def fake_get_by_id(_db, user_id):
        if user_id == actor.id:
            return inviter
        if user_id == invitee.id:
            return invitee
        return None

    monkeypatch.setattr(module.UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(module.UserTenantService, "save", lambda *args, **kwargs: saved.append(kwargs) or True)
    monkeypatch.setattr(module.UserService, "query", lambda _db, email: [invitee] if email == invitee.email else [])
    monkeypatch.setattr(module.UserService, "get_by_id", fake_get_by_id)

    background_tasks = module.BackgroundTasks()
    res = module.create(
        "tenant-1",
        request_body=module.InviteUserRequest(email=invitee.email),
        background_tasks=background_tasks,
        db=None,
        user=actor,
        _membership=_membership(module, user_id=actor.id, role=module.UserTenantRole.ADMIN),
    )

    assert res["retcode"] == 0, res
    assert res["data"]["id"] == invitee.id, res
    assert saved and saved[-1]["role"] == module.UserTenantRole.INVITE, saved
    # 邮件只注册为后台任务（响应后执行，天然 best-effort）
    assert len(background_tasks.tasks) == 1, background_tasks.tasks


def test_batch_invite_returns_statuses_and_summary(monkeypatch):
    module = _load_tenant_module(monkeypatch)

    actor = SimpleNamespace(id="owner-1", email="owner@example.com")
    inviter = _user("owner-1", "owner@example.com", "Owner")
    invitee = _user("invitee-1", "invitee@example.com", "Invitee")
    member = _user("member-1", "member@example.com", "Member")
    admin = _user("admin-2", "admin@example.com", "Admin")
    owner = _user("owner-2", "owner2@example.com", "Owner2")
    invited = _user("invite-1", "invited@example.com", "Invited")
    saved = []

    users_by_email = {
        invitee.email: invitee,
        member.email: member,
        admin.email: admin,
        owner.email: owner,
        invited.email: invited,
    }
    memberships = {
        member.id: _membership(module, user_id=member.id, role=module.UserTenantRole.NORMAL, membership_id="m-normal"),
        admin.id: _membership(module, user_id=admin.id, role=module.UserTenantRole.ADMIN, membership_id="m-admin"),
        owner.id: _membership(module, user_id=owner.id, role=module.UserTenantRole.OWNER, membership_id="m-owner"),
        invited.id: _membership(module, user_id=invited.id, role=module.UserTenantRole.INVITE, membership_id="m-invite"),
    }

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == actor.id:
            return _membership(module, user_id=actor.id, role=module.UserTenantRole.OWNER, tenant_id=tenant_id, membership_id="m-actor")
        return memberships.get(user_id)

    monkeypatch.setattr(module.UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(module.UserTenantService, "save", lambda *args, **kwargs: saved.append(kwargs) or True)
    monkeypatch.setattr(module.UserService, "query", lambda _db, email: [users_by_email[email]] if email in users_by_email else [])
    monkeypatch.setattr(module.UserService, "get_by_id", lambda _db, user_id: inviter if user_id == actor.id else None)

    res = module.batch_create(
        "tenant-1",
        request_body=module.BatchInviteUsersRequest(
            emails=[
                " invitee@example.com ",
                "bad-email",
                "member@example.com",
                "member@example.com",
                "admin@example.com",
                "owner2@example.com",
                "invited@example.com",
                "missing@example.com",
                "",
            ]
        ),
        background_tasks=module.BackgroundTasks(),
        db=None,
        user=actor,
        _membership=_membership(module, user_id=actor.id, role=module.UserTenantRole.OWNER),
    )

    assert res["retcode"] == 0, res
    statuses = {item["email"]: item["status"] for item in res["data"]["results"]}
    assert statuses == {
        "invitee@example.com": "invited",
        "bad-email": "invalid_email",
        "member@example.com": "already_member",
        "admin@example.com": "already_admin",
        "owner2@example.com": "already_owner",
        "invited@example.com": "already_invited",
        "missing@example.com": "user_not_found",
    }, statuses
    assert res["data"]["summary"] == {
        "total": 7,
        "invited": 1,
        "already_member": 1,
        "already_admin": 1,
        "already_owner": 1,
        "already_invited": 1,
        "user_not_found": 1,
        "invalid_email": 1,
    }, res
    assert len(saved) == 1, saved


def test_update_member_role_requires_owner(monkeypatch):
    module = _load_tenant_module(monkeypatch)

    actor = SimpleNamespace(id="admin-1", email="admin@example.com")
    target = _membership(module, user_id="member-1", role=module.UserTenantRole.NORMAL, membership_id="m-target")

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == actor.id:
            return _membership(module, user_id=actor.id, role=module.UserTenantRole.ADMIN, tenant_id=tenant_id, membership_id="m-actor")
        if user_id == target.user_id:
            return target
        return None

    monkeypatch.setattr(module.UserTenantService, "get_membership", fake_get_membership)

    admin_membership = module.UserTenantService.get_membership(None, tenant_id="tenant-1", user_id=actor.id)
    with pytest.raises(module.BusinessError) as denied:
        module.require_role_manager(admin_membership)

    assert denied.value.retcode == module.RetCode.AUTHENTICATION_ERROR
    assert denied.value.retmsg == "No authorization."


def test_update_member_role_allows_owner_to_promote_normal_to_admin(monkeypatch):
    module = _load_tenant_module(monkeypatch)

    actor = SimpleNamespace(id="owner-1", email="owner@example.com")
    target = _membership(module, user_id="member-1", role=module.UserTenantRole.NORMAL, membership_id="m-target")
    updated = []

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == actor.id:
            return _membership(module, user_id=actor.id, role=module.UserTenantRole.OWNER, tenant_id=tenant_id, membership_id="m-actor")
        if user_id == target.user_id:
            return target
        return None

    monkeypatch.setattr(module.UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(module.UserTenantService, "filter_update", lambda _db, filters, payload: updated.append((filters, payload)) or True)

    owner_membership = module.require_role_manager(module.UserTenantService.get_membership(None, tenant_id="tenant-1", user_id=actor.id))
    res = module.update_member_role(
        "tenant-1",
        target.user_id,
        request_body=module.UpdateTenantMemberRoleRequest(role="admin"),
        db=None,
        _membership=owner_membership,
    )

    assert res["retcode"] == 0, res
    assert res["data"] == {"user_id": target.user_id, "role": "admin"}, res
    assert updated and updated[-1][1] == {"role": "admin"}, updated


def test_update_member_role_rejects_owner_and_invite_targets(monkeypatch):
    module = _load_tenant_module(monkeypatch)

    actor = SimpleNamespace(id="owner-1", email="owner@example.com")
    targets = {
        "owner-target": _membership(module, user_id="owner-target", role=module.UserTenantRole.OWNER, membership_id="m-owner"),
        "invite-target": _membership(module, user_id="invite-target", role=module.UserTenantRole.INVITE, membership_id="m-invite"),
    }

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == actor.id:
            return _membership(module, user_id=actor.id, role=module.UserTenantRole.OWNER, tenant_id=tenant_id, membership_id="m-actor")
        return targets.get(user_id)

    monkeypatch.setattr(module.UserTenantService, "get_membership", fake_get_membership)

    owner_membership = module.require_role_manager(module.UserTenantService.get_membership(None, tenant_id="tenant-1", user_id=actor.id))
    owner_res = module.update_member_role(
        "tenant-1",
        "owner-target",
        request_body=module.UpdateTenantMemberRoleRequest(role="normal"),
        db=None,
        _membership=owner_membership,
    )
    invite_res = module.update_member_role(
        "tenant-1",
        "invite-target",
        request_body=module.UpdateTenantMemberRoleRequest(role="admin"),
        db=None,
        _membership=owner_membership,
    )

    assert owner_res["retmsg"] == "Owner role cannot be changed.", owner_res
    assert invite_res["retmsg"] == "Invite role cannot be changed before acceptance.", invite_res


def test_delete_member_allows_admin_but_protects_owner(monkeypatch):
    module = _load_tenant_module(monkeypatch)

    actor = SimpleNamespace(id="admin-1", email="admin@example.com")
    deleted_filters = []
    target_normal = _membership(module, user_id="member-1", role=module.UserTenantRole.NORMAL, membership_id="m-normal")
    target_owner = _membership(module, user_id="owner-1", role=module.UserTenantRole.OWNER, membership_id="m-owner")

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == actor.id:
            return _membership(module, user_id=actor.id, role=module.UserTenantRole.ADMIN, tenant_id=tenant_id, membership_id="m-actor")
        if user_id == target_normal.user_id:
            return target_normal
        if user_id == target_owner.user_id:
            return target_owner
        return None

    monkeypatch.setattr(module.UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(module.UserTenantService, "filter_delete", lambda _db, filters: deleted_filters.append(filters) or 1)

    ok_res = module.rm("tenant-1", target_normal.user_id, db=None, user=actor)
    owner_res = module.rm("tenant-1", target_owner.user_id, db=None, user=actor)

    assert ok_res["retcode"] == 0, ok_res
    assert deleted_filters, deleted_filters
    assert owner_res["retcode"] == module.RetCode.AUTHENTICATION_ERROR, owner_res
    assert owner_res["retmsg"] == "Owner cannot be removed by others.", owner_res


def test_knowledgebase_accessible_requires_owner_or_joined_member(monkeypatch):
    from api.db import UserTenantRole
    from api.db.services.knowledgebase_service import KnowledgebaseService
    from api.db.services.user_service import UserTenantService

    db = _FakeScalarSession("tenant-1")

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.INVITE, tenant_id=tenant_id, user_id=user_id),
    )
    assert KnowledgebaseService.accessible(db, "kb-1", "user-1") is False

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.OWNER, tenant_id=tenant_id, user_id=user_id),
    )
    assert KnowledgebaseService.accessible(db, "kb-1", "user-1") is True

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.ADMIN, tenant_id=tenant_id, user_id=user_id),
    )
    assert KnowledgebaseService.accessible(db, "kb-1", "user-1") is True

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.NORMAL, tenant_id=tenant_id, user_id=user_id),
    )
    assert KnowledgebaseService.accessible(db, "kb-1", "user-1") is True

    db.scalar_value = None
    assert KnowledgebaseService.accessible(db, "kb-missing", "user-1") is False


def test_document_accessible_requires_owner_or_joined_member(monkeypatch):
    from api.db import UserTenantRole
    from api.db.services.document_service import DocumentService
    from api.db.services.user_service import UserTenantService

    db = _FakeScalarSession("tenant-1")

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.INVITE, tenant_id=tenant_id, user_id=user_id),
    )
    assert DocumentService.accessible(db, "doc-1", "user-1") is False

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.OWNER, tenant_id=tenant_id, user_id=user_id),
    )
    assert DocumentService.accessible(db, "doc-1", "user-1") is True

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.ADMIN, tenant_id=tenant_id, user_id=user_id),
    )
    assert DocumentService.accessible(db, "doc-1", "user-1") is True

    monkeypatch.setattr(
        UserTenantService,
        "get_membership",
        lambda _db, tenant_id, user_id: SimpleNamespace(role=UserTenantRole.NORMAL, tenant_id=tenant_id, user_id=user_id),
    )
    assert DocumentService.accessible(db, "doc-1", "user-1") is True

    db.scalar_value = None
    assert DocumentService.accessible(db, "doc-missing", "user-1") is False
