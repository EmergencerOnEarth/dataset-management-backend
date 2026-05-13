"""Post-request import/export dispatch: avoids SQLite transaction races with a short delay."""

from __future__ import annotations

import threading
import time

from backend.app.services.export_jobs import run_directory_export_job, run_patient_export_job
from backend.app.services.import_pipeline import run_import_task
from backend.app.storage.backend import StorageBackend


def run_import_after_commit(storage: StorageBackend, import_task_id: str, delay_s: float = 0.05) -> None:
    """Run import in a worker thread after HTTP session commit (local/dev-friendly)."""

    def _job() -> None:
        time.sleep(delay_s)
        run_import_task(storage, import_task_id)

    threading.Thread(target=_job, daemon=True).start()


def run_directory_export_after_commit(storage: StorageBackend, export_record_id: str, delay_s: float = 0.05) -> None:
    def _job() -> None:
        time.sleep(delay_s)
        run_directory_export_job(storage, export_record_id)

    threading.Thread(target=_job, daemon=True).start()


def run_patient_export_after_commit(storage: StorageBackend, export_record_id: str, delay_s: float = 0.05) -> None:
    def _job() -> None:
        time.sleep(delay_s)
        run_patient_export_job(storage, export_record_id)

    threading.Thread(target=_job, daemon=True).start()
