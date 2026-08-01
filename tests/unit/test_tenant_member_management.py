"""[Deprecated] ``/v1/tenant/*`` 路由契约测试（迁移示范：TestClient 走真实中间件/路由/依赖/异常 handler 栈）。

正典 ``/api/v1/tenants/*`` 的契约测试在 ``test_tenant_restful_api.py``；本文件锁的是
旧 web 端点在收编后行为不变（前端过渡期仍在打这些路径）。

形态说明（internal/test_form_upgrade_plan.md Phase B）：
- 路由行为测试打真实 HTTP（conftest 的 session 级 ``client``），service 层用
  monkeypatch 给**真实类**打桩；断言面只有 HTTP 契约（状态码 / retcode / 载荷），
  对路由内部重构免疫。原 sys.modules 整包伪造已删除——它曾把
  RetCode.AUTHENTICATION_ERROR 桩成 401（真值 109），契约式从根上消灭此类漂移。
- 末尾两个 service 纯逻辑测试保留原形态（纯函数 + 显式打桩，仍合法）。
"""

import sys
from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.apps.services import tenant_api_service
from api.db import UserTenantRole
from api.db.services.user_service import UserService, UserTenantService
from common.constants import RetCode


def _tenant_module():
    """register_page 以 ``api.apps.tenant`` 名加载的真实路由模块（client fixture 保证已导入）。"""
    return sys.modules["api.apps.tenant"]


def _membership(user_id: str, role: UserTenantRole, tenant_id: str = "tenant-1", membership_id: str = "membership-1"):
    return SimpleNamespace(id=membership_id, tenant_id=tenant_id, user_id=user_id, role=role)


def _user(user_id: str, email: str, nickname: str):
    return SimpleNamespace(id=user_id, email=email, nickname=nickname, avatar="avatar-url")


