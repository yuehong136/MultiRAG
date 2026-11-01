"""Real Python recovery routes, scratch PostgreSQL, Redis and client isolation."""

import asyncio
import errno
import hashlib
import socket
import socketserver
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import User
from api.db.services import password_recovery_service as recovery
from api.db.services.user_service import UserService
from api.utils.password_recovery import COOKIE
from api.utils.web_utils import hash_code, verified_key
from common.constants import RetCode
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal


def payload(env: dict[str, Any], password: str = "new-plain-password", owner: int = 0) -> dict[str, str]:
    return {"email": f"{env['owners'][owner]}@release.example.com", "new_password": password, "confirm_new_password": password}


def state(env: dict[str, Any], owner: int = 0) -> dict[str, Any]:
    with Session(env["engine"]) as reader:
        user = reader.get(User, env["owners"][owner])
        return {"password": user.password, "access_token": user.access_token, "last_login_time": user.last_login_time, "email": user.email, "account_kind": user.account_kind}


def post(env: dict[str, Any], client: requests.Session, body: dict[str, str], path: str = "/reset", **kwargs: Any) -> requests.Response:
    return client.post(env["base"] + "/api/v1/auth/password" + path, json=body, timeout=15, **kwargs)


def challenge(env: dict[str, Any], email: str, code: str = "ABCD", owner: int = 0) -> None:
    salt = b"x" * 16
    REDIS_CONN.REDIS.set(recovery.keys(env["owners"][owner])[1], hash_code(code, salt) + ":" + salt.hex(), ex=300)


