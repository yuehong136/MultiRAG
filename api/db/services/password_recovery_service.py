"""Client-bound, one-use Python password recovery with explicit SQL commits."""

import asyncio
import hashlib
import hmac
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import UserAccountKind
from api.db.db_models import User
from api.db.services.user_service import UserService
from api.utils.web_utils import ATTEMPT_LIMIT, ATTEMPT_LOCK_SECONDS, OTP_LENGTH, OTP_TTL_SECONDS, RESEND_COOLDOWN_SECONDS, hash_code
from common.app_config import get_app_config
from common.constants import StatusEnum
from common.time_utils import current_timestamp
from core.utils.redis_conn import REDIS_CONN

T = TypeVar("T")
COMMIT_READBACK_TIMEOUT = 5.0


class RecoveryError(ValueError):
    """A recovery transition was rejected without granting authority."""


async def blocking(call: Callable[..., T], *args: Any) -> T:
    """Drain accepted Redis/hash work on cancellation, never abandon a write."""
    task = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def redis_eval(script: str, keys: list[str], *args: Any) -> Any:
    client = REDIS_CONN.REDIS
    if client is None:
        raise RecoveryError("Recovery state unavailable")
    return await blocking(client.eval, script, len(keys), *keys, *args)


def keys(user_id: str) -> list[str]:
    """Exact account ID isolates case-sensitive emails and the Go backend."""
    return [f"otp:py:{kind}:{user_id}" for kind in ("captcha", "code", "attempts", "last", "lock", "proof")]


async def account(db: AsyncSession, email: str, *, lock: bool = False) -> User:
    if get_app_config().authentication.disable_password_login:
        raise RecoveryError("Password recovery is disabled")
    query = select(User).where(User.email == email, User.status == StatusEnum.VALID.value, User.account_kind.in_((UserAccountKind.LOCAL.value, UserAccountKind.HYBRID.value)))
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query)
    if row is None:
        raise RecoveryError("Invalid recovery account")
    return row


async def set_captcha(db: AsyncSession, email: str, text: str) -> None:
    user = await account(db, email)
    result = await redis_eval("return redis.call('set', KEYS[1], ARGV[1], 'EX', 60)", [keys(user.id)[0]], text)
    if result != "OK":
        raise RecoveryError("Recovery state unavailable")


SEND = """
if redis.call('exists',KEYS[5])==1 then return {-2} end
local proof=redis.call('get',KEYS[6])
if proof and string.sub(proof,1,6)=='claim:' then return {-2} end
local captcha=redis.call('get',KEYS[1])
if not captcha or string.lower(captcha)~=ARGV[1] then return {-1} end
local last=tonumber(redis.call('get',KEYS[4]) or '0')
if not last or tonumber(ARGV[2])-last<tonumber(ARGV[4]) then return {-3} end
local old={1}
for i=2,4 do table.insert(old,redis.call('get',KEYS[i]) or ''); table.insert(old,redis.call('pttl',KEYS[i])) end
redis.call('del',KEYS[1],KEYS[6])
redis.call('set',KEYS[2],ARGV[3],'EX',ARGV[5])
redis.call('set',KEYS[3],0,'EX',ARGV[5])
redis.call('set',KEYS[4],ARGV[2],'EX',ARGV[5])
return old
"""
RESTORE_SEND = """
if redis.call('get',KEYS[1])~=ARGV[1] then return 0 end
redis.call('del',KEYS[1],KEYS[2],KEYS[3])
for i=1,3 do
 local ttl=tonumber(ARGV[2*i+1])-tonumber(ARGV[8])
 if ttl>0 then redis.call('set',KEYS[i],ARGV[2*i],'PX',ttl) end
end
return 1
"""


