import os
import tempfile

# Must run before any test module imports `app`, which opens the database.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="instagram-tracker-tests-"))
os.environ.setdefault("SECRET_KEY", "tests-only-secret-key")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("APP_TIMEZONE", "America/Sao_Paulo")
