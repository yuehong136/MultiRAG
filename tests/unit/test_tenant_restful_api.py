"""tenant RESTful API 契约测试（``/api/v1/tenants/*``）。

七条路由走真实 ``api.apps.app`` 的 HTTP 契约式测试，service 层用 monkeypatch 给
**真实类**打桩。两条钉板：
- 同步 service 必须收到 run_sync 的同步 facade（``sqlalchemy.orm.Session``），
  "AsyncSession 直递同步 service" 的变异必红；
- DELETE 的 user_id 走 **body**（旧的 ``/users/{user_id}`` 路径形态必红）。

响应信封是 web 风格 ``{"retcode", "retmsg", "data"}``（与被收编的 ``/v1/tenant/*``
一致，前端 apiClient 两种信封都吃）。
"""

import sys
from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.apps.services import tenant_api_service
from api.db import UserTenantRole
from api.db.services.user_service import UserService, UserTenantService
from common.constants import RetCode

TENANT = "tenant-1"


def _assert_sync_facade(sessions):
    """同步 service 必须运行在 run_sync 的同步 facade 上（AsyncSession 直递必红）。"""
    assert sessions
    for session in sessions:
        assert isinstance(session, Session), f"同步 service 收到 {type(session).__name__}，应为 sqlalchemy.orm.Session"


def _membership(user_id: str, role: UserTenantRole, membership_id: str = "membership-1"):
    return SimpleNamespace(id=membership_id, tenant_id=TENANT, user_id=user_id, role=role)


def _user(user_id: str, email: str, nickname: str):
    return SimpleNamespace(id=user_id, email=email, nickname=nickname, avatar="avatar-url")


def _stub_memberships(monkeypatch, sessions, memberships: dict):
    def fake_get_membership(_db, tenant_id, user_id):
        sessions.append(_db)
        return memberships.get(user_id)

    monkeypatch.setattr(UserTenantService, "get_membership", fake_get_membership)


def test_tenant_list_returns_delta_seconds(client, monkeypatch):
    sessions: list[object] = []

    def fake_get_tenants(_db, user_id):
        sessions.append(_db)
        assert user_id == "user-unit"
        return [{"tenant_id": TENANT, "update_date": "2024-01-01 00:00:00"}]

    monkeypatch.setattr(UserTenantService, "get_tenants_by_user_id", fake_get_tenants)
    monkeypatch.setattr(tenant_api_service, "delta_seconds", lambda _value: 42)

    res = client.get("/api/v1/tenants")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"][0]["delta_seconds"] == 42, body
    _assert_sync_facade(sessions)


def test_agree_is_patch_and_promotes_invite_to_normal(client, monkeypatch):
    sessions: list[object] = []
    updated: list[tuple] = []

    def fake_filter_update(_db, filters, payload):
        sessions.append(_db)
        updated.append((filters, payload))
        return True

    monkeypatch.setattr(UserTenantService, "filter_update", fake_filter_update)

    res = client.patch(f"/api/v1/tenants/{TENANT}")

    assert res.status_code == 200, res.text
    assert res.json() == {"retcode": RetCode.SUCCESS, "retmsg": "success", "data": True}
    assert updated[-1][1] == {"role": UserTenantRole.NORMAL}, updated
    _assert_sync_facade(sessions)

    # 旧的 PUT /agree/{tenant_id} 形态在 RESTful 面不存在
    assert client.put(f"/api/v1/tenants/agree/{TENANT}").status_code in (404, 405)


