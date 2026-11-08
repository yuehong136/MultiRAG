"""LINE HMAC and WeCom authenticated AES envelopes, without provider SDKs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml.ElementTree import fromstring
from pydantic import ValidationError

from api.channel_runtime.callbacks import CallbackMessage


class CallbackRejected(ValueError):
    """Classified rejection; never contains credentials, event bodies or IDs."""


def _message(**values: Any) -> CallbackMessage:
    try:
        return CallbackMessage.model_validate(values)
    except ValidationError:
        raise CallbackRejected("CHANNEL_CALLBACK_INVALID") from None


def line_messages(body: bytes, signature: str, credential: Mapping[str, str]) -> list[CallbackMessage]:
    expected = base64.b64encode(hmac.digest(credential["channel_secret"].encode(), body, "sha256")).decode()
    if not hmac.compare_digest(expected, signature):
        raise CallbackRejected("CHANNEL_CALLBACK_SIGNATURE_INVALID")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise CallbackRejected("CHANNEL_CALLBACK_INVALID") from None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list) or len(data["events"]) > 100:
        raise CallbackRejected("CHANNEL_CALLBACK_INVALID")
    messages = []
    for event in data["events"]:
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        content, source = event.get("message"), event.get("source")
        if not isinstance(content, dict) or content.get("type") != "text" or not isinstance(source, dict):
            continue
        source_type = source.get("type")
        if source_type not in {"user", "group", "room"}:
            continue
        chat_id = source.get({"user": "userId", "group": "groupId", "room": "roomId"}[source_type])
        messages.append(_message(message_id=content.get("id"), sender_id=source.get("userId"), chat_id=chat_id, chat_type="p2p" if source_type == "user" else "group", text=content.get("text")))
    return messages


def wecom_decrypt(encrypted: str, query: Mapping[str, str], credential: Mapping[str, str]) -> bytes:
    """Verify before decrypting; enforce timestamp, PKCS#7(32), length and corp."""
    timestamp, nonce, signature = (query.get(key, "") for key in ("timestamp", "nonce", "msg_signature"))
    if not timestamp.isdecimal() or abs(time.time() - int(timestamp)) > 300 or not nonce or len(nonce) > 255:
        raise CallbackRejected("CHANNEL_CALLBACK_INVALID")
    expected = hashlib.sha1("".join(sorted((credential["callback_token"], timestamp, nonce, encrypted))).encode()).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise CallbackRejected("CHANNEL_CALLBACK_SIGNATURE_INVALID")
    try:
        key = base64.b64decode(credential["aes_key"] + "=", validate=True)
        ciphertext = base64.b64decode(encrypted, validate=True)
        if len(key) != 32 or not ciphertext or len(ciphertext) % 16:
            raise ValueError
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad = padded[-1]
        if pad < 1 or pad > 32 or padded[-pad:] != bytes([pad]) * pad:
            raise ValueError
        plaintext = padded[:-pad]
        if len(plaintext) < 20:
            raise ValueError
        size = int.from_bytes(plaintext[16:20], "big")
        if size > len(plaintext) - 20 or plaintext[20 + size :] != credential["corp_id"].encode():
            raise ValueError
        return plaintext[20 : 20 + size]
    except (ValueError, binascii.Error, KeyError):
        raise CallbackRejected("CHANNEL_CALLBACK_INVALID") from None


def wecom_messages(body: bytes, query: Mapping[str, str], credential: Mapping[str, str]) -> list[CallbackMessage]:
    try:
        envelope = fromstring(body)
        encrypted = envelope.findtext("Encrypt")
        if not encrypted:
            raise ValueError
        message = fromstring(wecom_decrypt(encrypted, query, credential))
        if message.findtext("ToUserName") != credential["corp_id"] or message.findtext("AgentID") != credential["agent_id"]:
            raise ValueError
        if message.findtext("MsgType") != "text":
            return []
        sender = message.findtext("FromUserName")
        return [_message(message_id=message.findtext("MsgId"), sender_id=sender, chat_id=sender, chat_type="p2p", text=message.findtext("Content"))]
    except CallbackRejected:
        raise
    except Exception:
        # XML parsers include input in diagnostics. Keep it off the public
        # error surface and out of logs, including DTD/entity rejections.
        raise CallbackRejected("CHANNEL_CALLBACK_INVALID") from None
