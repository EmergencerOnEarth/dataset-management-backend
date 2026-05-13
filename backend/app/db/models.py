"""
ORM models aligned with design §4.2.

Table names are prefixed with ``dataset_`` to namespace this module's objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base


class DatasetDirectory(Base):
    __tablename__ = "dataset_directory"

    directory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    directory_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    import_status: Mapped[str] = mapped_column(String(32), index=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_zip_file_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    imported_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    import_tasks: Mapped[list["DatasetImportTask"]] = relationship(back_populates="directory")


class DatasetUploadTask(Base):
    __tablename__ = "dataset_upload_task"

    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business_type: Mapped[str] = mapped_column(String(64), default="DATASET_IMPORT", index=True)
    file_name: Mapped[str] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_hash: Mapped[str] = mapped_column(String(128), index=True)
    chunk_size: Mapped[int] = mapped_column(Integer)
    part_count: Mapped[int] = mapped_column(Integer)
    upload_status: Mapped[str] = mapped_column(String(32), index=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ftp_tmp_path: Mapped[str] = mapped_column(String(1024))
    expire_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    parts: Mapped[list["DatasetUploadPart"]] = relationship(
        back_populates="upload", cascade="all, delete-orphan"
    )


class DatasetUploadPart(Base):
    __tablename__ = "dataset_upload_part"
    __table_args__ = (UniqueConstraint("upload_id", "part_number", name="uq_dataset_upload_part"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dataset_upload_task.upload_id"), index=True
    )
    part_number: Mapped[int] = mapped_column(Integer)
    part_size: Mapped[int] = mapped_column(Integer)
    part_hash: Mapped[str] = mapped_column(String(128))
    etag: Mapped[str] = mapped_column(String(64))
    ftp_part_path: Mapped[str] = mapped_column(String(1024))
    range_start: Mapped[int] = mapped_column(BigInteger, default=0)
    range_end: Mapped[int] = mapped_column(BigInteger, default=0)

    upload: Mapped["DatasetUploadTask"] = relationship(back_populates="parts")


class DatasetMergedFile(Base):
    """User-merged zip on storage; referenced by ``file_id`` until import consumes it."""

    __tablename__ = "dataset_merged_file"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business_type: Mapped[str] = mapped_column(String(64), index=True)
    file_name: Mapped[str] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_hash: Mapped[str] = mapped_column(String(128), index=True)
    ftp_path: Mapped[str] = mapped_column(String(1024))
    merged_from_upload_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    directory_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DatasetImportTask(Base):
    __tablename__ = "dataset_import_task"

    import_task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    directory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dataset_directory.directory_id"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(128), default="")
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    asset_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    directory: Mapped["DatasetDirectory"] = relationship(back_populates="import_tasks")


class DatasetDynamicColumn(Base):
    __tablename__ = "dataset_dynamic_column"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    directory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dataset_directory.directory_id"), index=True
    )
    column_key: Mapped[str] = mapped_column(String(128))
    column_title: Mapped[str] = mapped_column(String(255))
    data_type: Mapped[str] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(32))
    display_order: Mapped[int] = mapped_column(Integer, default=0)


class DatasetQuestionnaireRecord(Base):
    __tablename__ = "dataset_questionnaire_record"

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    directory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dataset_directory.directory_id"), index=True
    )
    patient_id: Mapped[str] = mapped_column(String(128), index=True)
    survey_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    raw_row_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    normalized_row_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class DatasetImageAsset(Base):
    __tablename__ = "dataset_image_asset"

    image_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    directory_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dataset_directory.directory_id"), index=True
    )
    patient_id: Mapped[str] = mapped_column(String(128), index=True)
    survey_date: Mapped[str] = mapped_column(String(32), index=True)
    image_type: Mapped[str] = mapped_column(String(32))
    image_name: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(64))
    thumbnail_path: Mapped[str] = mapped_column(String(1024))
    preview_path: Mapped[str] = mapped_column(String(1024))
    original_path: Mapped[str] = mapped_column(String(1024))
    parsed_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    metadata_row: Mapped["DatasetImageMetadata"] = relationship(
        back_populates="image", uselist=False, cascade="all, delete-orphan"
    )


class DatasetImageMetadata(Base):
    __tablename__ = "dataset_image_metadata"

    image_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dataset_image_asset.image_id"), primary_key=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    series_no: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    instance_no: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    acquisition_datetime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    image: Mapped["DatasetImageAsset"] = relationship(back_populates="metadata_row")


class DatasetImportWarning(Base):
    __tablename__ = "dataset_import_warning"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_task_id: Mapped[str] = mapped_column(String(64), index=True)
    directory_id: Mapped[str] = mapped_column(String(64), index=True)
    warning_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExportRecord(Base):
    __tablename__ = "export_record"

    export_record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    export_type: Mapped[str] = mapped_column(String(64))
    export_status: Mapped[str] = mapped_column(String(32), index=True)
    file_name: Mapped[str] = mapped_column(String(512))
    ftp_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    expire_at: Mapped[datetime] = mapped_column(DateTime)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
