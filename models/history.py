from database import db
from services.clock import local_now


class History(db.Model):
    """Change observed between two imports.

    ``action`` is one of gained, missing (absent, awaiting validation), lost
    (removal confirmed), reappeared or followed_again.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    username = db.Column(db.String(255), nullable=False)
    action = db.Column(db.String(32), nullable=False)
    kind = db.Column(db.String(32), nullable=False)
    date_added = db.Column(db.DateTime, default=local_now)

    __table_args__ = (
        db.Index("ix_history_user_kind_date", "user_id", "kind", "date_added"),
    )
