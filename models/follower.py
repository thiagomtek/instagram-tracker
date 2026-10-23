from database import db
from datetime import datetime

class Follower(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    username = db.Column(db.String, nullable=False)
    kind = db.Column(db.String, nullable=False)  
    date_added = db.Column(db.DateTime, default=datetime.utcnow)
    is_present = db.Column(db.Boolean, nullable=False, default=True)
    missing_since = db.Column(db.DateTime, nullable=True)
    state = db.Column(db.String(32), nullable=False, default="present", index=True)
    missing_count = db.Column(db.Integer, nullable=False, default=0)
    validation_status = db.Column(db.String(32), nullable=False, default="idle")
    last_validation_result = db.Column(db.String(32), nullable=True)
    last_validated_at = db.Column(db.DateTime, nullable=True)
    retest_count = db.Column(db.Integer, nullable=False, default=0)
