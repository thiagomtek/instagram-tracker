import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)


def _flag(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    return int(os.environ.get(name, str(default)))


class Config:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(DATA_DIR, 'data.db')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Docker requires this environment variable. The local fallback prevents a
    # predictable secret from ever being committed, but invalidates sessions on
    # restart and differs between processes, so it is only suitable for development.
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    SECRET_KEY_IS_EPHEMERAL = not os.environ.get("SECRET_KEY")
    # Used only to create the first account when the user table is empty.
    INITIAL_ADMIN_EMAIL = os.environ.get("INITIAL_ADMIN_EMAIL", "")
    INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    # Closed by default. The very first account can always be created.
    ALLOW_REGISTRATION = _flag("ALLOW_REGISTRATION", False)
    MAX_CONTENT_LENGTH = _int("MAX_UPLOAD_BYTES", 512 * 1024 * 1024)

    # Long-lived signed login cookie. It remains valid across restarts as long as
    # SECRET_KEY remains stable and the browser does not clear site data.
    PERMANENT_SESSION_LIFETIME = timedelta(days=_int("SESSION_DAYS", 3650))
    SESSION_REFRESH_EACH_REQUEST = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE", False)

    # Sign-in throttling: after this many failures the pair (client, e-mail)
    # is locked for LOGIN_LOCK_SECONDS.
    LOGIN_MAX_FAILURES = _int("LOGIN_MAX_FAILURES", 5)
    LOGIN_LOCK_SECONDS = _int("LOGIN_LOCK_SECONDS", 900)

    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
    GOOGLE_TOKEN_ENCRYPTION_KEY = os.environ.get("GOOGLE_TOKEN_ENCRYPTION_KEY", "")
    GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
    # Product behavior: connected accounts are checked once per hour.
    GOOGLE_DRIVE_POLL_SECONDS = 60 * 60
    GOOGLE_DRIVE_SETTLE_SECONDS = max(60, _int("GOOGLE_DRIVE_SETTLE_SECONDS", 300))
    GOOGLE_DRIVE_MAX_ITEMS = _int("GOOGLE_DRIVE_MAX_ITEMS", 10000)
    # "trash" moves the processed export to the Drive trash (recoverable);
    # "delete" removes it permanently.
    GOOGLE_DRIVE_DELETE_MODE = "delete" if os.environ.get("GOOGLE_DRIVE_DELETE_MODE", "trash").strip().lower() == "delete" else "trash"

    # Import safeguards.
    # An import that would drop at least this share (and count) of the present
    # profiles of a list is rejected unless the user explicitly confirms it.
    IMPORT_MASS_DROP_RATIO = float(os.environ.get("IMPORT_MASS_DROP_RATIO", "0.3"))
    IMPORT_MASS_DROP_MIN = _int("IMPORT_MASS_DROP_MIN", 20)
    # The same snapshot sent again inside this window is ignored instead of
    # counting as another observation of the profiles that are missing.
    IMPORT_DUPLICATE_WINDOW_HOURS = _int("IMPORT_DUPLICATE_WINDOW_HOURS", 12)

    # Profile checks are deliberately serialized, delayed and jittered. This
    # keeps imports responsive and avoids bursts against Instagram.
    PROFILE_VALIDATION_ENABLED = _flag("PROFILE_VALIDATION_ENABLED", True)
    PROFILE_VALIDATION_INITIAL_MIN_SECONDS = _int("PROFILE_VALIDATION_INITIAL_MIN_SECONDS", 1800)
    PROFILE_VALIDATION_INITIAL_MAX_SECONDS = _int("PROFILE_VALIDATION_INITIAL_MAX_SECONDS", 10800)
    PROFILE_VALIDATION_GAP_MIN_SECONDS = _int("PROFILE_VALIDATION_GAP_MIN_SECONDS", 600)
    PROFILE_VALIDATION_GAP_MAX_SECONDS = _int("PROFILE_VALIDATION_GAP_MAX_SECONDS", 1800)
    PROFILE_VALIDATION_RETRY_MIN_SECONDS = _int("PROFILE_VALIDATION_RETRY_MIN_SECONDS", 21600)
    PROFILE_VALIDATION_RETRY_MAX_SECONDS = _int("PROFILE_VALIDATION_RETRY_MAX_SECONDS", 64800)
    PROFILE_VALIDATION_MAX_ATTEMPTS = _int("PROFILE_VALIDATION_MAX_ATTEMPTS", 3)
    PROFILE_VALIDATION_PAGE_TIMEOUT_MS = _int("PROFILE_VALIDATION_PAGE_TIMEOUT_MS", 45000)
    PROFILE_VALIDATION_RENDER_MIN_MS = _int("PROFILE_VALIDATION_RENDER_MIN_MS", 8000)
    PROFILE_VALIDATION_RENDER_MAX_MS = _int("PROFILE_VALIDATION_RENDER_MAX_MS", 20000)
    # A check still "running" after this long belongs to a dead worker.
    PROFILE_VALIDATION_STALE_SECONDS = _int("PROFILE_VALIDATION_STALE_SECONDS", 900)

    PUSH_VAPID_PUBLIC_KEY = os.environ.get("PUSH_VAPID_PUBLIC_KEY", "")
    PUSH_VAPID_PRIVATE_KEY = os.environ.get("PUSH_VAPID_PRIVATE_KEY", "")
    PUSH_VAPID_SUBJECT = os.environ.get("PUSH_VAPID_SUBJECT", "")

    PWA_ID = os.environ.get("PWA_ID", "/instagram-tracker-pwa")
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/Sao_Paulo")
    TRUST_PROXY_HEADERS = _flag("TRUST_PROXY_HEADERS", True)