def test_user_list_allows_admin_and_denies_normal(client, client_user, monkeypatch):
    sessions: list[object] = []
    memberships = {client_user.id: _membership(client_user.id, UserTenantRole.ADMIN)}
    _stub_memberships(monkeypatch, sessions, memberships)

    def fake_get_by_tenant_id(_db, _tenant_id):
        sessions.append(_db)
        return [{"user_id": "member-1", "update_date": "2024-01-01 00:00:00"}]

    monkeypatch.setattr(UserTenantService, "get_by_tenant_id", fake_get_by_tenant_id)
    monkeypatch.setattr(tenant_api_service, "delta_seconds", lambda _value: 42)

    ok_res = client.get(f"/api/v1/tenants/{TENANT}/users")

    assert ok_res.status_code == 200, ok_res.text
    body = ok_res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"][0]["delta_seconds"] == 42, body
    _assert_sync_facade(sessions)

    memberships[client_user.id] = _membership(client_user.id, UserTenantRole.NORMAL)
    denied = client.get(f"/api/v1/tenants/{TENANT}/users")

    assert denied.status_code == 200, denied.text
    assert denied.json() == {"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "No authorization.", "data": False}


def test_single_invite_saves_membership_and_sends_email(client, client_user, monkeypatch):
    sessions: list[object] = []
    invitee = _user("invitee-1", "invitee@example.com", "Invitee")
    inviter = _user(client_user.id, client_user.email, "Admin Inviter")
    saved: list[dict] = []
    sent: list[dict] = []

    _stub_memberships(monkeypatch, sessions, {client_user.id: _membership(client_user.id, UserTenantRole.ADMIN)})
    monkeypatch.setattr(UserTenantService, "save", lambda *args, **kwargs: saved.append(kwargs) or True)
    monkeypatch.setattr(UserService, "query", lambda _db, email: [invitee] if email == invitee.email else [])
    monkeypatch.setattr(UserService, "get_by_id", lambda _db, user_id: {client_user.id: inviter, invitee.id: invitee}.get(user_id))

    async def fake_send_invite_email(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(sys.modules["api.apps.restful_apis.tenant"], "send_invite_email", fake_send_invite_email)

    res = client.post(f"/api/v1/tenants/{TENANT}/users", json={"email": invitee.email})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"] == {"id": invitee.id, "avatar": invitee.avatar, "email": invitee.email, "nickname": invitee.nickname}, body
    assert saved and saved[-1]["role"] == UserTenantRole.INVITE, saved
    # 邮件经真实 BackgroundTasks 管道在响应后执行（天然 best-effort）
    assert len(sent) == 1 and sent[0]["to_email"] == invitee.email, sent
    assert sent[0]["inviter"] == "Admin Inviter", sent
    _assert_sync_facade(sessions)


def test_batch_invite_returns_statuses_and_summary(client, client_user, monkeypatch):
    sessions: list[object] = []
    inviter = _user(client_user.id, client_user.email, "Owner")
    invitee = _user("invitee-1", "invitee@example.com", "Invitee")
    member = _user("member-1", "member@example.com", "Member")
    saved: list[dict] = []
    sent: list[dict[str, object]] = []

    users_by_email = {u.email: u for u in (invitee, member)}
    memberships = {
        client_user.id: _membership(client_user.id, UserTenantRole.OWNER, membership_id="m-actor"),
        member.id: _membership(member.id, UserTenantRole.NORMAL, membership_id="m-normal"),
    }
    _stub_memberships(monkeypatch, sessions, memberships)
    monkeypatch.setattr(UserTenantService, "save", lambda *args, **kwargs: saved.append(kwargs) or True)
    monkeypatch.setattr(UserService, "query", lambda _db, email: [users_by_email[email]] if email in users_by_email else [])
    monkeypatch.setattr(UserService, "get_by_id", lambda _db, user_id: inviter if user_id == client_user.id else None)

    async def fake_send_invite_email(**kwargs: object) -> bool:
        sent.append(kwargs)
        return True

    monkeypatch.setattr(sys.modules["api.apps.restful_apis.tenant"], "send_invite_email", fake_send_invite_email)

    res = client.post(
        f"/api/v1/tenants/{TENANT}/users/batch",
        json={"emails": [" invitee@example.com ", "invitee@example.com", "bad-email", "member@example.com", "missing@example.com", ""]},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    statuses = {item["email"]: item["status"] for item in body["data"]["results"]}
    assert statuses == {
        "invitee@example.com": "invited",
        "bad-email": "invalid_email",
        "member@example.com": "already_member",
        "missing@example.com": "user_not_found",
    }, statuses
    assert body["data"]["summary"]["total"] == 4, body
    assert body["data"]["summary"]["invited"] == 1, body
    assert len(saved) == 1, saved
    assert len(sent) == 1 and sent[0]["to_email"] == invitee.email, sent
    _assert_sync_facade(sessions)


def test_remove_member_takes_user_id_from_body(client, client_user, monkeypatch):
    sessions: list[object] = []
    deleted_filters: list = []
    target = _membership("member-1", UserTenantRole.NORMAL, membership_id="m-normal")
    memberships = {
        client_user.id: _membership(client_user.id, UserTenantRole.ADMIN, membership_id="m-actor"),
        target.user_id: target,
    }
    _stub_memberships(monkeypatch, sessions, memberships)
    monkeypatch.setattr(UserTenantService, "filter_delete", lambda _db, filters: deleted_filters.append(filters) or 1)

    res = client.request("DELETE", f"/api/v1/tenants/{TENANT}/users", json={"user_id": target.user_id})

    assert res.status_code == 200, res.text
    assert res.json() == {"retcode": RetCode.SUCCESS, "retmsg": "success", "data": True}
    assert deleted_filters, deleted_filters
    _assert_sync_facade(sessions)

    # 缺 user_id 是参数错误，不是静默成功
    assert client.request("DELETE", f"/api/v1/tenants/{TENANT}/users", json={}).status_code == 422
    # 旧的路径形态在 RESTful 面不存在
    assert client.delete(f"/api/v1/tenants/{TENANT}/users/{target.user_id}").status_code in (404, 405)


def test_remove_member_protects_owner_and_rejects_outsiders(client, client_user, monkeypatch):
    sessions: list[object] = []
    owner = _membership("owner-1", UserTenantRole.OWNER, membership_id="m-owner")
    memberships = {
        client_user.id: _membership(client_user.id, UserTenantRole.ADMIN, membership_id="m-actor"),
        owner.user_id: owner,
    }
    _stub_memberships(monkeypatch, sessions, memberships)
    monkeypatch.setattr(UserTenantService, "filter_delete", lambda _db, _filters: 1)

    owner_res = client.request("DELETE", f"/api/v1/tenants/{TENANT}/users", json={"user_id": owner.user_id})

    owner_body = owner_res.json()
    assert owner_body["retcode"] == RetCode.AUTHENTICATION_ERROR, owner_body
    assert owner_body["retmsg"] == "Owner cannot be removed by others.", owner_body

    # 非管理员删别人：拒绝
    memberships[client_user.id] = _membership(client_user.id, UserTenantRole.NORMAL, membership_id="m-actor")
    denied = client.request("DELETE", f"/api/v1/tenants/{TENANT}/users", json={"user_id": "member-1"})

    denied_body = denied.json()
    assert denied_body["retcode"] == RetCode.AUTHENTICATION_ERROR, denied_body
    assert denied_body["retmsg"] == "No authorization.", denied_body
    _assert_sync_facade(sessions)


def test_update_member_role_requires_owner(client, client_user, monkeypatch):
    sessions: list[object] = []
    target = _membership("member-1", UserTenantRole.NORMAL, membership_id="m-target")
    memberships = {
        client_user.id: _membership(client_user.id, UserTenantRole.ADMIN, membership_id="m-actor"),
        target.user_id: target,
    }
    _stub_memberships(monkeypatch, sessions, memberships)

    denied = client.put(f"/api/v1/tenants/{TENANT}/users/{target.user_id}/role", json={"role": "admin"})

    assert denied.status_code == 200, denied.text
    assert denied.json() == {"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "No authorization.", "data": False}

    memberships[client_user.id] = _membership(client_user.id, UserTenantRole.OWNER, membership_id="m-actor")
    updated: list[tuple] = []
    monkeypatch.setattr(UserTenantService, "filter_update", lambda _db, filters, payload: updated.append((filters, payload)) or True)

    ok_res = client.put(f"/api/v1/tenants/{TENANT}/users/{target.user_id}/role", json={"role": "admin"})

    assert ok_res.status_code == 200, ok_res.text
    body = ok_res.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"] == {"user_id": target.user_id, "role": "admin"}, body
    assert updated[-1][1] == {"role": "admin"}, updated
    _assert_sync_facade(sessions)


def test_update_member_role_rejects_owner_and_invite_targets(client, client_user, monkeypatch):
    sessions: list[object] = []
    memberships = {
        client_user.id: _membership(client_user.id, UserTenantRole.OWNER, membership_id="m-actor"),
        "owner-target": _membership("owner-target", UserTenantRole.OWNER, membership_id="m-owner"),
        "invite-target": _membership("invite-target", UserTenantRole.INVITE, membership_id="m-invite"),
    }
    _stub_memberships(monkeypatch, sessions, memberships)

    owner_res = client.put(f"/api/v1/tenants/{TENANT}/users/owner-target/role", json={"role": "normal"})
    invite_res = client.put(f"/api/v1/tenants/{TENANT}/users/invite-target/role", json={"role": "admin"})

    owner_body, invite_body = owner_res.json(), invite_res.json()
    assert owner_body["retcode"] == RetCode.DATA_ERROR and owner_body["retmsg"] == "Owner role cannot be changed.", owner_body
    assert invite_body["retcode"] == RetCode.DATA_ERROR and invite_body["retmsg"] == "Invite role cannot be changed before acceptance.", invite_body
    _assert_sync_facade(sessions)
