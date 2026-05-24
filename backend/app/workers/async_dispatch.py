"""Post-request import/export dispatch: avoids SQLite transaction races with a short delay."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import update

from backend.app.db.models import DatasetImportTask
from backend.app.services.export_jobs import run_directory_export_job, run_patient_export_job
from backend.app.services.import_pipeline import run_import_task
from backend.app.storage.backend import StorageBackend

_log = logging.getLogger(__name__)

import threading

_lazy_dispatch_lock = threading.Lock()
_last_lazy_dispatch_at: dict[str, float] = {}
_LAZY_DISPATCH_MIN_INTERVAL_S = 5.0

# Non-daemon pool: long imports must not be dropped when the HTTP handler returns.
# One import at a time: multi‑GiB FTP fetch must not compete for workers / disk.
_import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dataset-import")
_export_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dataset-export")


def _mark_import_dispatched(import_task_id: str) -> None:
    """Visible in DB immediately so polls / recovery know dispatch was attempted."""
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    db = factory()
    try:
        db.execute(
            update(DatasetImportTask)
            .where(
                DatasetImportTask.import_task_id == import_task_id,
                DatasetImportTask.status == "IMPORTING",
                DatasetImportTask.stage.in_(("QUEUED", "DISPATCHED")),
                DatasetImportTask.progress.in_((0, 1)),
            )
            .values(progress=1, stage="DISPATCHED")
        )
        db.commit()
    finally:
        db.close()


def run_import_after_commit(storage: StorageBackend, import_task_id: str, delay_s: float = 0.05) -> None:
    """Run import in a worker thread after HTTP session commit (local/dev-friendly)."""
    _mark_import_dispatched(import_task_id)
    _log.info("import dispatch submit task=%s delay_s=%s", import_task_id, delay_s)

    def _job() -> None:
        try:
            if delay_s > 0:
                time.sleep(delay_s)
            run_import_task(storage, import_task_id)
        except Exception:
            _log.exception("import worker thread failed task=%s", import_task_id)

    _import_executor.submit(_job)


def maybe_lazy_redispatch_import(storage: StorageBackend, import_task_id: str) -> None:
    """If the initial worker never claimed the task, polling can trigger a throttled redispatch."""
    now = time.time()
    with _lazy_dispatch_lock:
        if now - _last_lazy_dispatch_at.get(import_task_id, 0) < _LAZY_DISPATCH_MIN_INTERVAL_S:
            return
        _last_lazy_dispatch_at[import_task_id] = now
    _log.info("lazy redispatch import task %s (poll-triggered)", import_task_id)
    run_import_after_commit(storage, import_task_id, delay_s=0.05)


def run_directory_export_after_commit(storage: StorageBackend, export_record_id: str, delay_s: float = 0.05) -> None:
    def _job() -> None:
        try:
            if delay_s > 0:
                time.sleep(delay_s)
            run_directory_export_job(storage, export_record_id)
        except Exception:
            _log.exception("directory export worker failed export=%s", export_record_id)

    _export_executor.submit(_job)


def run_patient_export_after_commit(storage: StorageBackend, export_record_id: str, delay_s: float = 0.05) -> None:
    def _job() -> None:
        try:
            if delay_s > 0:
                time.sleep(delay_s)
            run_patient_export_job(storage, export_record_id)
        except Exception:
            _log.exception("patient export worker failed export=%s", export_record_id)

    _export_executor.submit(_job)
