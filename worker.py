import logging
import signal
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)

from app import app
from services.google_drive import sync_all_connections


running = True


def stop_worker(_signum, _frame):
    global running
    running = False


signal.signal(signal.SIGTERM, stop_worker)
signal.signal(signal.SIGINT, stop_worker)


if __name__ == "__main__":
    interval = app.config["GOOGLE_DRIVE_POLL_SECONDS"]
    app.logger.info("Worker do Google Drive iniciado; intervalo de %s segundos", interval)
    while running:
        with app.app_context():
            sync_all_connections()
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)
