"""后台导出任务：在存储后端（local / FTP）上生成导出 zip。

目录导出优先自各目录入库时落地的 ``raw_zip/{directoryId}/source.zip`` 再展开，
避免依赖本地 ``ftp_mirror`` 的目录遍历（验收 P4 / 纯 FTP 场景）。
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

from typing import Any

from sqlalchemy import select

from sqlalchemy.orm import Session

from backend.app.db.models import (
    DatasetDirectory,
    DatasetImageAsset,
    DatasetImageMetadata,
    DatasetMergedFile,
    DatasetQuestionnaireRecord,
    ExportRecord,
)
from backend.app.storage.backend import StorageBackend, normalize_storage_path


class ExportSourceZipMissing(Exception):
    def __init__(self, directory_id: str):
        super().__init__(f"缺少 source.zip，无法导出目录 {directory_id}")


def _zip_member_safe(name: str) -> bool:
    p = Path(name.replace("\\", "/"))
    return ".." not in p.parts and not p.is_absolute()


def _resolve_directory_source_zip(storage: StorageBackend, db: Session, directory_id: str) -> str:
    canonical = f"/dataset/import/raw_zip/{directory_id}/source.zip"
    if storage.exists(canonical):
        return canonical
    directory = db.get(DatasetDirectory, directory_id)
    if directory and directory.raw_zip_file_id:
        merged = db.get(DatasetMergedFile, directory.raw_zip_file_id)
        if merged and storage.exists(merged.ftp_path):
            return merged.ftp_path
    raise ExportSourceZipMissing(directory_id)


def _append_directory_tree_from_source(
    zf: zipfile.ZipFile,
    storage: StorageBackend,
    db: Session,
    directory_id: str,
    *,
    arc_prefix: str,
) -> None:
    zip_path = _resolve_directory_source_zip(storage, db, directory_id)
    raw_zip = storage.get_bytes(zip_path)
    with zipfile.ZipFile(BytesIO(raw_zip), "r") as inner:
        for m in inner.infolist():
            if m.is_dir():
                continue
            if not _zip_member_safe(m.filename):
                continue
            arc = f"{arc_prefix}/{m.filename.replace(chr(92), '/')}"
            zf.writestr(arc, inner.read(m.filename))


def run_directory_export_job(storage: StorageBackend, export_record_id: str) -> None:
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    db = factory()
    try:
        rec = db.get(ExportRecord, export_record_id)
        if not rec or not rec.payload_json:
            return
        ids = rec.payload_json.get("directoryIds") or []
        opts = rec.payload_json.get("options") or {}
        include_parsed = bool(opts.get("includeParsedImages", True))
        dirs = db.execute(
            select(DatasetDirectory).where(DatasetDirectory.directory_id.in_(ids))
        ).scalars().all()
        id_to_name = {d.directory_id: d.directory_name for d in dirs}
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for did in ids:
                folder_name = id_to_name.get(did)
                if not folder_name:
                    raise ValueError(f"导出目录不存在: {did}")
                _append_directory_tree_from_source(
                    zf, storage, db, did, arc_prefix=folder_name
                )
                if include_parsed:
                    imgs = db.execute(
                        select(DatasetImageAsset).where(
                            DatasetImageAsset.directory_id == did,
                            DatasetImageAsset.deleted == False,  # noqa: E712
                            DatasetImageAsset.parsed_path.is_not(None),
                        )
                    ).scalars().all()
                    for img in imgs:
                        meta_row = db.get(DatasetImageMetadata, img.image_id)
                        meta_js = meta_row.metadata_json if meta_row else None
                        for sk, zp in parsed_derivative_zip_entries(storage, img, meta_js, did):
                            zp_n = zp.replace("\\", "/")
                            if not _zip_member_safe(zp_n):
                                continue
                            zf.writestr(
                                f"{folder_name}/_parsed_derived/{zp_n}",
                                storage.get_bytes(sk),
                            )
        data = buf.getvalue()
        out = f"/dataset/export/tmp/{export_record_id}/{rec.file_name}"
        storage.mkdir_p(f"/dataset/export/tmp/{export_record_id}")
        storage.put_bytes(out, data)
        rec.export_status = "DONE"
        rec.ftp_path = out
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        with factory() as db2:
            r = db2.get(ExportRecord, export_record_id)
            if r:
                r.export_status = "FAILED"
                r.failure_reason = str(exc)
            db2.commit()
    finally:
        db.close()


def _normalize_survey_filter_date(raw: Any) -> str | None:
    """Align with questionnaire ``YYYY-MM-DD`` strings."""
    s = str(raw).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _oct_json_logical_path(original_logical: str) -> str | None:
    """``.dat`` 旁的 ``.json`` 逻辑路径（与导入 raw_tree 布局一致）。"""
    op = normalize_storage_path(original_logical)
    if op.lower().endswith(".dat"):
        return op[:-4] + ".json"
    return None


def _relative_image_arc(directory_id: str, original_path: str, fallback_name: str) -> str:
    """ Strip ``/dataset/import/raw_tree/{directoryId}/`` prefix for archive layout. """
    op = normalize_storage_path(original_path)
    prefix = normalize_storage_path(f"/dataset/import/raw_tree/{directory_id}/")
    if op.startswith(prefix):
        return op[len(prefix) :].lstrip("/")
    return fallback_name.replace("\\", "/")


def parsed_derivative_zip_entries(
    storage: StorageBackend,
    img: DatasetImageAsset,
    metadata_json: dict[str, Any] | None,
    directory_id: str,
) -> list[tuple[str, str]]:
    """
    Resolved storage logical paths paired with posix paths relative to export subtrees:
    ``images/parsed/<path>`` or ``{{directory_id}}/_parsed_derived/<path>``.
    Full OCT PNG series uses ``metadata_json[\"octDat\"][\"frames\"]``.
    """
    if not img.parsed_path:
        return []
    parsed0 = normalize_storage_path(img.parsed_path.replace("\\", "/"))
    mirrored = _relative_image_arc(directory_id, img.original_path, img.image_name).replace("\\", "/")
    mirrored_p = Path(mirrored)
    oct_blob = metadata_json.get("octDat") if isinstance(metadata_json, dict) else None
    frames = oct_blob.get("frames") if isinstance(oct_blob, dict) else None
    is_oct = img.image_type == "OCT" or img.source_type in ("PARSED_OCT_DAT", "PARSED_DAT")
    stem = mirrored_p.stem
    frames_folder_zip = (mirrored_p.parent / f"{stem}.frames").as_posix()

    if is_oct and isinstance(frames, list) and frames:
        parent_fs = normalize_storage_path(str(Path(parsed0).parent))
        out: list[tuple[str, str]] = []
        for fn in frames:
            if not isinstance(fn, str) or not fn.strip():
                continue
            logical = normalize_storage_path(f"{parent_fs.rstrip('/')}/{fn.strip()}")
            if not storage.exists(logical):
                continue
            rel_zip = Path(frames_folder_zip) / Path(fn.strip()).name
            out.append((logical, rel_zip.as_posix()))
        if out:
            return out

    if storage.exists(parsed0):
        suf = Path(parsed0).suffix.lower()
        zip_rel = str(mirrored_p.with_suffix(suf if suf else ".jpg"))
        return [(parsed0, zip_rel)]

    return []


def run_patient_export_job(storage: StorageBackend, export_record_id: str) -> None:
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    db = factory()
    try:
        rec = db.get(ExportRecord, export_record_id)
        if not rec or not rec.payload_json:
            return
        did = rec.payload_json["directoryId"]
        pid = rec.payload_json["patientId"]
        opts = rec.payload_json.get("options") or {}
        raw_dates = opts.get("surveyDates")
        date_filt: set[str] | None = None
        if isinstance(raw_dates, list) and raw_dates:
            normalized = {_normalize_survey_filter_date(x) for x in raw_dates}
            date_filt = {x for x in normalized if x}
            if not date_filt:
                date_filt = None

        q_filters = [
            DatasetQuestionnaireRecord.directory_id == did,
            DatasetQuestionnaireRecord.patient_id == pid,
            DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
        ]
        img_filters = [
            DatasetImageAsset.directory_id == did,
            DatasetImageAsset.patient_id == pid,
            DatasetImageAsset.deleted == False,  # noqa: E712
        ]
        if date_filt is not None:
            q_filters.append(DatasetQuestionnaireRecord.survey_date.in_(date_filt))
            img_filters.append(DatasetImageAsset.survey_date.in_(date_filt))

        include_original_attachments = bool(opts.get("includeOriginalAttachments", True))
        include_parsed_images = bool(opts.get("includeParsedImages", True))

        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            rows = db.execute(select(DatasetQuestionnaireRecord).where(*q_filters)).scalars().all()
            imgs = db.execute(select(DatasetImageAsset).where(*img_filters)).scalars().all()
            if date_filt is not None and not rows and not imgs:
                raise ValueError("指定 surveyDates 条件下无问卷或影像可导出。")
            zf.writestr(
                "questionnaire_rows.json",
                json.dumps([r.normalized_row_json for r in rows], ensure_ascii=False, indent=2).encode("utf-8"),
            )
            for img in imgs:
                op_key = normalize_storage_path(img.original_path)
                arc = _relative_image_arc(did, img.original_path, img.image_name)
                if include_original_attachments and storage.exists(op_key):
                    zf.writestr(f"images/{arc}", storage.get_bytes(op_key))
                if include_parsed_images and img.parsed_path:
                    meta_row = db.get(DatasetImageMetadata, img.image_id)
                    entries = parsed_derivative_zip_entries(
                        storage,
                        img,
                        meta_row.metadata_json if meta_row else None,
                        did,
                    )
                    for sk, zp in entries:
                        zp_n = zp.replace("\\", "/")
                        if _zip_member_safe(zp_n):
                            zf.writestr(f"images/parsed/{zp_n}", storage.get_bytes(sk))
                if include_original_attachments and img.source_type in ("PARSED_DAT", "PARSED_OCT_DAT"):
                    jkey = _oct_json_logical_path(op_key)
                    if jkey and storage.exists(jkey):
                        json_arc = Path(arc.replace("\\", "/")).with_suffix(".json").as_posix()
                        zf.writestr(f"images/oct_json/{json_arc}", storage.get_bytes(jkey))
        data = buf.getvalue()
        out = f"/dataset/export/tmp/{export_record_id}/{rec.file_name}"
        storage.mkdir_p(f"/dataset/export/tmp/{export_record_id}")
        storage.put_bytes(out, data)
        rec.export_status = "DONE"
        rec.ftp_path = out
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        with factory() as db2:
            r = db2.get(ExportRecord, export_record_id)
            if r:
                r.export_status = "FAILED"
                r.failure_reason = str(exc)
            db2.commit()
    finally:
        db.close()
