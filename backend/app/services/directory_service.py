"""Dataset directories, import/reimport, browsing, exports (API-02,08～17 partial)."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.app.core.errors import AppError, NotFoundError
from backend.app.db.models import (
    DatasetDirectory,
    DatasetDynamicColumn,
    DatasetImageAsset,
    DatasetImageMetadata,
    DatasetImportTask,
    DatasetMergedFile,
    DatasetQuestionnaireRecord,
    ExportRecord,
)
from backend.app.storage.backend import StorageBackend
from backend.app.util.ids import new_id


def _now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _frame_url(img: DatasetImageAsset) -> str | None:
    """OCT DAT 多帧基底 URL；前端拼接 ``metadata.octDat.frames`` 中的 PNG 文件名。"""
    parsed = getattr(img, "parsed_path", None)
    if not parsed:
        return None
    norm = str(parsed).replace("\\", "/")
    if ".frames/" in norm or norm.rsplit("/", 1)[-1].startswith("frame_"):
        return f"/api/v1/dataset-files/{img.image_id}/frame/"
    return None


def _perm_flags(row: DatasetDirectory) -> dict[str, bool]:
    st = row.import_status
    return {
        "canView": st == "SUCCESS",
        "canReimport": st == "FAILED",
        "canDelete": st != "IMPORTING",
        "canExport": st == "SUCCESS",
    }


def list_directories(
    db: Session,
    *,
    directory_name: str | None,
    import_status: str | None,
    imported_at_start: str | None,
    imported_at_end: str | None,
    page_no: int,
    page_size: int,
) -> dict[str, Any]:
    filters = [DatasetDirectory.deleted == False]  # noqa: E712
    if directory_name:
        filters.append(DatasetDirectory.directory_name.contains(directory_name))
    if import_status:
        filters.append(DatasetDirectory.import_status == import_status)
    if imported_at_start:
        filters.append(DatasetDirectory.imported_at >= imported_at_start)
    if imported_at_end:
        filters.append(DatasetDirectory.imported_at <= imported_at_end + " 23:59:59")

    total = db.scalar(select(func.count()).select_from(DatasetDirectory).where(*filters)) or 0
    list_stmt = (
        select(DatasetDirectory)
        .where(*filters)
        .order_by(DatasetDirectory.imported_at.desc(), DatasetDirectory.created_at.desc())
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    )
    rows = db.execute(list_stmt).scalars().all()

    records = []
    for r in rows:
        item = {
            "directoryId": r.directory_id,
            "directoryName": r.directory_name,
            "directoryDescription": r.description,
            "importStatus": r.import_status,
            "importRecordCount": r.record_count,
            "warningCount": r.warning_count,
            "importedAt": r.imported_at.strftime("%Y-%m-%d %H:%M:%S") if r.imported_at else None,
            "failureReason": r.failure_reason,
        }
        item.update(_perm_flags(r))
        records.append(item)
    return {"records": records, "total": total, "pageNo": page_no, "pageSize": page_size}


def create_directory(
    db: Session, body: dict[str, Any]
) -> dict[str, Any]:
    mf = db.get(DatasetMergedFile, body["fileId"])
    if not mf or mf.consumed:
        raise NotFoundError("上传文件不存在或尚未合并。")

    directory_id = new_id("dir")
    import_task_id = new_id("imp")
    now = dt.datetime.utcnow()

    directory = DatasetDirectory(
        directory_id=directory_id,
        directory_name=body["directoryName"],
        description=body.get("directoryDescription") or "",
        import_status="IMPORTING",
        record_count=0,
        warning_count=0,
        raw_zip_file_id=mf.file_id,
        imported_at=now,
    )
    task = DatasetImportTask(
        import_task_id=import_task_id,
        directory_id=directory_id,
        attempt_no=1,
        status="IMPORTING",
        progress=0,
        stage="QUEUED",
    )
    mf.consumed = True
    mf.directory_id = directory_id
    db.add(directory)
    db.add(task)
    db.flush()

    return {
        "directoryId": directory_id,
        "importTaskId": import_task_id,
        "importStatus": "IMPORTING",
        "submittedAt": _now_str(),
    }


def get_import_task(
    db: Session,
    import_task_id: str,
    *,
    storage: StorageBackend | None = None,
) -> dict[str, Any]:
    task = db.get(DatasetImportTask, import_task_id)
    if not task:
        raise NotFoundError("导入任务不存在。")
    if storage is not None and task.status == "IMPORTING" and task.created_at is not None:
        created = task.created_at
        if getattr(created, "tzinfo", None):
            created = created.replace(tzinfo=None)
        age_s = (dt.datetime.now() - created).total_seconds()
        from backend.app.services.import_pipeline import is_import_task_active

        needs_redispatch = (
            task.stage in ("QUEUED", "DISPATCHED") and task.progress < 5 and age_s >= 3
        ) or (
            task.stage in ("WORKER_CLAIMED", "READ_ZIP", "FETCH_SOURCE")
            and task.progress < 15
            and age_s >= 180
            and not is_import_task_active(import_task_id)
        )
        if needs_redispatch:
            from backend.app.workers.async_dispatch import maybe_lazy_redispatch_import

            maybe_lazy_redispatch_import(storage, import_task_id)
    return {
        "directoryId": task.directory_id,
        "importTaskId": task.import_task_id,
        "importStatus": task.status,
        "progress": task.progress,
        "stage": task.stage,
        "recordCount": task.record_count,
        "assetCount": task.asset_count,
        "warningCount": task.warning_count,
        "failureReason": task.failure_reason,
    }


def reimport_directory(
    db: Session, directory_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    d = db.get(DatasetDirectory, directory_id)
    if not d or d.deleted:
        raise NotFoundError("数据目录不存在。")
    if d.import_status not in ("FAILED",):
        raise AppError("仅失败目录允许重新导入。", "DATASET_REIMPORT_NOT_ALLOWED")
    mf = db.get(DatasetMergedFile, body["fileId"])
    if not mf or mf.consumed:
        raise NotFoundError("上传文件不存在或尚未合并。")

    attempt = (
        db.execute(
            select(func.coalesce(func.max(DatasetImportTask.attempt_no), 0)).where(
                DatasetImportTask.directory_id == directory_id
            )
        ).scalar_one()
        + 1
    )

    import_task_id = new_id("imp")
    d.directory_name = body["directoryName"]
    d.description = body.get("directoryDescription") or d.description
    d.import_status = "IMPORTING"
    d.failure_reason = None
    mf.consumed = True
    mf.directory_id = directory_id
    d.raw_zip_file_id = mf.file_id

    task = DatasetImportTask(
        import_task_id=import_task_id,
        directory_id=directory_id,
        attempt_no=attempt,
        status="IMPORTING",
        progress=0,
        stage="QUEUED",
    )
    db.add(task)
    db.flush()

    return {
        "directoryId": directory_id,
        "importTaskId": import_task_id,
        "importAttemptNo": attempt,
        "importStatus": "IMPORTING",
    }


def delete_directory(db: Session, directory_id: str) -> dict[str, Any]:
    d = db.get(DatasetDirectory, directory_id)
    if not d or d.deleted:
        raise NotFoundError("数据目录不存在。")
    if d.import_status == "IMPORTING":
        raise AppError("数据导入中，暂不允许删除。", "DATASET_DIRECTORY_IMPORTING")
    d.deleted = True
    db.flush()

    def _cleanup():
        from backend.app.db.session import get_session_factory

        # Future: async FTP cleanup; placeholder for acceptance G-04 traceability.
        with get_session_factory()() as s:
            pass

    threading.Thread(target=_cleanup, daemon=True).start()
    return {"directoryId": directory_id, "deleted": True, "deletedAt": _now_str()}


def directory_records(
    db: Session,
    directory_id: str,
    *,
    patient_id: str | None,
    survey_date_start: str | None,
    survey_date_end: str | None,
    page_no: int,
    page_size: int,
) -> dict[str, Any]:
    d = db.get(DatasetDirectory, directory_id)
    if not d or d.deleted:
        raise NotFoundError("数据目录不存在。")

    cols = db.execute(
        select(DatasetDynamicColumn)
        .where(DatasetDynamicColumn.directory_id == directory_id)
        .order_by(DatasetDynamicColumn.display_order)
    ).scalars().all()
    columns = [
        {
            "columnKey": c.column_key,
            "columnTitle": c.column_title,
            "dataType": c.data_type,
            "sourceType": c.source_type,
        }
        for c in cols
    ]

    q_filters = [
        DatasetQuestionnaireRecord.directory_id == directory_id,
        DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
    ]
    if patient_id:
        q_filters.append(DatasetQuestionnaireRecord.patient_id == patient_id)
    if survey_date_start:
        q_filters.append(
            or_(
                DatasetQuestionnaireRecord.survey_date.is_(None),
                DatasetQuestionnaireRecord.survey_date >= survey_date_start,
            )
        )
    if survey_date_end:
        q_filters.append(
            or_(
                DatasetQuestionnaireRecord.survey_date.is_(None),
                DatasetQuestionnaireRecord.survey_date <= survey_date_end,
            )
        )

    total = (
        db.scalar(
            select(func.count()).select_from(DatasetQuestionnaireRecord).where(*q_filters),
        )
        or 0
    )
    rows = db.execute(
        select(DatasetQuestionnaireRecord)
        .where(*q_filters)
        .order_by(DatasetQuestionnaireRecord.record_id)
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    pids_with_images = set(
        db.scalars(
            select(DatasetImageAsset.patient_id)
            .where(
                DatasetImageAsset.directory_id == directory_id,
                DatasetImageAsset.deleted == False,  # noqa: E712
            )
            .distinct()
        ).all()
    )

    records = []
    for r in rows:
        has_images = r.patient_id in pids_with_images
        cells = dict(r.normalized_row_json or {})
        records.append(
            {
                "recordId": r.record_id,
                "patientId": r.patient_id,
                "surveyDate": r.survey_date,
                "hasImages": has_images,
                "cells": cells,
            }
        )

    return {
        "columns": columns,
        "records": records,
        "total": total,
        "pageNo": page_no,
        "pageSize": page_size,
    }


def patient_timeline(db: Session, directory_id: str, patient_id: str) -> dict[str, Any]:
    d = db.get(DatasetDirectory, directory_id)
    if not d or d.deleted:
        raise NotFoundError("数据目录不存在。")
    rows = db.execute(
        select(DatasetImageAsset.survey_date, func.count())
        .where(
            DatasetImageAsset.directory_id == directory_id,
            DatasetImageAsset.patient_id == patient_id,
            DatasetImageAsset.deleted == False,  # noqa: E712
        )
        .group_by(DatasetImageAsset.survey_date)
    ).all()
    counts = {d0: c for d0, c in rows}
    sorted_dates = sorted(counts.keys(), reverse=True)
    dates = [
        {
            "surveyDate": date,
            "imageCount": counts[date],
            "defaultSelected": i == 0,
        }
        for i, date in enumerate(sorted_dates)
    ]
    return {"dates": dates}


def patient_images(
    db: Session, directory_id: str, patient_id: str, survey_date: str, page_no: int, page_size: int
) -> dict[str, Any]:
    d = db.get(DatasetDirectory, directory_id)
    if not d or d.deleted:
        raise NotFoundError("数据目录不存在。")
    filters = [
        DatasetImageAsset.directory_id == directory_id,
        DatasetImageAsset.patient_id == patient_id,
        DatasetImageAsset.survey_date == survey_date,
        DatasetImageAsset.deleted == False,  # noqa: E712
    ]
    total = db.scalar(select(func.count()).select_from(DatasetImageAsset).where(*filters)) or 0
    imgs = db.execute(
        select(DatasetImageAsset)
        .where(*filters)
        .order_by(DatasetImageAsset.image_id)
        .offset((page_no - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    records = []
    for i in imgs:
        records.append(
            {
                "imageId": i.image_id,
                "patientId": i.patient_id,
                "surveyDate": i.survey_date,
                "imageName": i.image_name,
                "sourceType": i.source_type,
                "thumbnailUrl": i.thumbnail_path,
                "previewUrl": i.preview_path,
                "frameUrl": _frame_url(i),
                "originalUrl": f"/api/v1/dataset-files/{i.image_id}/original",
                "createdAt": i.created_at.strftime("%Y-%m-%d %H:%M:%S") if i.created_at else None,
                "metadata": {},
            }
        )
    return {"records": records, "total": total, "pageNo": page_no, "pageSize": page_size}


def image_detail(db: Session, directory_id: str, patient_id: str, image_id: str) -> dict[str, Any]:
    img = db.get(DatasetImageAsset, image_id)
    if (
        not img
        or img.deleted
        or img.directory_id != directory_id
        or img.patient_id != patient_id
    ):
        raise NotFoundError("影像不存在。")
    meta_row = db.get(DatasetImageMetadata, image_id)
    meta_json = meta_row.metadata_json if meta_row else {}
    return {
        "imageId": img.image_id,
        "previewUrl": img.preview_path,
        "frameUrl": _frame_url(img),
        "originalUrl": f"/api/v1/dataset-files/{img.image_id}/original",
        "metadata": meta_json,
        "sequence": {"current": 1, "total": 1, "sameDateImageIds": [img.image_id]},
    }


def create_directory_export(
    db: Session, body: dict[str, Any]
) -> dict[str, Any]:
    ids = body["directoryIds"]
    if not ids:
        raise AppError("请选择至少一个目录。", "DATASET_VALIDATION_ERROR", code=42201, status_code=422)
    dirs = db.execute(select(DatasetDirectory).where(DatasetDirectory.directory_id.in_(ids))).scalars().all()
    if len(dirs) != len(ids):
        raise NotFoundError("部分数据目录不存在。")
    for d in dirs:
        if d.import_status != "SUCCESS" or d.deleted:
            raise AppError(
                "仅导入成功的目录可导出。",
                "DATASET_EXPORT_DIRECTORIES_INVALID",
                details={"directoryId": d.directory_id, "status": d.import_status},
            )
    export_id = new_id("exp")
    joined = "-".join(ids[:3])
    file_name = f"{joined}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.zip"
    expire = dt.datetime.utcnow() + dt.timedelta(days=7)
    rec = ExportRecord(
        export_record_id=export_id,
        export_type="DATASET_DIRECTORY",
        export_status="PREPARING",
        file_name=file_name,
        expire_at=expire,
        payload_json={"directoryIds": ids, "options": body},
    )
    db.add(rec)
    db.flush()

    return {
        "exportRecordId": export_id,
        "exportType": "DATASET_DIRECTORY",
        "exportStatus": "PREPARING",
        "fileName": file_name,
        "expireAt": expire.strftime("%Y-%m-%d %H:%M:%S"),
    }


def create_patient_export(
    db: Session, directory_id: str, patient_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    d = db.get(DatasetDirectory, directory_id)
    if not d or d.deleted:
        raise NotFoundError("数据目录不存在。")
    if d.import_status != "SUCCESS":
        raise AppError("目录未完成导入，无法导出患者数据。", "DATASET_EXPORT_NOT_READY")

    qc = db.scalar(
        select(func.count())
        .select_from(DatasetQuestionnaireRecord)
        .where(
            DatasetQuestionnaireRecord.directory_id == directory_id,
            DatasetQuestionnaireRecord.patient_id == patient_id,
            DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
        )
    ) or 0
    ic = db.scalar(
        select(func.count())
        .select_from(DatasetImageAsset)
        .where(
            DatasetImageAsset.directory_id == directory_id,
            DatasetImageAsset.patient_id == patient_id,
            DatasetImageAsset.deleted == False,  # noqa: E712
        )
    ) or 0
    if qc == 0 and ic == 0:
        raise AppError(
            "该患者在目录下无可导出的问卷或影像资源。",
            "DATASET_EXPORT_PATIENT_EMPTY",
        )

    export_id = new_id("exp")
    file_name = f"患者数据导出-{patient_id}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.zip"
    expire = dt.datetime.utcnow() + dt.timedelta(days=7)
    rec = ExportRecord(
        export_record_id=export_id,
        export_type="DATASET_PATIENT",
        export_status="PREPARING",
        file_name=file_name,
        expire_at=expire,
        payload_json={"directoryId": directory_id, "patientId": patient_id, "options": body},
    )
    db.add(rec)
    db.flush()

    return {
        "exportRecordId": export_id,
        "exportType": "DATASET_PATIENT",
        "exportStatus": "PREPARING",
        "fileName": file_name,
        "expireAt": expire.strftime("%Y-%m-%d %H:%M:%S"),
    }


_EXPORT_TYPE_NAMES = {
    "DATASET_DIRECTORY": "数据目录导出",
    "DATASET_PATIENT": "患者数据导出",
}

_EXPORT_STATUS_NAMES = {
    "PREPARING": "打包中",
    "DONE": "可下载",
    "FAILED": "导出失败",
    "EXPIRED": "已过期",
}


def _utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


def _format_dt(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _effective_export_status(rec: ExportRecord, *, now: dt.datetime | None = None) -> str:
    now = now or _utcnow()
    if rec.export_status == "DONE" and rec.expire_at < now:
        return "EXPIRED"
    return rec.export_status


def _is_export_downloadable(rec: ExportRecord, *, now: dt.datetime | None = None) -> bool:
    return _effective_export_status(rec, now=now) == "DONE" and bool(rec.ftp_path)


def _export_download_url(export_record_id: str, rec: ExportRecord, *, now: dt.datetime | None = None) -> str | None:
    if _is_export_downloadable(rec, now=now):
        return f"/api/v1/dataset-exports/{export_record_id}/download"
    return None


def _export_summary(rec: ExportRecord) -> dict[str, Any]:
    payload = rec.payload_json or {}
    if rec.export_type == "DATASET_DIRECTORY":
        ids = payload.get("directoryIds") or []
        return {"directoryIds": ids, "directoryCount": len(ids)}
    if rec.export_type == "DATASET_PATIENT":
        opts = payload.get("options") or {}
        return {
            "directoryId": payload.get("directoryId"),
            "patientId": payload.get("patientId"),
            "surveyDates": opts.get("surveyDates"),
        }
    return {}


def _export_payload(rec: ExportRecord) -> dict[str, Any]:
    payload = rec.payload_json or {}
    if rec.export_type == "DATASET_DIRECTORY":
        opts = payload.get("options") or {}
        return {
            "directoryIds": payload.get("directoryIds") or [],
            "includeOriginalTable": opts.get("includeOriginalTable"),
            "includeMergedTable": opts.get("includeMergedTable"),
            "includeParsedImages": opts.get("includeParsedImages"),
            "includeOriginalAttachments": opts.get("includeOriginalAttachments"),
        }
    if rec.export_type == "DATASET_PATIENT":
        opts = payload.get("options") or {}
        out: dict[str, Any] = {
            "directoryId": payload.get("directoryId"),
            "patientId": payload.get("patientId"),
        }
        if opts.get("surveyDates") is not None:
            out["surveyDates"] = opts.get("surveyDates")
        return out
    return dict(payload)


def _serialize_export_record(rec: ExportRecord, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    status = _effective_export_status(rec, now=now)
    return {
        "exportRecordId": rec.export_record_id,
        "exportType": rec.export_type,
        "exportTypeName": _EXPORT_TYPE_NAMES.get(rec.export_type, rec.export_type),
        "exportStatus": status,
        "exportStatusName": _EXPORT_STATUS_NAMES.get(status, status),
        "fileName": rec.file_name,
        "createdAt": _format_dt(rec.created_at),
        "finishedAt": None,
        "expireAt": _format_dt(rec.expire_at),
        "downloadable": _is_export_downloadable(rec, now=now),
        "downloadUrl": _export_download_url(rec.export_record_id, rec, now=now),
        "failureReason": rec.failure_reason,
        "summary": _export_summary(rec),
    }


def _serialize_export_detail(rec: ExportRecord, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    status = _effective_export_status(rec, now=now)
    return {
        "exportRecordId": rec.export_record_id,
        "exportType": rec.export_type,
        "exportStatus": status,
        "fileName": rec.file_name,
        "createdAt": _format_dt(rec.created_at),
        "finishedAt": None,
        "expireAt": _format_dt(rec.expire_at),
        "downloadable": _is_export_downloadable(rec, now=now),
        "downloadUrl": _export_download_url(rec.export_record_id, rec, now=now),
        "failureReason": rec.failure_reason,
        "payload": _export_payload(rec),
    }


def list_exports(
    db: Session,
    *,
    offset: int,
    limit: int,
    export_type: str | None,
    export_status: str | None,
    created_at_start: str | None,
    created_at_end: str | None,
) -> dict[str, Any]:
    if offset < 0:
        raise AppError("offset 不能小于 0。", "DATASET_VALIDATION_ERROR", code=42201, status_code=422)
    if limit < 1 or limit > 100:
        raise AppError("limit 须在 1～100 之间。", "DATASET_VALIDATION_ERROR", code=42201, status_code=422)

    now = _utcnow()
    filters: list[Any] = []
    if export_type:
        filters.append(ExportRecord.export_type == export_type)
    if created_at_start:
        filters.append(ExportRecord.created_at >= created_at_start)
    if created_at_end:
        filters.append(ExportRecord.created_at <= created_at_end + " 23:59:59")

    if export_status == "EXPIRED":
        filters.extend(
            [
                ExportRecord.export_status == "DONE",
                ExportRecord.expire_at < now,
            ]
        )
    elif export_status == "DONE":
        filters.extend(
            [
                ExportRecord.export_status == "DONE",
                ExportRecord.expire_at >= now,
            ]
        )
    elif export_status:
        filters.append(ExportRecord.export_status == export_status)

    count_stmt = select(func.count()).select_from(ExportRecord)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = db.scalar(count_stmt) or 0

    list_stmt = select(ExportRecord).order_by(ExportRecord.created_at.desc())
    if filters:
        list_stmt = list_stmt.where(*filters)
    rows = db.execute(list_stmt.offset(offset).limit(limit)).scalars().all()
    return {
        "records": [_serialize_export_record(r, now=now) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def get_export_detail(db: Session, export_record_id: str) -> dict[str, Any]:
    rec = db.get(ExportRecord, export_record_id)
    if not rec:
        raise NotFoundError("导出任务不存在。")
    return _serialize_export_detail(rec)


def get_export_download(db: Session, export_record_id: str) -> tuple[str, bytes]:
    rec = db.get(ExportRecord, export_record_id)
    if not rec:
        raise NotFoundError("导出任务不存在。")
    if not _is_export_downloadable(rec):
        raise AppError("导出文件不可下载。", "DATASET_EXPORT_NOT_DOWNLOADABLE", code=40301, status_code=403)
    if not rec.ftp_path:
        raise AppError("导出文件不可下载。", "DATASET_EXPORT_NOT_DOWNLOADABLE", code=40301, status_code=403)
    return rec.file_name, rec.ftp_path
