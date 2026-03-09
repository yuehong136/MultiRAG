from http_client import HttpClient


class AuthException(Exception):
    def __init__(self, message, code=401):
        super().__init__(message)
        self.code = code
        self.message = message


def encrypt_password(password_plain: str) -> str:
    try:
        from api.utils.crypt import crypt
    except Exception as exc:
        raise AuthException(
            "Password encryption unavailable; install pycryptodomex (uv sync --python 3.12 --group test)."
        ) from exc
    return crypt(password_plain)


def login_user(client: HttpClient, server_type: str, email: str, password: str) -> str:
    if server_type == "admin":
        payload = {"email": email, "password": encrypt_password(password)}
        response = client.request("POST", "/admin/login", use_api_base=True, auth_kind=None, json_body=payload)
    else:
        payload = {"username": email, "password": password}
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
    payload = {"email": email, "nickname": nickname, "password": password}
    res = client.request_json("POST", "/user/register", use_api_base=False, auth_kind=None, json_body=payload)
    if res.get("code", res.get("retcode", -1)) == 0:
        return
    msg = res.get("message", res.get("retmsg", ""))
    if "has already registered" in msg:
        return
    raise AuthException(f"Register failed: {msg}")
