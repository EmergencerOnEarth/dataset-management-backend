"""
数据集 REST 路由，对照设计 §3.2：API-01～21。

分块：导入配置 / 上传（instant-check～complete）/ 目录与导入任务 /
动态浏览与影像 / 目录与患者导出 / 导出任务查询 / 静态影像流 ``dataset-files``。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field

from backend.app.api.deps import DbSession, StorageDep
from backend.app.core.errors import NotFoundError
from backend.app.core.responses import ok
from backend.app.db.models import DatasetImageAsset
from backend.app.parsers.image_stubs import STUB_JPEG_BYTES
from backend.app.storage.backend import normalize_storage_path
from backend.app.services import directory_service, upload_service
from backend.app.workers.async_dispatch import (
    run_directory_export_after_commit,
    run_import_after_commit,
    run_patient_export_after_commit,
)

router = APIRouter(prefix="/api/v1", tags=["dataset"])


class InstantCheckRequest(BaseModel):
    fileName: str
    fileSize: int
    fileHash: str
    hashAlgorithm: str = "SHA-256"
    businessType: str


class CreateUploadRequest(BaseModel):
    fileName: str
    fileSize: int
    fileHash: str
    chunkSize: Optional[int] = None
    businessType: str


class CompleteUploadRequest(BaseModel):
    """合并分片：当前实现仅校验整包 ``fileHash``；``parts`` 若由前端携带（如 etag 字符串列表）应忽略而非 422。"""

    model_config = {"extra": "ignore"}

    fileHash: str


class CreateDirectoryRequest(BaseModel):
    directoryName: str = Field(min_length=2, max_length=30)
    directoryDescription: Optional[str] = Field(default=None, max_length=100)
    fileId: str
    originalFileName: Optional[str] = None


class ReimportRequest(CreateDirectoryRequest):
    remark: Optional[str] = None


class DirectoryExportRequest(BaseModel):
    directoryIds: list[str]
    includeOriginalTable: bool = True
    includeMergedTable: bool = True
    includeParsedImages: bool = True
    includeOriginalAttachments: bool = True


class PatientExportRequest(BaseModel):
    surveyDates: Optional[list[str]] = None
    includeOriginalTable: bool = True
    includeMergedTable: bool = True
    includeParsedImages: bool = True
    includeOriginalAttachments: bool = True


@router.get("/dataset-import/config")
def import_config(request: Request):
    return ok(request, upload_service.import_config_dict())


@router.get("/dataset-directories")
def list_directories(
    request: Request,
    db: DbSession,
    directoryName: Optional[str] = Query(default=None),
    importStatus: Optional[str] = Query(default=None),
    importedAtStart: Optional[str] = Query(default=None),
    importedAtEnd: Optional[str] = Query(default=None),
    pageNo: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=200),
):
    return ok(
        request,
        directory_service.list_directories(
            db,
            directory_name=directoryName,
            import_status=importStatus,
            imported_at_start=importedAtStart,
            imported_at_end=importedAtEnd,
            page_no=pageNo,
            page_size=pageSize,
        ),
    )


@router.post("/dataset-upload/instant-check")
def instant_check(request: Request, db: DbSession, body: InstantCheckRequest):
    return ok(request, upload_service.instant_check(db, body.model_dump()))


@router.post("/dataset-upload/uploads")
def create_upload(request: Request, db: DbSession, storage: StorageDep, body: CreateUploadRequest):
    return ok(request, upload_service.create_upload_task(db, storage, body.model_dump()))


@router.put("/dataset-upload/uploads/{upload_id}/parts/{part_number}")
async def put_part(
    request: Request,
    db: DbSession,
    storage: StorageDep,
    upload_id: str,
    part_number: int,
    content_range: Optional[str] = Header(default=None, alias="Content-Range"),
    x_part_hash: Optional[str] = Header(default=None, alias="X-Part-Hash"),
):
    data = await request.body()
    return ok(
        request,
        upload_service.put_upload_part(
            db,
            storage,
            upload_id=upload_id,
            part_number=part_number,
            body=data,
            content_range=content_range,
            x_part_hash=x_part_hash,
        ),
    )


@router.get("/dataset-upload/uploads/{upload_id}")
def get_upload(request: Request, db: DbSession, upload_id: str):
    return ok(request, upload_service.get_upload_task(db, upload_id))


@router.post("/dataset-upload/uploads/{upload_id}/complete")
def complete_upload(request: Request, db: DbSession, storage: StorageDep, upload_id: str, body: CompleteUploadRequest):
    payload = upload_service.complete_upload(db, storage, upload_id, body.fileHash)
    # Commit before returning so a immediately following create-directory request (possibly on
    # another worker) sees ``DatasetMergedFile``; teardown commit runs after the response is sent.
    db.commit()
    return ok(request, payload)


@router.post("/dataset-directories")
def create_directory(
    request: Request,
    db: DbSession,
    storage: StorageDep,
    body: CreateDirectoryRequest,
):
    payload = directory_service.create_directory(db, body.model_dump())
    db.commit()
    run_import_after_commit(storage, payload["importTaskId"])
    return ok(request, payload)


@router.get("/dataset-import/tasks/{import_task_id}")
def get_import_task(request: Request, db: DbSession, storage: StorageDep, import_task_id: str):
    return ok(request, directory_service.get_import_task(db, import_task_id, storage=storage))


@router.post("/dataset-directories/{directory_id}/reimport")
def reimport(
    request: Request,
    db: DbSession,
    storage: StorageDep,
    directory_id: str,
    body: ReimportRequest,
):
    payload = directory_service.reimport_directory(db, directory_id, body.model_dump())
    db.commit()
    run_import_after_commit(storage, payload["importTaskId"])
    return ok(request, payload)


@router.get("/dataset-directories/{directory_id}/records")
def directory_records(
    request: Request,
    db: DbSession,
    directory_id: str,
    patientId: Optional[str] = Query(default=None),
    surveyDateStart: Optional[str] = Query(default=None),
    surveyDateEnd: Optional[str] = Query(default=None),
    pageNo: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=200),
):
    return ok(
        request,
        directory_service.directory_records(
            db,
            directory_id,
            patient_id=patientId,
            survey_date_start=surveyDateStart,
            survey_date_end=surveyDateEnd,
            page_no=pageNo,
            page_size=pageSize,
        ),
    )


@router.delete("/dataset-directories/{directory_id}")
def delete_directory(request: Request, db: DbSession, directory_id: str):
    return ok(request, directory_service.delete_directory(db, directory_id))


@router.post("/dataset-directories/export")
def export_directories(
    request: Request,
    db: DbSession,
    storage: StorageDep,
    body: DirectoryExportRequest,
):
    payload = directory_service.create_directory_export(db, body.model_dump())
    db.commit()
    run_directory_export_after_commit(storage, payload["exportRecordId"])
    return ok(request, payload)


@router.get("/dataset-directories/{directory_id}/patients/{patient_id}/timeline")
def patient_timeline(request: Request, db: DbSession, directory_id: str, patient_id: str):
    return ok(request, directory_service.patient_timeline(db, directory_id, patient_id))


@router.get("/dataset-directories/{directory_id}/patients/{patient_id}/images")
def patient_images(
    request: Request,
    db: DbSession,
    directory_id: str,
    patient_id: str,
    surveyDate: str = Query(),
    pageNo: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
):
    return ok(
        request,
        directory_service.patient_images(db, directory_id, patient_id, surveyDate, pageNo, pageSize),
    )


@router.get("/dataset-directories/{directory_id}/patients/{patient_id}/images/{image_id}")
def image_detail(request: Request, db: DbSession, directory_id: str, patient_id: str, image_id: str):
    return ok(request, directory_service.image_detail(db, directory_id, patient_id, image_id))


@router.post("/dataset-directories/{directory_id}/patients/{patient_id}/export")
def export_patient(
    request: Request,
    db: DbSession,
    storage: StorageDep,
    directory_id: str,
    patient_id: str,
    body: PatientExportRequest,
):
    payload = directory_service.create_patient_export(db, directory_id, patient_id, body.model_dump())
    db.commit()
    run_patient_export_after_commit(storage, payload["exportRecordId"])
    return ok(request, payload)


@router.get("/dataset-exports/pending-download-count")
def pending_download_count(
    request: Request,
    db: DbSession,
    exportType: Optional[str] = Query(default=None),
):
    return ok(request, directory_service.count_pending_downloads(db, export_type=exportType))


@router.get("/dataset-exports")
def list_exports(
    request: Request,
    db: DbSession,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    exportType: Optional[str] = Query(default=None),
    exportStatus: Optional[str] = Query(default=None),
    createdAtStart: Optional[str] = Query(default=None),
    createdAtEnd: Optional[str] = Query(default=None),
):
    return ok(
        request,
        directory_service.list_exports(
            db,
            offset=offset,
            limit=limit,
            export_type=exportType,
            export_status=exportStatus,
            created_at_start=createdAtStart,
            created_at_end=createdAtEnd,
        ),
    )


@router.get("/dataset-exports/{export_record_id}/download")
def download_export(
    export_record_id: str,
    db: DbSession,
    storage: StorageDep,
):
    file_name, ftp_path = directory_service.get_export_download(db, storage, export_record_id)
    body = storage.get_bytes(ftp_path)
    ascii_name = file_name.encode("ascii", "ignore").decode() or "export.zip"
    encoded_name = quote(file_name)
    headers = {
        "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}',
    }
    return Response(content=body, media_type="application/zip", headers=headers)


@router.get("/dataset-exports/{export_record_id}")
def get_export_detail(request: Request, db: DbSession, export_record_id: str):
    return ok(request, directory_service.get_export_detail(db, export_record_id))


def _parsed_jpeg_logical_path(img: DatasetImageAsset) -> str:
    if getattr(img, "parsed_path", None):
        return normalize_storage_path(img.parsed_path)
    stem = Path(img.image_name).stem
    base = f"/dataset/import/parsed/{img.directory_id}"
    if img.source_type == "PARSED_FDT":
        return normalize_storage_path(f"{base}/fundus/{stem}.jpg")
    if img.source_type == "PARSED_DAT":
        return normalize_storage_path(f"{base}/oct/{stem}.jpg")
    return normalize_storage_path(f"{base}/{stem}.jpg")


@router.get("/dataset-files/{image_id}/{variant}")
def serve_image_file(
    image_id: str,
    variant: str,
    db: DbSession,
    storage: StorageDep,
):
    """缩略图/预览读解析 jpeg；原图按 ``original_path`` 经存储后端读取（FTP 与 local 统一）。"""
    img = db.get(DatasetImageAsset, image_id)
    if not img or img.deleted:
        raise NotFoundError("影像不存在。")
    if variant in ("thumbnail", "preview"):
        lp = _parsed_jpeg_logical_path(img)
        if storage.exists(lp):
            body = storage.get_bytes(lp)
            mt = "image/png" if lp.lower().endswith(".png") else "image/jpeg"
            return Response(content=body, media_type=mt)
        return Response(content=STUB_JPEG_BYTES, media_type="image/jpeg")
    if variant == "original":
        op = normalize_storage_path(img.original_path)
        if storage.exists(op):
            return Response(content=storage.get_bytes(op), media_type="application/octet-stream")
        return Response(content=STUB_JPEG_BYTES, media_type="image/jpeg")
    raise NotFoundError("影像不存在。")


@router.get("/dataset-files/{image_id}/frame/{frame_name}")
def serve_oct_frame(
    image_id: str,
    frame_name: str,
    db: DbSession,
    storage: StorageDep,
):
    """按帧名返回 OCT DAT 解析帧 PNG。
    frame_name 形如 ``frame_00000.png``；从 parsed_path 推算帧目录，直接读取对应文件。
    前端可遍历 metadata.octDat.frames 列表，依次请求此接口实现滚动播放。
    """
    img = db.get(DatasetImageAsset, image_id)
    if not img or img.deleted:
        raise NotFoundError("影像不存在。")
    if not getattr(img, "parsed_path", None):
        raise NotFoundError("该影像无解析帧。")
    # parsed_path 指向 frame_00000.png，其父目录即帧目录
    frames_dir = normalize_storage_path(str(Path(img.parsed_path).parent))
    target = f"{frames_dir}/{frame_name}"
    if not storage.exists(target):
        raise NotFoundError(f"帧文件不存在: {frame_name}")
    body = storage.get_bytes(target)
    return Response(content=body, media_type="image/png")


@router.get("/mock-files/{image_id}/{variant}")
def mock_file_url_compat(request: Request, image_id: str, variant: str):
    content_hash = hashlib.sha256(f"{image_id}:{variant}".encode()).hexdigest()
    return ok(request, {"imageId": image_id, "variant": variant, "mockContentHash": content_hash})
