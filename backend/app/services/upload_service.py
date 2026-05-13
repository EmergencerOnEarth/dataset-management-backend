"""Upload lifecycle: instant-check, multipart upload, merge (API-03～07)."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, NotFoundError
from backend.app.db.models import DatasetMergedFile, DatasetUploadPart, DatasetUploadTask
from backend.app.storage.backend import StorageBackend
from backend.app.util.content_range import merge_expects_no_gaps, parse_content_range, validate_part_against_task
from backend.app.util.ids import new_id


def _validate_zip_name_size(file_name: str, file_size: int) -> None:
    s = get_settings()
    if not file_name.lower().endswith(".zip"):
        raise AppError("导入文件格式非zip文件，请检查导入文件。", "DATASET_IMPORT_FILE_TYPE_INVALID")
    if file_size > s.dataset_max_file_size:
        raise AppError(
            "导入文件超过8000m，请检查后重新上传。",
            "DATASET_IMPORT_FILE_SIZE_EXCEEDED",
            details={"maxFileSize": s.dataset_max_file_size},
        )


def import_config_dict() -> dict[str, Any]:
    s = get_settings()
    return {
        "maxFileSize": s.dataset_max_file_size,
        "maxFileSizeText": "8000M以内",
        "allowedExtensions": [".zip"],
        "recommendedChunkSize": s.dataset_chunk_size,
        "folderRules": {
            "questionnaire": {"required": True, "extensions": [".xlsx"], "patientIdRequired": True},
            "biometry": {"required": False, "parse": False, "blockingValidation": False},
            "fundus": {"required": False, "parse": "fdt_to_jpg", "blockingValidation": False},
            "oct": {"required": False, "parse": "dat_json", "blockingValidation": False},
        },
    }


def instant_check(
    db: Session,
    body: dict[str, Any],
) -> dict[str, Any]:
    s = get_settings()
    _validate_zip_name_size(body["fileName"], body["fileSize"])
    bt = body["businessType"]
    fh = body["fileHash"]

    hit = db.execute(
        select(DatasetMergedFile).where(
            DatasetMergedFile.file_hash == fh,
            DatasetMergedFile.file_size == body["fileSize"],
            DatasetMergedFile.business_type == bt,
            DatasetMergedFile.consumed == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if hit:
        return {
            "hit": True,
            "fileId": hit.file_id,
            "uploadId": None,
            "uploadedParts": [],
            "recommendedChunkSize": s.dataset_chunk_size,
        }

    active = db.execute(
        select(DatasetUploadTask)
        .where(
            DatasetUploadTask.file_hash == fh,
            DatasetUploadTask.file_size == body["fileSize"],
            DatasetUploadTask.business_type == bt,
            DatasetUploadTask.upload_status.in_(("INIT", "UPLOADING")),
        )
        .order_by(DatasetUploadTask.created_at.desc())
    ).scalar_one_or_none()

    if active:
        parts = db.scalars(
            select(DatasetUploadPart.part_number)
            .where(DatasetUploadPart.upload_id == active.upload_id)
            .order_by(DatasetUploadPart.part_number)
        ).all()
        return {
            "hit": False,
            "fileId": None,
            "uploadId": active.upload_id,
            "uploadedParts": list(parts),
            "recommendedChunkSize": active.chunk_size,
        }

    return {
        "hit": False,
        "fileId": None,
        "uploadId": None,
        "uploadedParts": [],
        "recommendedChunkSize": s.dataset_chunk_size,
    }


def create_upload_task(db: Session, storage: StorageBackend, body: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    _validate_zip_name_size(body["fileName"], body["fileSize"])
    upload_id = new_id("upl")
    chunk_size = body.get("chunkSize") or s.dataset_chunk_size
    chunk_size = int(max(1024 * 1024, min(chunk_size, 64 * 1024 * 1024)))
    part_count = math.ceil(body["fileSize"] / chunk_size)
    tmp_prefix = f"/dataset/upload/tmp/{upload_id}/parts"
    storage.mkdir_p(tmp_prefix)

    expire = datetime.utcnow() + timedelta(days=1)
    task = DatasetUploadTask(
        upload_id=upload_id,
        business_type=body.get("businessType") or "DATASET_IMPORT",
        file_name=body["fileName"],
        file_size=body["fileSize"],
        file_hash=body["fileHash"],
        chunk_size=chunk_size,
        part_count=part_count,
        upload_status="INIT",
        ftp_tmp_path=tmp_prefix,
        expire_at=expire,
    )
    db.add(task)
    db.flush()
    return {
        "uploadId": upload_id,
        "uploadStatus": "INIT",
        "chunkSize": chunk_size,
        "partCount": part_count,
        "expireAt": expire.strftime("%Y-%m-%d %H:%M:%S"),
    }


_TERMINAL_UPLOAD = frozenset({"MERGED", "FAILED", "CANCELED"})


def put_upload_part(
    db: Session,
    storage: StorageBackend,
    *,
    upload_id: str,
    part_number: int,
    body: bytes,
    content_range: str | None,
    x_part_hash: str | None,
) -> dict[str, Any]:
    task = db.get(DatasetUploadTask, upload_id)
    if not task:
        raise NotFoundError("上传任务不存在。")
    if task.upload_status in _TERMINAL_UPLOAD:
        raise AppError(
            "上传任务已结束，无法继续上传分片。",
            "DATASET_UPLOAD_TASK_CLOSED",
            code=40901,
            status_code=409,
        )

    if part_number < 1 or part_number > task.part_count:
        raise AppError(
            "上传分片序号不正确。",
            "DATASET_VALIDATION_ERROR",
            code=42201,
            status_code=422,
            details={"partNumber": part_number, "partCount": task.part_count},
        )

    pr = parse_content_range(content_range, task.file_size)
    validate_part_against_task(
        part_number=part_number,
        part_count=task.part_count,
        chunk_size=task.chunk_size,
        file_size=task.file_size,
        range_=pr,
        body_len=len(body),
    )

    digest = hashlib.sha256(body).hexdigest()
    if x_part_hash and x_part_hash.lower() != digest.lower():
        raise AppError("上传分片校验失败。", "DATASET_UPLOAD_PART_HASH_MISMATCH")

    rel_path = f"{task.ftp_tmp_path.rstrip('/')}/{part_number:08d}.part"
    storage.put_bytes(rel_path, body)

    existing = db.execute(
        select(DatasetUploadPart).where(
            DatasetUploadPart.upload_id == upload_id,
            DatasetUploadPart.part_number == part_number,
        )
    ).scalar_one_or_none()
    if existing:
        db.delete(existing)
        db.flush()
    db.add(
        DatasetUploadPart(
            upload_id=upload_id,
            part_number=part_number,
            part_size=len(body),
            part_hash=digest,
            etag=digest[:16],
            ftp_part_path=rel_path,
            range_start=pr.start,
            range_end=pr.end,
        )
    )
    task.upload_status = "UPLOADING"
    db.flush()

    uploaded = db.scalars(
        select(DatasetUploadPart.part_number)
        .where(DatasetUploadPart.upload_id == upload_id)
        .order_by(DatasetUploadPart.part_number)
    ).all()
    return {
        "uploadId": upload_id,
        "partNumber": part_number,
        "etag": digest[:16],
        "uploadedParts": list(uploaded),
        "uploadStatus": task.upload_status,
    }


def get_upload_task(db: Session, upload_id: str) -> dict[str, Any]:
    task = db.get(DatasetUploadTask, upload_id)
    if not task:
        raise NotFoundError("上传任务不存在。")
    uploaded = db.scalars(
        select(DatasetUploadPart.part_number)
        .where(DatasetUploadPart.upload_id == upload_id)
        .order_by(DatasetUploadPart.part_number)
    ).all()
    return {
        "fileName": task.file_name,
        "fileSize": task.file_size,
        "chunkSize": task.chunk_size,
        "partCount": task.part_count,
        "uploadedParts": list(uploaded),
        "uploadStatus": task.upload_status,
        "failureReason": task.failure_reason,
    }


def complete_upload(db: Session, storage: StorageBackend, upload_id: str, file_hash: str) -> dict[str, Any]:
    task = db.get(DatasetUploadTask, upload_id)
    if not task:
        raise NotFoundError("上传任务不存在。")

    parts = db.execute(
        select(DatasetUploadPart)
        .where(DatasetUploadPart.upload_id == upload_id)
        .order_by(DatasetUploadPart.part_number)
    ).scalars().all()
    if len(parts) != task.part_count:
        raise AppError("上传分片不完整，请重新上传缺失分片。", "DATASET_UPLOAD_PART_MISSING")

    triples = [(p.part_number, p.range_start, p.range_end) for p in parts]
    merge_expects_no_gaps(triples, task.file_size)

    s = get_settings()
    s.dataset_runtime_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", dir=s.dataset_runtime_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        with tmp_path.open("wb") as out:
            for p in parts:
                data = storage.get_bytes(p.ftp_part_path)
                digest.update(data)
                out.write(data)
        calculated = digest.hexdigest()
        if calculated != file_hash:
            task.upload_status = "FAILED"
            task.failure_reason = "文件校验失败"
            raise AppError("文件校验失败，请重新上传。", "DATASET_UPLOAD_HASH_MISMATCH")

        file_id = new_id("file")
        safe_name = re.sub(r"[^\w.\-]", "_", task.file_name)[:200] or "upload.zip"
        merged_path = f"/dataset/upload/merged/{file_id}/{safe_name}"
        storage.mkdir_p(f"/dataset/upload/merged/{file_id}")
        storage.put_file_from_path(merged_path, tmp_path)

        mf = DatasetMergedFile(
            file_id=file_id,
            business_type=task.business_type,
            file_name=task.file_name,
            file_size=task.file_size,
            file_hash=calculated,
            ftp_path=merged_path,
            merged_from_upload_id=upload_id,
            consumed=False,
        )
        db.add(mf)
        task.upload_status = "MERGED"
        db.flush()

        return {
            "fileId": file_id,
            "fileName": task.file_name,
            "fileSize": task.file_size,
            "fileHash": calculated,
            "storageKey": merged_path,
            "uploadStatus": "MERGED",
        }
    finally:
        tmp_path.unlink(missing_ok=True)