async def send_otp(db: AsyncSession, email: str, captcha: str, sender: Callable[..., Awaitable[None]]) -> None:
    user = await account(db, email)
    addresses = keys(user.id)
    otp = "".join(secrets.choice(string.ascii_uppercase) for _ in range(OTP_LENGTH))
    salt = secrets.token_bytes(16)
    stored = hash_code(otp, salt) + ":" + salt.hex()
    started = time.monotonic()
    old = await redis_eval(SEND, addresses, captcha.lower(), int(time.time()), stored, RESEND_COOLDOWN_SECONDS, OTP_TTL_SECONDS)
    if old[0] != 1:
        raise RecoveryError("Invalid captcha, locked account or resend cooldown")
    sending = asyncio.ensure_future(sender(subject="Your Password Reset Code", to_email=email, template_key="reset_code", code=otp, ttl_min=OTP_TTL_SECONDS // 60))
    try:
        await asyncio.shield(sending)
    except asyncio.CancelledError:
        try:
            await sending
        except BaseException:
            await redis_eval(RESTORE_SEND, addresses[1:4], stored, *old[1:], int((time.monotonic() - started) * 1000))
        # Accepted mail remains usable, even if this HTTP caller has left.
        raise
    except Exception:
        # Only restore this send's exact challenge; a newer send wins.
        await redis_eval(RESTORE_SEND, addresses[1:4], stored, *old[1:], int((time.monotonic() - started) * 1000))
        raise


VERIFY = """
if redis.call('exists',KEYS[5])==1 then return -2 end
local proof=redis.call('get',KEYS[6])
if proof and string.sub(proof,1,6)=='claim:' then return -2 end
if redis.call('get',KEYS[2])~=ARGV[1] then return -1 end
if ARGV[2]~='1' then
 local n=redis.call('incr',KEYS[3]); redis.call('expire',KEYS[3],ARGV[4])
 if n>=tonumber(ARGV[5]) then redis.call('set',KEYS[5],1,'EX',ARGV[6]) end
 return -3
end
redis.call('del',KEYS[2],KEYS[3],KEYS[4])
redis.call('set',KEYS[6],ARGV[3],'EX',ARGV[4])
return 1
"""


async def verify_otp(db: AsyncSession, email: str, otp: str) -> str:
    user = await account(db, email)
    addresses = keys(user.id)
    stored = await redis_eval("return redis.call('get',KEYS[1])", [addresses[1]])
    if not isinstance(stored, str):
        raise RecoveryError("Expired OTP")
    try:
        expected, salt_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        if len(salt) != 16 or len(expected) != 64:
            raise ValueError("Invalid challenge")
    except ValueError as error:
        raise RecoveryError("Invalid OTP state") from error
    valid = hmac.compare_digest(hash_code(otp.upper(), salt), expected)
    token = secrets.token_hex(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    result = await redis_eval(VERIFY, addresses, stored, "1" if valid else "0", digest, OTP_TTL_SECONDS, ATTEMPT_LIMIT, ATTEMPT_LOCK_SECONDS)
    if result != 1:
        raise RecoveryError("Invalid or expired OTP")
    return token


CLAIM = """
if redis.call('get',KEYS[1])~=ARGV[1] then return 0 end
local ttl=redis.call('pttl',KEYS[1])
if ttl<=0 then return 0 end
redis.call('set',KEYS[1],ARGV[2],'PX',ttl)
return ttl
"""
RESTORE_PROOF = """
if redis.call('get',KEYS[1])~=ARGV[1] then return 0 end
local ttl=redis.call('pttl',KEYS[1])
if ttl<=0 then return 0 end
redis.call('set',KEYS[1],ARGV[2],'PX',ttl)
return 1
"""
DELETE_CLAIM = """
if redis.call('get',KEYS[1])~=ARGV[1] then return 0 end
return redis.call('del',KEYS[1])
"""


async def update_password(db: AsyncSession, user: User, password_hash: str) -> None:
    """Flush in the request transaction; this helper must never commit."""
    await db.execute(update(User).where(User.id == user.id).values(password=password_hash, update_time=current_timestamp(), update_date=datetime.now()))
    await db.flush()


async def committed_password(db: AsyncSession, user_id: str, password_hash: str) -> bool:
    """Only a locked independent read of this unique hash confirms COMMIT."""
    try:
        async with asyncio.timeout(COMMIT_READBACK_TIMEOUT), AsyncSession(bind=db.bind) as reader:
            await reader.connection(execution_options={"isolation_level": "READ COMMITTED"})
            await reader.execute(text("SET LOCAL lock_timeout = '4s'"))
            actual = await reader.scalar(select(User.password).where(User.id == user_id).with_for_update())
            return isinstance(actual, str) and hmac.compare_digest(actual, password_hash)
    except Exception:
        return False


async def reset_password(db: AsyncSession, email: str, password: str, token: str) -> None:
    user = await account(db, email, lock=True)
    user_id = user.id
    proof_key = keys(user_id)[5]
    digest = hashlib.sha256(token.encode()).hexdigest()
    claim = "claim:" + secrets.token_hex(32)
    if not await redis_eval(CLAIM, [proof_key], digest, claim):
        raise RecoveryError("Email not verified by this client")
    commit_attempted = False
    password_hash = ""
    try:
        password_hash = await blocking(UserService.hash_password, password)
        await update_password(db, user, password_hash)
        commit_attempted = True
        await db.commit()
    except BaseException as error:
        # A successful rollback after a COMMIT error does not prove non-commit.
        await db.rollback()
        if not commit_attempted:
            await redis_eval(RESTORE_PROOF, [proof_key], claim, digest)
            raise
        if isinstance(error, asyncio.CancelledError) or not await committed_password(db, user_id, password_hash):
            raise
    if await redis_eval(DELETE_CLAIM, [proof_key], claim) != 1:
        raise RecoveryError("Password changed but recovery cleanup could not be confirmed")
