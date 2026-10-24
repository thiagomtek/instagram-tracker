from werkzeug.security import check_password_hash, generate_password_hash

from database import db
from services.clock import utc_now


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(512), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    # Bumped on password change so every older login cookie stops working.
    session_epoch = db.Column(db.Integer, nullable=False, default=0)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