def verified(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, client: requests.Session) -> str:
    sent: list[dict[str, Any]] = []

    async def sender(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(sys.modules["api.apps.restful_apis.user"], "send_email_html", sender)
    email = payload(env)["email"]
    captcha = client.post(env["base"] + "/api/v1/auth/password/forgot/captcha", params={"email": email}, timeout=15)
    assert captcha.status_code == 200 and captcha.headers["content-type"].startswith("image/")
    text = REDIS_CONN.REDIS.get(recovery.keys(env["owners"][0])[0])
    sent_response = post(env, client, {"email": email, "captcha": text}, "/forgot/otp")
    assert sent_response.json()["retcode"] == 0 and len(sent) == 1
    response = post(env, client, {"email": email, "otp": sent[0]["code"]}, "/forgot/otp/verify")
    assert response.json()["retcode"] == 0, response.text
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Domain=" not in cookie
    token = client.cookies.get(COOKIE)
    assert token and REDIS_CONN.REDIS.get(recovery.keys(env["owners"][0])[5]) == hashlib.sha256(token.encode()).hexdigest()
    return token


def test_legacy_marker_cannot_authorize_a_client(release_api: dict[str, Any]) -> None:
    env = release_api
    body = payload(env)
    before = state(env)
    REDIS_CONN.REDIS.set(verified_key(body["email"]), "1", ex=300)
    with requests.Session() as attacker:
        response = post(env, attacker, body)
    assert response.json()["retcode"] == RetCode.AUTHENTICATION_ERROR
    assert state(env) == before


def test_recovery_binding_and_replay(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    before = state(env)
    with requests.Session() as owner, requests.Session() as attacker:
        token = verified(env, monkeypatch, owner)
        proof_key = recovery.keys(env["owners"][0])[5]
        digest = REDIS_CONN.REDIS.get(proof_key)
        for cookie in [None, "1", "b" * 64]:
            headers = {} if cookie is None else {"Cookie": COOKIE + "=" + cookie}
            assert post(env, attacker, payload(env), headers=headers).json()["retcode"] == RetCode.AUTHENTICATION_ERROR
            assert REDIS_CONN.REDIS.get(proof_key) == digest and state(env) == before
        assert post(env, owner, dict(payload(env), confirm_new_password="different")).json()["retcode"] == RetCode.ARGUMENT_ERROR
        assert REDIS_CONN.REDIS.get(proof_key) == digest
        assert post(env, owner, payload(env)).json()["retcode"] == 0
        changed = state(env)
        assert UserService.verify_password(payload(env)["new_password"], changed["password"])
        assert changed["access_token"] == before["access_token"] and changed["last_login_time"] == before["last_login_time"]
        assert REDIS_CONN.REDIS.get(proof_key) is None and owner.cookies.get(COOKIE) is None
        assert post(env, attacker, payload(env, "replayed"), headers={"Cookie": COOKIE + "=" + token}).json()["retcode"] != 0
        assert state(env) == changed


@pytest.mark.parametrize("phase", ["verify", "reset"])
def test_concurrent_recovery_consumes_once(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    env = release_api
    token = ""
    if phase == "reset":
        with requests.Session() as client:
            token = verified(env, monkeypatch, client)
    else:
        challenge(env, payload(env)["email"])

    def invoke(_: int) -> int:
        with requests.Session() as client:
            if phase == "verify":
                result = post(env, client, {"email": payload(env)["email"], "otp": "ABCD"}, "/forgot/otp/verify")
            else:
                result = post(env, client, payload(env), headers={"Cookie": COOKIE + "=" + token})
            return result.json()["retcode"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert results.count(0) == 1, results


def test_rollback_retains_proof_but_commit_uncertainty_does_not(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    with requests.Session() as client:
        verified(env, monkeypatch, client)
        proof_key = recovery.keys(env["owners"][0])[5]
        digest = REDIS_CONN.REDIS.get(proof_key)
        before = state(env)
        table = User.__table__.fullname
        try:
            with env["engine"].begin() as connection:
                connection.exec_driver_sql("CREATE FUNCTION usr_ai.reject_recovery() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled recovery failure'; END $$")
                connection.exec_driver_sql(f"CREATE TRIGGER reject_recovery BEFORE UPDATE OF password ON {table} FOR EACH ROW EXECUTE FUNCTION usr_ai.reject_recovery()")
            assert post(env, client, payload(env)).json()["retcode"] != 0
            assert state(env) == before and REDIS_CONN.REDIS.get(proof_key) == digest
        finally:
            with env["engine"].begin() as connection:
                connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS reject_recovery ON {table}")
                connection.exec_driver_sql("DROP FUNCTION IF EXISTS usr_ai.reject_recovery()")
        real_commit = AsyncSession.commit

        async def unknown_commit(self: AsyncSession) -> None:
            raise OSError("controlled unknown COMMIT result")

        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "commit", unknown_commit)
            assert post(env, client, payload(env)).json()["retcode"] != 0
        assert state(env) == before
        assert REDIS_CONN.REDIS.get(proof_key).startswith("claim:")
        assert post(env, client, payload(env)).json()["retcode"] != 0

        # A new, independently verified OTP can issue a fresh proof after expiry.
        REDIS_CONN.REDIS.delete(proof_key)
        verified(env, monkeypatch, client)

        async def acknowledged_lost(self: AsyncSession) -> None:
            await real_commit(self)
            raise OSError("controlled COMMIT acknowledgement loss")

        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "commit", acknowledged_lost)
            assert post(env, client, payload(env)).json()["retcode"] == 0
        assert UserService.verify_password(payload(env)["new_password"], state(env)["password"])


def test_recovery_origin_lock_and_redis_failure(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    before = state(env)
    with requests.Session() as client:
        token = verified(env, monkeypatch, client)
        proof_key = recovery.keys(env["owners"][0])[5]
        digest = REDIS_CONN.REDIS.get(proof_key)
        for origin in ["null", "https://attacker.example", env["base"] + "/"]:
            for path in ["/forgot/captcha", "/forgot/otp", "/forgot/otp/verify", "/reset"]:
                response = post(env, client, payload(env), path, headers={"Origin": origin})
                assert response.status_code == 403
            response = client.options(env["base"] + "/api/v1/auth/password/reset", headers={"Origin": origin, "Access-Control-Request-Method": "POST"}, timeout=15)
            assert response.status_code == 403
            assert state(env) == before and REDIS_CONN.REDIS.get(proof_key) == digest
        addresses = recovery.keys(env["owners"][0])
        REDIS_CONN.REDIS.set(addresses[4], 1, ex=1800)
        REDIS_CONN.REDIS.set(addresses[0], "ABCD", ex=60)
        response = post(env, client, {"email": payload(env)["email"], "captcha": "ABCD"}, "/forgot/otp")
        assert response.json()["retcode"] != 0 and REDIS_CONN.REDIS.ttl(addresses[4]) > 0
        real_eval = REDIS_CONN.REDIS.eval

        def broken_eval(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("controlled Redis unavailability")

        with monkeypatch.context() as patch:
            patch.setattr(REDIS_CONN.REDIS, "eval", broken_eval)
            response = post(env, client, payload(env), headers={"Cookie": COOKIE + "=" + token})
            assert response.json()["retcode"] != 0
        assert REDIS_CONN.REDIS.eval == real_eval
        assert state(env) == before and REDIS_CONN.REDIS.get(proof_key) == digest


def test_account_case_and_backend_namespaces_do_not_alias(release_api: dict[str, Any]) -> None:
    env = release_api
    email = payload(env)["email"]
    second_email = email.upper()
    with env["engine"].begin() as connection:
        connection.execute(sa.update(User).where(User.id == env["owners"][1]).values(email=second_email))
    before = state(env, 1)
    challenge(env, email)
    REDIS_CONN.REDIS.set("otp:go:verified:" + second_email, "a" * 64, ex=300)
    with requests.Session() as client:
        result = post(env, client, {"email": second_email, "otp": "ABCD"}, "/forgot/otp/verify")
        assert result.json()["retcode"] != 0
        result = post(env, client, dict(payload(env), email=second_email), headers={"Cookie": COOKIE + "=" + "a" * 64})
        assert result.json()["retcode"] != 0
    assert state(env, 1) == before and REDIS_CONN.REDIS.get(recovery.keys(env["owners"][0])[1])


@contextmanager
def accepted_smtp(block: bool) -> Iterator[tuple[int, threading.Event, threading.Event]]:
    entered, release = threading.Event(), threading.Event()
    journal = ResourceJournal("recovery-smtp")

    class SMTP(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.connection.settimeout(5)
            self.wfile.write(b"220 controlled SMTP\r\n")
            while command := self.rfile.readline():
                if command.upper().startswith(b"DATA"):
                    self.wfile.write(b"354 message\r\n")
                    while line := self.rfile.readline():
                        if line == b".\r\n":
                            break
                    entered.set()
                    if block:
                        assert release.wait(5)
                    self.wfile.write(b"250 accepted\r\n")
                elif command.upper().startswith(b"QUIT"):
                    self.wfile.write(b"500 controlled QUIT failure after DATA\r\n")
                    break
                else:
                    self.wfile.write(b"250 ok\r\n")

    for attempt in range(32):
        address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
        journal.event("reserve-listener", address=list(address), attempt=attempt + 1)
        server = socketserver.ThreadingTCPServer(address, SMTP, bind_and_activate=False)
        try:
            server.server_bind()
            server.server_activate()
        except OSError as error:
            server.server_close()
            if error.errno != errno.EADDRINUSE or attempt == 31:
                raise
            continue
        break
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield address[1], entered, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
        with socket.socket() as reader:
            assert reader.connect_ex(address) != 0
        journal.event("listener-independent-readback", address=list(address), absent=True)


@pytest.mark.parametrize("cancel", [False, True])
def test_accepted_smtp_and_cancellation_preserve_delivered_otp(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    from api.utils import web_utils

    env = release_api
    email = payload(env)["email"]
    sent: list[dict[str, Any]] = []

    async def sender(**kwargs: Any) -> None:
        sent.append(kwargs)
        await web_utils.send_email_html(**kwargs)

    with accepted_smtp(cancel) as (port, entered, release):
        for name, value in {
            "SMTP_CONF": {"controlled": True},
            "MAIL_SERVER": "127.0.0.1",
            "MAIL_PORT": port,
            "MAIL_USE_SSL": False,
            "MAIL_USE_TLS": False,
            "MAIL_USERNAME": "",
            "MAIL_DEFAULT_SENDER": ("Controlled", "sender@example.test"),
        }.items():
            monkeypatch.setattr(web_utils.settings, name, value)
        monkeypatch.setattr(sys.modules["api.apps.restful_apis.user"], "send_email_html", sender)
        REDIS_CONN.REDIS.set(recovery.keys(env["owners"][0])[0], "ABCD", ex=60)
        if cancel:

            async def cancelled() -> None:
                engine = create_async_engine(env["engine"].url)
                try:
                    async with AsyncSession(engine) as db:
                        task = asyncio.create_task(recovery.send_otp(db, email, "ABCD", sender))
                        try:
                            assert await asyncio.to_thread(entered.wait, 3)
                            task.cancel()
                            await asyncio.sleep(0.03)
                            assert not task.done(), "SMTP must finish before cancellation returns"
                        finally:
                            release.set()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                finally:
                    await engine.dispose()

            asyncio.run(cancelled())
        else:
            response = post(env, env["client"], {"email": email, "captcha": "ABCD"}, "/forgot/otp")
            assert response.json()["retcode"] == 0
        assert entered.is_set() and len(sent) == 1
    response = post(env, env["client"], {"email": email, "otp": sent[0]["code"]}, "/forgot/otp/verify")
    assert response.json()["retcode"] == 0, "Accepted mail must still authorize its recipient"
