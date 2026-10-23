import logging
import signal
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)

from app import app
from services.google_drive import sync_all_connections
from services.profile_validation import process_next_validation, seconds_until_next_validation


running = True


def stop_worker(_signum, _frame):
    global running
    running = False


signal.signal(signal.SIGTERM, stop_worker)
signal.signal(signal.SIGINT, stop_worker)


if __name__ == "__main__":
    drive_interval = app.config["GOOGLE_DRIVE_POLL_SECONDS"]
    next_drive_check = 0
    next_profile_check = 0
    app.logger.info("Worker iniciado: Google Drive a cada 1 hora e validações assíncronas serializadas")
    while running:
        now = time.monotonic()
        with app.app_context():
            if now >= next_drive_check:
                sync_all_connections()
                next_drive_check = time.monotonic() + drive_interval
            if now >= next_profile_check:
                processed = process_next_validation()
                next_profile_check = time.monotonic() + (seconds_until_next_validation() if processed else 60)
        time.sleep(5)
