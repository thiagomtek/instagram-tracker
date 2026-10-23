import json
from datetime import datetime

from database import db
from models.integration import AnalysisRun


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
    run.completed_at = datetime.utcnow()
    run.files_read = summary.files_read
    run.followers_found = summary.followers_found
    run.following_found = summary.following_found
    run.followers_gained = summary.followers_gained
    run.followers_lost = summary.followers_lost
    run.gained_usernames = json.dumps(summary.gained_usernames, ensure_ascii=False)
    run.lost_usernames = json.dumps(summary.lost_usernames, ensure_ascii=False)
    run.cleanup_error = cleanup_error
    db.session.commit()


def fail_analysis_run(run_id, error):
    db.session.rollback()
    run = db.session.get(AnalysisRun, run_id)
    if run:
        run.status = "failed"
        run.completed_at = datetime.utcnow()
        run.error = str(error)[:2000]
        db.session.commit()
