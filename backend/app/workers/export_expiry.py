"""Background sweep for expired export records: clear FTP artifacts and mark EXPIRED."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select, update

from backend.app.core.config import get_settings
from backend.app.db.models import ExportRecord
from backend.app.db.session import get_session_factory
from backend.app.storage.backend import StorageBackend, get_storage

logger = logging.getLogger(__name__)


def _sweep_expired_exports_batch(storage: StorageBackend) -> int:
    settings = get_settings()
    factory = get_session_factory()
    db = factory()
    cleaned = 0
    try:
        now = dt.datetime.utcnow()
        rows = db.execute(
            select(ExportRecord)
            .where(
                ExportRecord.export_status == "DONE",
                ExportRecord.expire_at < now,
            )
            .limit(settings.dataset_export_expiry_batch_size)
        ).scalars().all()

        for rec in rows:
            ftp_path = rec.ftp_path
            result = db.execute(
                update(ExportRecord)
                .where(
                    ExportRecord.export_record_id == rec.export_record_id,
                    ExportRecord.export_status == "DONE",
                    ExportRecord.expire_at < now,
                )
                .values(export_status="EXPIRED", ftp_path=None)
            )
            if result.rowcount == 0:
                continue
            if ftp_path:
                try:
                    if storage.exists(ftp_path):
                        storage.remove_file(ftp_path)
                except Exception:
                    logger.exception(
                        "export expiry: failed to remove FTP object for %s at %s",
                        rec.export_record_id,
                        ftp_path,
                    )
            cleaned += 1
            logger.info("export expiry: marked %s EXPIRED", rec.export_record_id)

        if cleaned:
            db.commit()
    finally:
        db.close()
    return cleaned


def sweep_expired_exports(storage: StorageBackend | None = None, *, max_rounds: int = 100) -> int:
    """Run batch sweeps until no more expired DONE records remain (or max_rounds)."""
    settings = get_settings()
    if storage is None:
        storage = get_storage(settings)
    total = 0
    for _ in range(max_rounds):
        n = _sweep_expired_exports_batch(storage)
        total += n
        if n == 0:
            break
    if total:
        logger.info("export expiry: cleaned %s record(s)", total)
    return total
