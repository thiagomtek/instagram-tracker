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
    source_fingerprint = db.Column(db.String(64), nullable=True)
    observed_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        db.UniqueConstraint("user_id", "kind", name="uq_import_observation_user_kind"),
    )


class NetworkStatus(db.Model):
    """Single row (id=1) describing how the worker reaches Instagram.

    The worker writes it; the web interface only reads it, except for
    ``allow_direct_fallback``, which the user flips from the interface.
    All timestamps are naive UTC.
    """

    id = db.Column(db.Integer, primary_key=True)
    vpn_configured = db.Column(db.Boolean, nullable=False, default=False)
    vpn_up = db.Column(db.Boolean, nullable=False, default=False)
    vpn_ip = db.Column(db.String(64), nullable=True)
    vpn_location = db.Column(db.String(255), nullable=True)
    direct_ip = db.Column(db.String(64), nullable=True)
    # "vpn" | "direct" | "paused" (VPN down and direct fallback not allowed)
    route = db.Column(db.String(16), nullable=False, default="direct")
    detail = db.Column(db.Text, nullable=True)
    allow_direct_fallback = db.Column(db.Boolean, nullable=False, default=True)
    # While set and in the future, Instagram has blocked the VPN address and the
    # direct network is tried instead; afterwards the VPN is used again.
    vpn_avoid_until = db.Column(db.DateTime, nullable=True)
    blocked_until = db.Column(db.DateTime, nullable=True)
    consecutive_blocks = db.Column(db.Integer, nullable=False, default=0)
    checked_at = db.Column(db.DateTime, nullable=True)
    route_changed_at = db.Column(db.DateTime, nullable=True)


class LoginThrottle(db.Model):
    """Failed sign-in counter, keyed by a hash of client address and e-mail."""

    key = db.Column(db.String(64), primary_key=True)
    failures = db.Column(db.Integer, nullable=False, default=0)
    first_failure_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    locked_until = db.Column(db.DateTime, nullable=True)
