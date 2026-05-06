from __future__ import annotations

import hashlib
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, Query, Request
from pydantic import BaseModel, Field

from backend.app.core.responses import ok
from backend.app.services.mock_store import store

router = APIRouter(prefix="/api/v1", tags=["dataset"])


class InstantCheckRequest(BaseModel):
    fileName: str
    fileSize: int
    fileHash: str
    hashAlgorithm: str = "SHA-256"
    businessType: str = "DATASET_IMPORT"


class CreateUploadRequest(BaseModel):
    fileName: str
    fileSize: int
    fileHash: str
    chunkSize: Optional[int] = None
    businessType: str = "DATASET_IMPORT"


class CompleteUploadRequest(BaseModel):
    fileHash: str
    parts: Optional[list[dict[str, Any]]] = None


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
    return ok(request, store.import_config())


@router.get("/dataset-directories")
def list_directories(
    request: Request,
    directoryName: Optional[str] = Query(default=None),
    importStatus: Optional[str] = Query(default=None),
    pageNo: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=200),
):
    return ok(
        request,
        store.list_directories(
            page_no=pageNo,
            page_size=pageSize,
            directory_name=directoryName,
            import_status=importStatus,
        ),
    )


@router.post("/dataset-upload/instant-check")
def instant_check(request: Request, body: InstantCheckRequest):
    return ok(request, store.instant_check(body.model_dump()))


@router.post("/dataset-upload/uploads")
def create_upload(request: Request, body: CreateUploadRequest):
    return ok(request, store.create_upload(body.model_dump()))


@router.put("/dataset-upload/uploads/{upload_id}/parts/{part_number}")
async def put_part(
    request: Request,
    upload_id: str,
    part_number: int,
    x_part_hash: Optional[str] = Header(default=None, alias="X-Part-Hash"),
):
    data = await request.body()
    return ok(request, await store.put_part(upload_id, part_number, data, x_part_hash))


@router.get("/dataset-upload/uploads/{upload_id}")
def get_upload(request: Request, upload_id: str):
    return ok(request, store.get_upload(upload_id))


@router.post("/dataset-upload/uploads/{upload_id}/complete")
def complete_upload(request: Request, upload_id: str, body: CompleteUploadRequest):
    return ok(request, store.complete_upload(upload_id, body.fileHash))


@router.post("/dataset-directories")
def create_directory(request: Request, body: CreateDirectoryRequest):
    return ok(request, store.create_directory(body.model_dump()))


@router.get("/dataset-import/tasks/{import_task_id}")
def get_import_task(request: Request, import_task_id: str):
    return ok(request, store.get_import_task(import_task_id))


@router.post("/dataset-directories/{directory_id}/reimport")
def reimport(request: Request, directory_id: str, body: ReimportRequest):
    return ok(request, store.reimport(directory_id, body.model_dump()))


@router.get("/dataset-directories/{directory_id}/records")
def directory_records(
    request: Request,
    directory_id: str,
    patientId: Optional[str] = Query(default=None),
    pageNo: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=200),
):
    return ok(request, store.directory_records(directory_id, pageNo, pageSize, patientId))


@router.delete("/dataset-directories/{directory_id}")
def delete_directory(request: Request, directory_id: str):
    return ok(request, store.delete_directory(directory_id))


@router.post("/dataset-directories/export")
def export_directories(request: Request, body: DirectoryExportRequest):
    joined = "-".join(body.directoryIds[:3])
    file_name = f"{joined}-20260506120000.zip"
    return ok(request, store.create_export("DATASET_DIRECTORY", file_name))


@router.get("/dataset-directories/{directory_id}/patients/{patient_id}/timeline")
def patient_timeline(request: Request, directory_id: str, patient_id: str):
    return ok(request, store.timeline(directory_id, patient_id))


@router.get("/dataset-directories/{directory_id}/patients/{patient_id}/images")
def patient_images(
    request: Request,
    directory_id: str,
    patient_id: str,
    surveyDate: str = Query(),
    pageNo: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=200),
):
    return ok(request, store.patient_images(directory_id, patient_id, surveyDate, pageNo, pageSize))


@router.get("/dataset-directories/{directory_id}/patients/{patient_id}/images/{image_id}")
def image_detail(request: Request, directory_id: str, patient_id: str, image_id: str):
    return ok(request, store.image_detail(directory_id, patient_id, image_id))


@router.post("/dataset-directories/{directory_id}/patients/{patient_id}/export")
def export_patient(request: Request, directory_id: str, patient_id: str, body: PatientExportRequest):
    file_name = f"患者数据导出-{patient_id}-20260506120000.zip"
    return ok(request, store.create_export("DATASET_PATIENT", file_name))


@router.get("/mock-files/{image_id}/{variant}")
def mock_file_url(request: Request, image_id: str, variant: str):
    content_hash = hashlib.sha256(f"{image_id}:{variant}".encode()).hexdigest()
    return ok(request, {"imageId": image_id, "variant": variant, "mockContentHash": content_hash})
