from datetime import datetime

from database import db


class History(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    username = db.Column(db.String, nullable=False)
    action = db.Column(db.String, nullable=False)
    kind = db.Column(db.String, nullable=False)
    date_added = db.Column(db.DateTime, default=datetime.now)
