import logging
import signal
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)

from app import app
from database import db
from services.google_drive import sync_all_connections
from services.profile_validation import process_next_validation, seconds_until_next_validation

HEARTBEAT_FILE = Path("/tmp/worker-heartbeat")
LOOP_SLEEP_SECONDS = 5

running = True


def stop_worker(_signum, _frame):
    global running
    running = False


signal.signal(signal.SIGTERM, stop_worker)
signal.signal(signal.SIGINT, stop_worker)


def guarded(step_name, step):
    """Run one step; a failure is logged and rolled back instead of killing the worker."""
    try:
        return step()
    except Exception:
        db.session.rollback()
        app.logger.exception("Falha na etapa '%s' do worker", step_name)
        return None


def beat():
    try:
        HEARTBEAT_FILE.touch()
    except OSError:
        pass


if __name__ == "__main__":
    drive_interval = app.config["GOOGLE_DRIVE_POLL_SECONDS"]
    next_drive_check = 0
    next_profile_check = 0
    app.logger.info("Worker iniciado: Google Drive a cada 1 hora e validações assíncronas serializadas")
    while running:
        beat()
        now = time.monotonic()
        with app.app_context():
            if now >= next_drive_check:
                guarded("google-drive", sync_all_connections)
                next_drive_check = time.monotonic() + drive_interval
            else:
                guarded("google-drive-solicitado", lambda: sync_all_connections(only_requested=True))
            if now >= next_profile_check:
                if app.config["PROFILE_VALIDATION_ENABLED"]:
                    processed = guarded("validacao-de-perfil", process_next_validation)
                    next_profile_check = time.monotonic() + (seconds_until_next_validation() if processed else 60)
                else:
                    next_profile_check = time.monotonic() + 300
            db.session.remove()
        beat()
        time.sleep(LOOP_SLEEP_SECONDS)
