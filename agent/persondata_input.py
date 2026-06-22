import os
import re
from typing import Any

from common.config_utils import get_base_config
from common.datav_persondata_crypto import PersonDataCryptoError, decrypt_persondata_prompt


PERSONDATA_REDACTED_VALUE = "[PERSON_DATA_DECRYPTED]"
_PERSONDATA_TYPE_RE = re.compile(r"[\s_-]+")


def is_persondata_input(input_spec: Any) -> bool:
    if not isinstance(input_spec, dict):
        return False

    input_type = input_spec.get("type")
    if not isinstance(input_type, str):
        return False

    return _PERSONDATA_TYPE_RE.sub("", input_type).lower() == "persondata"


def _get_crypto_config() -> tuple[str, str]:
    cfg = get_base_config("datav_persondata_crypto", {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}

    aes_key_hex = os.getenv("DATAV_PERSONDATA_AES_KEY_HEX") or cfg.get("aes_key_hex")
    aes_iv_hex = os.getenv("DATAV_PERSONDATA_AES_IV_HEX") or cfg.get("aes_iv_hex")
    if not aes_key_hex or not aes_iv_hex:
        raise PersonDataCryptoError("person_data 解密失败，请配置 DATAV_PERSONDATA_AES_KEY_HEX/DATAV_PERSONDATA_AES_IV_HEX 或 datav_persondata_crypto.aes_key_hex/aes_iv_hex")

    return str(aes_key_hex), str(aes_iv_hex)


def decrypt_persondata_input_value(input_spec: dict[str, Any], canvas: Any | None = None) -> Any:
    raw_value = input_spec.get("value")
    if raw_value is None and input_spec.get("optional"):
        return None
    if isinstance(raw_value, list):
        # The current web runtime submits selected datav dataobject values here.
        # Only decrypt when the integration sends an encrypted prompt string.
        return raw_value
    if not isinstance(raw_value, str):
        raise PersonDataCryptoError("person_data 解密失败，输入 value 必须是 Base64 字符串或 dataobject 数组")

    aes_key_hex, aes_iv_hex = _get_crypto_config()
    plaintext = decrypt_persondata_prompt(raw_value, aes_key_hex, aes_iv_hex)

    if canvas is not None and hasattr(canvas, "register_sensitive_value"):
        canvas.register_sensitive_value(plaintext)

    return plaintext


def redact_persondata_inputs(value: Any) -> Any:
    if isinstance(value, dict):
        copied = {}
        for key, item in value.items():
            if key == "value" and is_persondata_input(value):
                copied[key] = PERSONDATA_REDACTED_VALUE
            else:
                copied[key] = redact_persondata_inputs(item)
        return copied

    if isinstance(value, list):
        return [redact_persondata_inputs(item) for item in value]

    return value


def redact_sensitive_values(value: Any, sensitive_values: list[str]) -> Any:
    if not sensitive_values:
        return value

    if isinstance(value, str):
        redacted = value
        for sensitive in sensitive_values:
            if not sensitive:
                continue
            if redacted == sensitive:
                redacted = PERSONDATA_REDACTED_VALUE
            elif len(sensitive) >= 8 and sensitive in redacted:
                redacted = redacted.replace(sensitive, PERSONDATA_REDACTED_VALUE)
        return redacted

    if isinstance(value, dict):
        return {key: redact_sensitive_values(item, sensitive_values) for key, item in value.items()}

    if isinstance(value, list):
        return [redact_sensitive_values(item, sensitive_values) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_sensitive_values(item, sensitive_values) for item in value)

    return value


def redact_persondata_payload(value: Any, sensitive_values: list[str]) -> Any:
    return redact_persondata_inputs(redact_sensitive_values(value, sensitive_values))