def test_user_list_allows_admin_and_denies_normal(client, client_user, monkeypatch):
    memberships = {client_user.id: _membership(client_user.id, UserTenantRole.ADMIN)}
    monkeypatch.setattr(UserTenantService, "get_membership", lambda _db, tenant_id, user_id: memberships.get(user_id))
    monkeypatch.setattr(UserTenantService, "get_by_tenant_id", lambda _db, _tenant_id: [{"user_id": "member-1", "update_date": "2024-01-01 00:00:00"}])
    monkeypatch.setattr(tenant_api_service, "delta_seconds", lambda _value: 42)

    ok_res = client.get("/v1/tenant/tenant-1/user/list")
    assert ok_res.status_code == 200, ok_res.text
    body = ok_res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"][0]["delta_seconds"] == 42, body

    # 拒绝：normal 角色过不了 require_member_manager 依赖（BusinessError → 全局 handler）
    memberships[client_user.id] = _membership(client_user.id, UserTenantRole.NORMAL)
    denied = client.get("/v1/tenant/tenant-1/user/list")
    assert denied.status_code == 200, denied.text
    assert denied.json() == {"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "No authorization.", "data": False}


def test_single_invite_allows_admin_and_is_best_effort_on_email(client, client_user, monkeypatch):
    invitee = _user("invitee-1", "invitee@example.com", "Invitee")
    inviter = _user(client_user.id, client_user.email, "Admin Inviter")
    saved: list[dict] = []
    sent: list[dict] = []

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == client_user.id:
            return _membership(client_user.id, UserTenantRole.ADMIN, tenant_id=tenant_id)
        return None

    def fake_get_by_id(_db, user_id):
        return {client_user.id: inviter, invitee.id: invitee}.get(user_id)

    async def fake_send_invite_email(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(UserTenantService, "save", lambda *args, **kwargs: saved.append(kwargs) or True)
    monkeypatch.setattr(UserService, "query", lambda _db, email: [invitee] if email == invitee.email else [])
    monkeypatch.setattr(UserService, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(_tenant_module(), "send_invite_email", fake_send_invite_email)

    res = client.post("/v1/tenant/tenant-1/user", json={"email": invitee.email})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"] == {"id": invitee.id, "avatar": invitee.avatar, "email": invitee.email, "nickname": invitee.nickname}, body
    assert saved and saved[-1]["role"] == UserTenantRole.INVITE, saved
    # 邮件经真实 BackgroundTasks 管道在响应后执行（天然 best-effort）
    assert len(sent) == 1 and sent[0]["to_email"] == invitee.email, sent


def test_batch_invite_returns_statuses_and_summary(client, client_user, monkeypatch):
    inviter = _user(client_user.id, client_user.email, "Owner")
    invitee = _user("invitee-1", "invitee@example.com", "Invitee")
    member = _user("member-1", "member@example.com", "Member")
    admin = _user("admin-2", "admin@example.com", "Admin")
    owner = _user("owner-2", "owner2@example.com", "Owner2")
    invited = _user("invite-1", "invited@example.com", "Invited")
    saved: list[dict] = []

    users_by_email = {u.email: u for u in (invitee, member, admin, owner, invited)}
    memberships = {
        member.id: _membership(member.id, UserTenantRole.NORMAL, membership_id="m-normal"),
        admin.id: _membership(admin.id, UserTenantRole.ADMIN, membership_id="m-admin"),
        owner.id: _membership(owner.id, UserTenantRole.OWNER, membership_id="m-owner"),
        invited.id: _membership(invited.id, UserTenantRole.INVITE, membership_id="m-invite"),
    }

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == client_user.id:
            return _membership(client_user.id, UserTenantRole.OWNER, tenant_id=tenant_id, membership_id="m-actor")
        return memberships.get(user_id)

    async def fake_send_invite_email(**_kwargs):
        return True

    monkeypatch.setattr(UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(UserTenantService, "save", lambda *args, **kwargs: saved.append(kwargs) or True)
    monkeypatch.setattr(UserService, "query", lambda _db, email: [users_by_email[email]] if email in users_by_email else [])
    monkeypatch.setattr(UserService, "get_by_id", lambda _db, user_id: inviter if user_id == client_user.id else None)
    monkeypatch.setattr(_tenant_module(), "send_invite_email", fake_send_invite_email)

    res = client.post(
        "/v1/tenant/tenant-1/user/batch",
        json={
            "emails": [
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
        },
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    statuses = {item["email"]: item["status"] for item in body["data"]["results"]}
    assert statuses == {
        "invitee@example.com": "invited",
        "bad-email": "invalid_email",
        "member@example.com": "already_member",
        "admin@example.com": "already_admin",
        "owner2@example.com": "already_owner",
        "invited@example.com": "already_invited",
        "missing@example.com": "user_not_found",
    }, statuses
    assert body["data"]["summary"] == {
        "total": 7,
        "invited": 1,
        "already_member": 1,
        "already_admin": 1,
        "already_owner": 1,
        "already_invited": 1,
        "user_not_found": 1,
        "invalid_email": 1,
    }, body
    assert len(saved) == 1, saved


def test_update_member_role_requires_owner(client, client_user, monkeypatch):
    target = _membership("member-1", UserTenantRole.NORMAL, membership_id="m-target")

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == client_user.id:
            return _membership(client_user.id, UserTenantRole.ADMIN, tenant_id=tenant_id, membership_id="m-actor")
        return target if user_id == target.user_id else None

    monkeypatch.setattr(UserTenantService, "get_membership", fake_get_membership)

    res = client.put(f"/v1/tenant/tenant-1/user/{target.user_id}/role", json={"role": "admin"})

    assert res.status_code == 200, res.text
    assert res.json() == {"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "No authorization.", "data": False}


def test_update_member_role_allows_owner_to_promote_normal_to_admin(client, client_user, monkeypatch):
    target = _membership("member-1", UserTenantRole.NORMAL, membership_id="m-target")
    updated: list[tuple] = []

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == client_user.id:
            return _membership(client_user.id, UserTenantRole.OWNER, tenant_id=tenant_id, membership_id="m-actor")
        return target if user_id == target.user_id else None

    monkeypatch.setattr(UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(UserTenantService, "filter_update", lambda _db, filters, payload: updated.append((filters, payload)) or True)

    res = client.put(f"/v1/tenant/tenant-1/user/{target.user_id}/role", json={"role": "admin"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"] == {"user_id": target.user_id, "role": "admin"}, body
    assert updated and updated[-1][1] == {"role": "admin"}, updated


def test_update_member_role_rejects_owner_and_invite_targets(client, client_user, monkeypatch):
    targets = {
        "owner-target": _membership("owner-target", UserTenantRole.OWNER, membership_id="m-owner"),
        "invite-target": _membership("invite-target", UserTenantRole.INVITE, membership_id="m-invite"),
    }

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == client_user.id:
            return _membership(client_user.id, UserTenantRole.OWNER, tenant_id=tenant_id, membership_id="m-actor")
        return targets.get(user_id)

    monkeypatch.setattr(UserTenantService, "get_membership", fake_get_membership)

    owner_res = client.put("/v1/tenant/tenant-1/user/owner-target/role", json={"role": "normal"})
    invite_res = client.put("/v1/tenant/tenant-1/user/invite-target/role", json={"role": "admin"})

    assert owner_res.status_code == 200 and invite_res.status_code == 200
    owner_body, invite_body = owner_res.json(), invite_res.json()
    assert owner_body["retcode"] == RetCode.DATA_ERROR and owner_body["retmsg"] == "Owner role cannot be changed.", owner_body
    assert invite_body["retcode"] == RetCode.DATA_ERROR and invite_body["retmsg"] == "Invite role cannot be changed before acceptance.", invite_body


def test_delete_member_allows_admin_but_protects_owner(client, client_user, monkeypatch):
    deleted_filters: list = []
    target_normal = _membership("member-1", UserTenantRole.NORMAL, membership_id="m-normal")
    target_owner = _membership("owner-1", UserTenantRole.OWNER, membership_id="m-owner")

    def fake_get_membership(_db, tenant_id, user_id):
        if user_id == client_user.id:
            return _membership(client_user.id, UserTenantRole.ADMIN, tenant_id=tenant_id, membership_id="m-actor")
        return {target_normal.user_id: target_normal, target_owner.user_id: target_owner}.get(user_id)

    monkeypatch.setattr(UserTenantService, "get_membership", fake_get_membership)
    monkeypatch.setattr(UserTenantService, "filter_delete", lambda _db, filters: deleted_filters.append(filters) or 1)

    ok_res = client.delete(f"/v1/tenant/tenant-1/user/{target_normal.user_id}")
    owner_res = client.delete(f"/v1/tenant/tenant-1/user/{target_owner.user_id}")

    assert ok_res.status_code == 200, ok_res.text
    assert ok_res.json() == {"retcode": RetCode.SUCCESS, "retmsg": "success", "data": True}
    assert deleted_filters, deleted_filters
    owner_body = owner_res.json()
    assert owner_body["retcode"] == RetCode.AUTHENTICATION_ERROR, owner_body
    assert owner_body["retmsg"] == "Owner cannot be removed by others.", owner_body


# ---------------------------------------------------------------------------
# service 纯逻辑测试（保留原形态：纯函数 + 显式打桩）
# ---------------------------------------------------------------------------


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


def test_knowledgebase_accessible_requires_owner_or_joined_member(monkeypatch):
    from api.db.services.knowledgebase_service import KnowledgebaseService

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
    from api.db.services.document_service import DocumentService

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
