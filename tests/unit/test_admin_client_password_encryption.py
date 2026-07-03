import importlib
from base64 import b64decode
from pathlib import Path

from api.utils.crypt import decrypt


def test_admin_client_encrypt_password_matches_server_private_key(monkeypatch):
    client_dir = Path(__file__).resolve().parents[2] / "admin" / "client"
    monkeypatch.syspath_prepend(str(client_dir))

    user_module = importlib.import_module("user")
    encrypted_password = user_module.encrypt_password("admin")

    assert b64decode(decrypt(encrypted_password)).decode("utf-8") == "admin"
