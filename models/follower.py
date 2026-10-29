from database import db
from services.clock import local_now


class Follower(db.Model):
    """One relationship (`kind`) with one profile, in the user's current snapshot.

    ``state`` is the lifecycle of the relationship:
    present -> pending (missing from one export) -> absent (profile unavailable,
    one retest scheduled or still unavailable) -> removed (confirmed existence).
    A profile in any state returns to ``present`` when it shows up in an export.
    """

    __table_args__ = (
        db.UniqueConstraint("user_id", "kind", "username", name="uq_follower_user_kind_username"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    username = db.Column(db.String(255), nullable=False)
    kind = db.Column(db.String(32), nullable=False)
    date_added = db.Column(db.DateTime, default=local_now)
    is_present = db.Column(db.Boolean, nullable=False, default=True)
    missing_since = db.Column(db.DateTime, nullable=True)
    state = db.Column(db.String(32), nullable=False, default="present", index=True)
    missing_count = db.Column(db.Integer, nullable=False, default=0)
    validation_status = db.Column(db.String(32), nullable=False, default="idle")
    last_validation_result = db.Column(db.String(64), nullable=True)
    last_validated_at = db.Column(db.DateTime, nullable=True)
    retest_count = db.Column(db.Integer, nullable=False, default=0)
    avatar_data = db.Column(db.LargeBinary, nullable=True)
    avatar_mime = db.Column(db.String(64), nullable=True)
    profile_status = db.Column(db.String(32), nullable=False, default="unknown")
    profile_checked_at = db.Column(db.DateTime, nullable=True)
