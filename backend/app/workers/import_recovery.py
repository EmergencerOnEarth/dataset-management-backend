"""Recover import tasks stuck after worker loss or hung FTP transfer."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select, update

from backend.app.core.config import get_settings
from backend.app.db.models import DatasetImportTask
from backend.app.db.session import get_session_factory
from backend.app.services.import_pipeline import is_import_task_active
from backend.app.storage.backend import get_storage
from backend.app.workers.async_dispatch import run_import_after_commit

logger = logging.getLogger(__name__)

_STALE_DISPATCH_SECONDS = 180


def recover_stalled_import_tasks_on_startup() -> None:
    """Re-dispatch imports that never left the queue or died after WORKER_CLAIMED."""
    factory = get_session_factory()
    db = factory()
    try:
        now = dt.datetime.now()
        stale_before = now - dt.timedelta(seconds=_STALE_DISPATCH_SECONDS)

        queued_ids = list(
            db.scalars(
                select(DatasetImportTask.import_task_id).where(
                    DatasetImportTask.status == "IMPORTING",
                    DatasetImportTask.stage.in_(("QUEUED", "DISPATCHED")),
                    DatasetImportTask.progress < 5,
                )
            ).all()
        )

        stale_claimed_ids = list(
            db.scalars(
                select(DatasetImportTask.import_task_id).where(
                    DatasetImportTask.status == "IMPORTING",
                    DatasetImportTask.stage.in_(
                        ("WORKER_CLAIMED", "READ_ZIP", "FETCH_SOURCE", "UNZIP")
                    ),
                    DatasetImportTask.progress < 15,
                    DatasetImportTask.updated_at < stale_before,
                )
            ).all()
        )

        for tid in stale_claimed_ids:
            if is_import_task_active(tid):
                continue
            db.execute(
                update(DatasetImportTask)
                .where(
                    DatasetImportTask.import_task_id == tid,
                    DatasetImportTask.status == "IMPORTING",
                    DatasetImportTask.progress < 15,
                )
                .values(progress=1, stage="DISPATCHED")
            )
            logger.warning("import recovery: reset stale claimed task %s -> DISPATCHED", tid)

        if stale_claimed_ids:
            db.commit()

        ids = [*queued_ids]
        for tid in stale_claimed_ids:
            if tid not in ids and not is_import_task_active(tid):
                ids.append(tid)

        if not ids:
            return

        settings = get_settings()
        storage = get_storage(settings)
        logger.warning(
            "import recovery: re-dispatching %s stalled task(s) (first %s ids): %s",
            len(ids),
            min(20, len(ids)),
            ids[:20],
        )
        for i, tid in enumerate(ids):
            if is_import_task_active(tid):
                continue
            run_import_after_commit(storage, tid, delay_s=0.3 + i * 0.08)
    finally:
        db.close()
