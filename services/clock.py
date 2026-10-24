"""Single source of truth for the two datetime conventions used by the app.

* ``local_now()`` is a naive datetime in ``APP_TIMEZONE``. It is used for the
  relationship snapshot (``Follower`` and ``History``), whose dates come from
  Instagram's export and are compared with the day filters typed by the user.
* ``utc_now()`` is a naive UTC datetime. It is used for every operational
  timestamp (analysis runs, Drive sync, profile-validation scheduling).

Neither depends on the timezone of the container's operating system.
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def app_timezone():
    return ZoneInfo(os.environ.get("APP_TIMEZONE", "America/Sao_Paulo"))


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now():
    return datetime.now(app_timezone()).replace(tzinfo=None)


def utc_to_local(value):
    """Convert a naive UTC datetime to an aware datetime in ``APP_TIMEZONE``."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(app_timezone())
