import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

class Config:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(DATA_DIR, 'data.db')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Docker requires this environment variable. The local fallback prevents a
    # predictable secret from ever being committed, but invalidates sessions on restart.
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    # Used only to create the first account when the user table is empty.
    INITIAL_ADMIN_EMAIL = os.environ.get("INITIAL_ADMIN_EMAIL", "")
    INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "true").lower() == "true"
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))

    # Long-lived signed login cookie. It remains valid across restarts as long as
    # SECRET_KEY remains stable and the browser does not clear site data.
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.environ.get("SESSION_DAYS", "3650")))
    SESSION_REFRESH_EACH_REQUEST = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
    GOOGLE_TOKEN_ENCRYPTION_KEY = os.environ.get("GOOGLE_TOKEN_ENCRYPTION_KEY", "")
    GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
    # Product behavior: connected accounts are checked once per hour.
    GOOGLE_DRIVE_POLL_SECONDS = 60 * 60
    GOOGLE_DRIVE_SETTLE_SECONDS = max(60, int(os.environ.get("GOOGLE_DRIVE_SETTLE_SECONDS", "300")))
    GOOGLE_DRIVE_MAX_ITEMS = int(os.environ.get("GOOGLE_DRIVE_MAX_ITEMS", "10000"))

    PUSH_VAPID_PUBLIC_KEY = os.environ.get("PUSH_VAPID_PUBLIC_KEY", "")
    PUSH_VAPID_PRIVATE_KEY = os.environ.get("PUSH_VAPID_PRIVATE_KEY", "")
    PUSH_VAPID_SUBJECT = os.environ.get("PUSH_VAPID_SUBJECT", "")

    PWA_ID = os.environ.get("PWA_ID", "/instagram-tracker-pwa")
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/Sao_Paulo")
    TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "true").lower() == "true"
