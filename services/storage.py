from database import db
from models.follower import Follower

def save_followers(kind, new_data_list, user_id, commit=True):
    """Replace one relationship kind with the current Instagram snapshot.

    Dates inside Instagram's export are when each relationship started, not a
    boundary for a partial list. An all-time export can contain very old dates
    while still representing the exact relationships that are current today.
    """
    existing_items = Follower.query.filter_by(user_id=user_id, kind=kind).all()
    existing_by_key = {item.username.casefold(): item for item in existing_items}
    incoming_by_key = {item["username"].casefold(): item for item in new_data_list}
    is_baseline = not existing_items

    gained = []
    lost = []

    for key, item in incoming_by_key.items():
        existing = existing_by_key.get(key)
        if existing is None:
            db.session.add(
                Follower(
                    user_id=user_id,
                    username=item["username"],
                    kind=kind,
                    date_added=item["date"],
                )
            )
            if not is_baseline:
                gained.append(item["username"])
        else:
            existing.username = item["username"]
            existing.date_added = item["date"]

    for key, follower in existing_by_key.items():
        if key not in incoming_by_key:
            db.session.delete(follower)
            lost.append(follower.username)

    if commit:
        db.session.commit()
    
    return gained, lost

def load_followers(kind, user_id):
    """Return the latest exact snapshot for one relationship kind."""
    return [
        item.username
        for item in Follower.query.filter_by(kind=kind, user_id=user_id)
        .order_by(Follower.username.asc())
        .all()
    ]
