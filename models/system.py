from database import db
from services.clock import utc_now


class SchemaMigration(db.Model):
    """Names of the one-off data migrations that already ran."""

    name = db.Column(db.String(128), primary_key=True)
    applied_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ImportObservation(db.Model):
    """Fingerprint of the last snapshot imported for each user and kind.

    It lets the importer recognise the same export being sent twice, which must
    not count as a second observation of a missing profile.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    kind = db.Column(db.String(32), nullable=False)
    fingerprint = db.Column(db.String(64), nullable=False)
    observed_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        db.UniqueConstraint("user_id", "kind", name="uq_import_observation_user_kind"),
    )


class LoginThrottle(db.Model):
    """Failed sign-in counter, keyed by a hash of client address and e-mail."""

    key = db.Column(db.String(64), primary_key=True)
    failures = db.Column(db.Integer, nullable=False, default=0)
    first_failure_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    locked_until = db.Column(db.DateTime, nullable=True)
