import base64
import binascii

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class PersonDataCryptoError(ValueError):
    """Raised when datav person_data payload decryption fails."""


def _decode_hex(value: str, expected_len: int, name: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise PersonDataCryptoError(f"{name} 不能为空")

    try:
        decoded = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise PersonDataCryptoError(f"{name} 必须是 Hex 格式") from exc

    if len(decoded) != expected_len:
        raise PersonDataCryptoError(f"{name} 长度必须是 {expected_len} 字节")

    return decoded


def decrypt_persondata_prompt(encrypted_prompt: str, aes_key_hex: str, aes_iv_hex: str) -> str:
    """
    Decrypt datav person_data prompt encrypted by Java AES/CBC/PKCS5Padding.

    Java PKCS5Padding is compatible with PKCS7 for AES's 16-byte block size.
    The encrypted prompt is Base64 encoded ciphertext.
    """
    if not isinstance(encrypted_prompt, str) or not encrypted_prompt.strip():
        raise PersonDataCryptoError("person_data 密文不能为空")

    key = _decode_hex(aes_key_hex, 32, "person_data AES_KEY")
    iv = _decode_hex(aes_iv_hex, 16, "person_data AES_IV")

    try:
        ciphertext = base64.b64decode(encrypted_prompt.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PersonDataCryptoError("person_data 密文必须是 Base64 格式") from exc

    if not ciphertext or len(ciphertext) % 16 != 0:
        raise PersonDataCryptoError("person_data 密文长度不是有效的 AES 块大小")

    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return plaintext.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise PersonDataCryptoError("person_data 解密失败，请检查 AES key/iv 或密文格式") from exc
