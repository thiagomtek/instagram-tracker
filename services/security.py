"""Sign-in throttling stored in SQLite so it holds across Gunicorn workers."""

import hashlib
from datetime import timedelta

from flask import current_app

from database import db
from models.system import LoginThrottle
from services.clock import utc_now

FAILURE_WINDOW = timedelta(hours=1)


def _key(client, email):
    return hashlib.sha256(f"{client}|{email}".encode("utf-8")).hexdigest()


def seconds_locked(client, email):
    """Remaining lock time for this client and e-mail, or 0."""
    record = db.session.get(LoginThrottle, _key(client, email))
    if record is None or record.locked_until is None:
        return 0
    remaining = (record.locked_until - utc_now()).total_seconds()
    return max(0, int(remaining))


def register_failure(client, email):
    now = utc_now()
    key = _key(client, email)
    record = db.session.get(LoginThrottle, key)
    if record is None or now - record.first_failure_at > FAILURE_WINDOW:
        if record is None:
            record = LoginThrottle(key=key)
            db.session.add(record)
        record.failures = 0
        record.first_failure_at = now
        record.locked_until = None
    record.failures += 1
    if record.failures >= current_app.config["LOGIN_MAX_FAILURES"]:
        record.locked_until = now + timedelta(seconds=current_app.config["LOGIN_LOCK_SECONDS"])
        record.failures = 0
        record.first_failure_at = now
    db.session.commit()


def clear_failures(client, email):
    record = db.session.get(LoginThrottle, _key(client, email))
    if record is not None:
        db.session.delete(record)
        db.session.commit()
