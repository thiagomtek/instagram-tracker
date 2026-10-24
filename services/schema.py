"""Small, idempotent SQLite upgrades for databases created by earlier releases.

Structural changes (missing columns and indexes) are safe to repeat. Data
changes run once and are recorded in ``schema_migration`` so they can never
rewrite newer data on a later start.
"""

import logging

from sqlalchemy.exc import OperationalError

from database import db
from models.system import SchemaMigration

logger = logging.getLogger(__name__)

FOLLOWER_COLUMNS = (
    ("user_id", "INTEGER"),
    ("is_present", "BOOLEAN NOT NULL DEFAULT 1"),
    ("missing_since", "DATETIME"),
    ("state", "VARCHAR(32) NOT NULL DEFAULT 'present'"),
    ("missing_count", "INTEGER NOT NULL DEFAULT 0"),
    ("validation_status", "VARCHAR(32) NOT NULL DEFAULT 'idle'"),
    ("last_validation_result", "VARCHAR(64)"),
    ("last_validated_at", "DATETIME"),
    ("retest_count", "INTEGER NOT NULL DEFAULT 0"),
)
HISTORY_COLUMNS = (("user_id", "INTEGER"),)
RUN_COLUMNS = (
    ("followers_reappeared", "INTEGER NOT NULL DEFAULT 0"),
    ("followers_followed_again", "INTEGER NOT NULL DEFAULT 0"),
    ("reappeared_usernames", "TEXT"),
    ("followed_again_usernames", "TEXT"),
    ("followers_missing", "INTEGER NOT NULL DEFAULT 0"),
    ("validation_total", "INTEGER NOT NULL DEFAULT 0"),
    ("validation_completed", "INTEGER NOT NULL DEFAULT 0"),
)
USER_COLUMNS = (("session_epoch", "INTEGER NOT NULL DEFAULT 0"),)
DRIVE_COLUMNS = (("sync_requested", "BOOLEAN NOT NULL DEFAULT 0"),)

DUPLICATE_FOLLOWER = (
    "EXISTS (SELECT 1 FROM follower g WHERE g.user_id IS f.user_id AND g.kind = f.kind "
    "AND g.username = f.username AND g.id < f.id)"
)


def _columns(table):
    return {column["name"] for column in db.inspect(db.engine).get_columns(table)}


def _add_missing_columns(table, definitions):
    existing = _columns(table)
    for name, ddl in definitions:
        if name not in existing:
            db.session.execute(db.text(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}'))
            logger.info("Coluna %s.%s adicionada", table, name)
    db.session.commit()


def _has_unique_follower_key():
    inspector = db.inspect(db.engine)
    wanted = ["user_id", "kind", "username"]
    constraints = inspector.get_unique_constraints("follower")
    indexes = [index for index in inspector.get_indexes("follower") if index.get("unique")]
    return any(item["column_names"] == wanted for item in constraints + indexes)


def _create_index(statement):
    try:
        db.session.execute(db.text(statement))
        db.session.commit()
    except OperationalError:
        db.session.rollback()
        logger.exception("Não foi possível criar o índice: %s", statement)


def _run_once(name, action, *, skip=False):
    if db.session.get(SchemaMigration, name):
        return
    if not skip:
        action()
    db.session.add(SchemaMigration(name=name))
    db.session.commit()


def _legacy_lost_to_missing():
    # Releases before profile validation called "no longer in the list" `lost`.
    # Today `lost` means a confirmed removal, so this must never run again.
    db.session.execute(db.text("UPDATE history SET action = 'missing' WHERE action = 'lost'"))


def _dedupe_followers():
    db.session.execute(db.text(
        f"DELETE FROM relationship_validation WHERE follower_id IN (SELECT id FROM follower f WHERE {DUPLICATE_FOLLOWER})"
    ))
    db.session.execute(db.text(f"DELETE FROM follower WHERE id IN (SELECT id FROM follower f WHERE {DUPLICATE_FOLLOWER})"))


def run_schema_upgrades():
    is_legacy_database = "state" not in _columns("follower")

    _add_missing_columns("follower", FOLLOWER_COLUMNS)
    _add_missing_columns("history", HISTORY_COLUMNS)
    _add_missing_columns("analysis_run", RUN_COLUMNS)
    _add_missing_columns("user", USER_COLUMNS)
    _add_missing_columns("drive_connection", DRIVE_COLUMNS)

    db.session.execute(db.text("UPDATE follower SET is_present = 1 WHERE is_present IS NULL"))
    db.session.execute(db.text("UPDATE follower SET state = 'present' WHERE state IS NULL"))
    db.session.execute(db.text("UPDATE follower SET missing_count = 0 WHERE missing_count IS NULL"))
    db.session.execute(db.text("UPDATE follower SET validation_status = 'idle' WHERE validation_status IS NULL"))
    db.session.commit()

    _run_once("legacy_lost_to_missing", _legacy_lost_to_missing, skip=not is_legacy_database)
    _run_once("dedupe_followers_v1", _dedupe_followers)

    statements = [
        "CREATE INDEX IF NOT EXISTS ix_follower_user_id ON follower (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_history_user_id ON history (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_follower_presence ON follower (user_id, kind, is_present)",
        "CREATE INDEX IF NOT EXISTS ix_history_user_kind_date ON history (user_id, kind, date_added)",
    ]
    if not _has_unique_follower_key():
        statements.append(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_follower_user_kind_username ON follower (user_id, kind, username)"
        )
    for statement in statements:
        _create_index(statement)
