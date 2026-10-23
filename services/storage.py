from datetime import datetime

from database import db
from models.follower import Follower
from services.profile_validation import cancel_relationship_validations


def order_usernames_newest_first(usernames, dates_by_username):
    """Order usernames by relationship date, using the name only as a tie-breaker."""
    alphabetized = sorted(usernames, key=str.casefold)
    return sorted(
        alphabetized,
        key=lambda username: dates_by_username.get(username.casefold()) or datetime.min,
        reverse=True,
    )

def save_followers(kind, new_data_list, user_id, commit=True, observed_at=None):
    """Replace one relationship kind with the current Instagram snapshot.

    Dates inside Instagram's export are when each relationship started, not a
    boundary for a partial list. An all-time export can contain very old dates
    while still representing the exact relationships that are current today.
    """
    existing_items = Follower.query.filter_by(user_id=user_id, kind=kind).all()
    existing_by_key = {item.username.casefold(): item for item in existing_items}
    incoming_by_key = {item["username"].casefold(): item for item in new_data_list}
    existing_dates = {key: item.date_added for key, item in existing_by_key.items()}
    incoming_dates = {key: item["date"] for key, item in incoming_by_key.items()}
    is_baseline = not existing_items
    observed_at = observed_at or datetime.now()

    gained = []
    missing = []
    reappeared = []

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
            if not existing.is_present:
                reappeared.append(item["username"])
                cancel_relationship_validations(existing, observed_at)
            existing.username = item["username"]
            existing.date_added = item["date"]
            existing.is_present = True
            existing.missing_since = None
            existing.state = "present"
            existing.missing_count = 0
            existing.validation_status = "idle"
            existing.last_validation_result = None

    for key, follower in existing_by_key.items():
        if key in incoming_by_key:
            continue
        if follower.is_present:
            follower.is_present = False
            follower.missing_since = observed_at
            follower.state = "pending"
            follower.missing_count = 1
            follower.validation_status = "idle"
            missing.append(follower.username)
        else:
            follower.missing_count += 1

    if commit:
        db.session.commit()

    return (
        order_usernames_newest_first(gained, incoming_dates),
        order_usernames_newest_first(missing, existing_dates),
        order_usernames_newest_first(reappeared, incoming_dates),
    )

def load_followers(kind, user_id):
    """Return the latest exact snapshot for one relationship kind."""
    return [
        item.username
        for item in Follower.query.filter_by(kind=kind, user_id=user_id, is_present=True)
        .order_by(Follower.date_added.desc(), Follower.username.asc())
        .all()
    ]
