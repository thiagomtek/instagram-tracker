import json

from database import db
from models.integration import AnalysisRun
from services.clock import utc_now


def start_analysis_run(user_id, source, source_name):
    run = AnalysisRun(
        user_id=user_id,
        source=source,
        source_name=source_name[:512],
        status="processing",
    )
    db.session.add(run)
    db.session.commit()
    return run


def complete_analysis_run(run, summary, cleanup_error=None):
    run.status = "success" if not cleanup_error else "cleanup_failed"
    run.completed_at = utc_now()
    run.files_read = summary.files_read
    run.followers_found = summary.followers_found
    run.following_found = summary.following_found
    run.followers_gained = summary.followers_gained
    run.followers_lost = summary.followers_lost
    run.followers_missing = summary.followers_missing
    run.followers_reappeared = summary.followers_reappeared
    run.followers_followed_again = summary.followers_followed_again
    run.validation_total = summary.validation_total
    # Validations that finished during the import (existence proven by the
    # opposite list) count as done; the worker adds the others later.
    run.validation_completed = min(
        summary.validation_total, max(run.validation_completed or 0, summary.validation_completed)
    )
    run.gained_usernames = json.dumps(summary.gained_usernames, ensure_ascii=False)
    run.lost_usernames = json.dumps(summary.missing_usernames, ensure_ascii=False)
    run.reappeared_usernames = json.dumps(summary.reappeared_usernames, ensure_ascii=False)
    run.followed_again_usernames = json.dumps(summary.followed_again_usernames, ensure_ascii=False)
    run.cleanup_error = cleanup_error
    db.session.commit()


def fail_analysis_run(run_id, error):
    db.session.rollback()
    run = db.session.get(AnalysisRun, run_id)
    if run:
        run.status = "failed"
        run.completed_at = utc_now()
        run.error = str(error)[:2000]
        db.session.commit()
