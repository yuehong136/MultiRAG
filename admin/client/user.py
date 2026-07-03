import base64

from http_client import HttpClient

PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArq9XTUSeYr2+N1h3Afl/
z8Dse/2yD0ZGrKwx+EEEcdsBLca9Ynmx3nIB5obmLlSfmskLpBo0UACBmB5rEjBp
2Q2f3AG3Hjd4B+gNCG6BDaawuDlgANIhGnaTLrIqWrrcm4EMzJOnAOI1fgzJRsOO
UEfaS318Eq9OVO3apEyCCt0lOQK6PuksduOjVxtltDav+guVAA068NrPYmRNabVK
RNLJpL8w4D44sfth5RvZ3q9t+6RTArpEtc5sh5ChzvqPOzKGMXW83C95TxmXqpbK
6olN4RevSfVjEAgCydH6HN6OhtOQEcnrU97r9H0iZOWwbw3pVrZiUkuRD1R56Wzs
2wIDAQAB
-----END PUBLIC KEY-----"""


class AuthException(Exception):
    def __init__(self, message, code=401):
        super().__init__(message)
        self.code = code
        self.message = message


def encrypt_password(password_plain: str) -> str:
    try:
        from Cryptodome.Cipher import PKCS1_v1_5 as Cipher_pkcs1_v1_5
        from Cryptodome.PublicKey import RSA
    except Exception as exc:
        raise AuthException("Password encryption unavailable; install pycryptodomex (uv sync --python 3.12 --group test).") from exc

    rsa_key = RSA.importKey(PUBLIC_KEY_PEM)
    cipher = Cipher_pkcs1_v1_5.new(rsa_key)
    password_base64 = base64.b64encode(password_plain.encode("utf-8"))
    encrypted_password = cipher.encrypt(password_base64)
    return base64.b64encode(encrypted_password).decode("utf-8")


def login_user(client: HttpClient, server_type: str, email: str, password: str) -> str:
    password_enc = encrypt_password(password)
    if server_type == "admin":
        payload = {"email": email, "password": password_enc}
        response = client.request("POST", "/admin/login", use_api_base=True, auth_kind=None, json_body=payload)
    else:
        payload = {"username": email, "password": password_enc}
        response = client.request("POST", "/user/login", use_api_base=False, auth_kind=None, json_body=payload)
    try:
        res = response.json()
    except Exception as exc:
        raise AuthException(f"Login failed: invalid JSON response ({exc})") from exc
    # user-mode API uses retcode/retmsg; admin-mode uses code/message
    error_code = res.get("code", res.get("retcode", -1))
    if error_code != 0:
        error_msg = res.get("message", res.get("retmsg", "Unknown error"))
        raise AuthException(f"Login failed: {error_msg}")
    token = response.headers.get("Authorization")
    if not token:
        raise AuthException("Login failed: missing Authorization header")
    return token


def register_user(client: HttpClient, email: str, nickname: str, password: str) -> None:
    payload = {"email": email, "nickname": nickname, "password": encrypt_password(password)}
    res = client.request_json("POST", "/user/register", use_api_base=False, auth_kind=None, json_body=payload)
    if res.get("code", res.get("retcode", -1)) == 0:
        return
    msg = res.get("message", res.get("retmsg", ""))
    if "has already registered" in msg:
        return
    raise AuthException(f"Register failed: {msg}")
