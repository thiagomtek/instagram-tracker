import base64
import hashlib

from cryptography.fernet import Fernet
from flask import current_app


def _fernet():
    configured_key = current_app.config.get("GOOGLE_TOKEN_ENCRYPTION_KEY", "").strip()
    if configured_key:
        key = configured_key.encode("utf-8")
    else:
        # A stable SECRET_KEY is already mandatory in Docker. A derived key keeps
        # refresh tokens encrypted even when a separate key was not configured.
        secret = current_app.config["SECRET_KEY"]
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret + b":google-drive").digest())
    return Fernet(key)


def encrypt_token(token):
    return _fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token):
    return _fernet().decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
