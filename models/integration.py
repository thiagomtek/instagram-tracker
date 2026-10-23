from datetime import datetime

from database import db


class DriveConnection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    google_email = db.Column(db.String(255), nullable=True)
    encrypted_refresh_token = db.Column(db.Text, nullable=False)
    folder_id = db.Column(db.String(255), nullable=True)
    folder_name = db.Column(db.String(512), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    connected_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_checked_at = db.Column(db.DateTime, nullable=True)
    last_success_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    last_import_summary = db.Column(db.Text, nullable=True)


class DriveImport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey("drive_connection.id"), nullable=False, index=True)
    source_id = db.Column(db.String(255), nullable=False)
    source_version = db.Column(db.String(128), nullable=False)
    source_name = db.Column(db.String(512), nullable=False)
    analysis_run_id = db.Column(db.Integer, db.ForeignKey("analysis_run.id"), nullable=True)
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    summary = db.Column(db.Text, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deletion_error = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("connection_id", "source_id", "source_version", name="uq_drive_import_version"),
    )


class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.Text, nullable=False)
    auth = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AnalysisRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    source = db.Column(db.String(32), nullable=False)
    source_name = db.Column(db.String(512), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="processing")
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    files_read = db.Column(db.Integer, nullable=False, default=0)
    followers_found = db.Column(db.Integer, nullable=False, default=0)
    following_found = db.Column(db.Integer, nullable=False, default=0)
    followers_gained = db.Column(db.Integer, nullable=False, default=0)
    followers_lost = db.Column(db.Integer, nullable=False, default=0)
    followers_missing = db.Column(db.Integer, nullable=False, default=0)
    followers_reappeared = db.Column(db.Integer, nullable=False, default=0)
    validation_total = db.Column(db.Integer, nullable=False, default=0)
    validation_completed = db.Column(db.Integer, nullable=False, default=0)
    gained_usernames = db.Column(db.Text, nullable=True)
    lost_usernames = db.Column(db.Text, nullable=True)
    reappeared_usernames = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    cleanup_error = db.Column(db.Text, nullable=True)


class RelationshipValidation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    analysis_run_id = db.Column(db.Integer, db.ForeignKey("analysis_run.id"), nullable=True, index=True)
    follower_id = db.Column(db.Integer, db.ForeignKey("follower.id"), nullable=False, index=True)
    username = db.Column(db.String, nullable=False)
    kind = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="queued", index=True)
    outcome = db.Column(db.String(64), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    missing_count_snapshot = db.Column(db.Integer, nullable=False, default=1)
    next_attempt_at = db.Column(db.DateTime, nullable=False, index=True)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    notification_sent_at = db.Column(db.DateTime, nullable=True)
    details = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
