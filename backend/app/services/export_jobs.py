"""后台导出任务：在存储后端（local / FTP）上生成导出 zip。

目录导出优先自各目录入库时落地的 ``raw_zip/{directoryId}/source.zip`` 再展开，
避免依赖本地 ``ftp_mirror`` 的目录遍历（验收 P4 / 纯 FTP 场景）。
"""

from __future__ import annotations

import json
import re
import csv
import zipfile
from io import BytesIO, StringIO
from pathlib import Path

from typing import Any, Iterator

from sqlalchemy import select

from sqlalchemy.orm import Session

from backend.app.db.models import (
    DatasetDirectory,
    DatasetDynamicColumn,
    DatasetImageAsset,
    DatasetImageMetadata,
    DatasetMergedFile,
    DatasetQuestionnaireRecord,
    ExportRecord,
)
from backend.app.services.import_pipeline import _decode_zip_member_name
from backend.app.storage.backend import StorageBackend, normalize_storage_path


class ExportSourceZipMissing(Exception):
    def __init__(self, directory_id: str):
        super().__init__(f"缺少 source.zip，无法导出目录 {directory_id}")


def _zip_member_safe(name: str) -> bool:
    p = Path(name.replace("\\", "/"))
    return ".." not in p.parts and not p.is_absolute()


def _write_zip_member(
    zf: zipfile.ZipFile,
    arcname: str,
    data: bytes,
    *,
    written: set[str] | None = None,
) -> bool:
    normalized = arcname.replace("\\", "/")
    if not _zip_member_safe(normalized):
        return False
    if written is not None:
        if normalized in written:
            return False
        written.add(normalized)
    zf.writestr(normalized, data)
    return True


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


def _iter_directory_source_members(
    storage: StorageBackend,
    db: Session,
    directory_id: str,
) -> Iterator[tuple[str, bytes]]:
    zip_path = _resolve_directory_source_zip(storage, db, directory_id)
    raw_zip = storage.get_bytes(zip_path)
    with zipfile.ZipFile(BytesIO(raw_zip), "r") as inner:
        for m in inner.infolist():
            if m.is_dir():
                continue
            decoded_name = _decode_zip_member_name(m)
            if not _zip_member_safe(decoded_name):
                continue
            yield decoded_name.replace("\\", "/"), inner.read(m)


def _append_directory_tree_from_source(
    zf: zipfile.ZipFile,
    storage: StorageBackend,
    db: Session,
    directory_id: str,
    *,
    arc_prefix: str,
    written: set[str] | None = None,
) -> None:
    for decoded_name, data in _iter_directory_source_members(storage, db, directory_id):
        _write_zip_member(zf, f"{arc_prefix}/{decoded_name}", data, written=written)


def _questionnaire_rows(
    db: Session,
    directory_id: str,
    *,
    patient_id: str | None = None,
    date_filt: set[str] | None = None,
) -> list[dict[str, Any]]:
    filters = [
        DatasetQuestionnaireRecord.directory_id == directory_id,
        DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
    ]
    if patient_id is not None:
        filters.append(DatasetQuestionnaireRecord.patient_id == patient_id)
    if date_filt is not None:
        filters.append(DatasetQuestionnaireRecord.survey_date.in_(date_filt))
    rows = db.execute(
        select(DatasetQuestionnaireRecord)
        .where(*filters)
        .order_by(
            DatasetQuestionnaireRecord.patient_id,
            DatasetQuestionnaireRecord.survey_date,
            DatasetQuestionnaireRecord.record_id,
        )
    ).scalars().all()
    return [r.normalized_row_json or {} for r in rows]


def _questionnaire_records(
    db: Session,
    directory_ids: list[str],
    *,
    patient_id: str | None = None,
    date_filt: set[str] | None = None,
) -> list[DatasetQuestionnaireRecord]:
    filters = [
        DatasetQuestionnaireRecord.directory_id.in_(directory_ids),
        DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
    ]
    if patient_id is not None:
        filters.append(DatasetQuestionnaireRecord.patient_id == patient_id)
    if date_filt is not None:
        filters.append(DatasetQuestionnaireRecord.survey_date.in_(date_filt))
    return db.execute(
        select(DatasetQuestionnaireRecord)
        .where(*filters)
        .order_by(
            DatasetQuestionnaireRecord.directory_id,
            DatasetQuestionnaireRecord.survey_date,
            DatasetQuestionnaireRecord.record_id,
        )
    ).scalars().all()


def _write_questionnaire_rows(
    zf: zipfile.ZipFile,
    rows: list[dict[str, Any]],
    *,
    arcname: str,
    written: set[str] | None = None,
) -> None:
    payload = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    _write_zip_member(zf, arcname, payload, written=written)


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _parsed_json_payload(rec: DatasetQuestionnaireRecord) -> dict[str, Any]:
    raw = rec.raw_row_json or {}
    normalized = rec.normalized_row_json or {}
    excluded = set(raw.keys()) | {"patientId", "surveyDate", "newvisionImportPayload"}
    return {k: v for k, v in normalized.items() if k not in excluded}


def _dynamic_columns_by_directory(
    db: Session, directory_ids: list[str]
) -> dict[str, list[DatasetDynamicColumn]]:
    if not directory_ids:
        return {}
    rows = db.execute(
        select(DatasetDynamicColumn)
        .where(DatasetDynamicColumn.directory_id.in_(directory_ids))
        .order_by(DatasetDynamicColumn.directory_id, DatasetDynamicColumn.display_order)
    ).scalars().all()
    out: dict[str, list[DatasetDynamicColumn]] = {}
    for col in rows:
        out.setdefault(col.directory_id, []).append(col)
    return out


def _fallback_raw_columns(rec: DatasetQuestionnaireRecord) -> list[tuple[str, str]]:
    raw = rec.raw_row_json or {}
    return [(key, key) for key in raw.keys()]


def _unique_csv_header(preferred: str | None, fallback: str, seen: set[str]) -> str:
    for value in (preferred, fallback):
        header = str(value or "").strip()
        if header and header not in seen:
            seen.add(header)
            return header
    base = str(fallback or preferred or "字段").strip() or "字段"
    idx = 2
    while f"{base}_{idx}" in seen:
        idx += 1
    header = f"{base}_{idx}"
    seen.add(header)
    return header


def _merged_questionnaire_csv_bytes(
    db: Session,
    records: list[DatasetQuestionnaireRecord],
    *,
    directories_by_id: dict[str, DatasetDirectory],
    include_directory_context: bool,
) -> bytes:
    directory_ids = list(dict.fromkeys(r.directory_id for r in records))
    dynamic_by_dir = _dynamic_columns_by_directory(db, directory_ids)
    questionnaire_by_dir: dict[str, list[tuple[str, str]]] = {
        directory_id: [
            (col.column_key, col.column_title)
            for col in cols
            if col.source_type == "QUESTIONNAIRE"
        ]
        for directory_id, cols in dynamic_by_dir.items()
    }

    raw_headers: list[str] = []
    raw_header_by_dir_key: dict[tuple[str, str], str] = {}
    raw_header_by_title: dict[str, str] = {}
    seen_headers: set[str] = {"目录ID", "目录名称"} if include_directory_context else set()
    for rec in records:
        cols = questionnaire_by_dir.get(rec.directory_id) or _fallback_raw_columns(rec)
        for key, title in cols:
            preferred = title or key
            if preferred not in raw_header_by_title:
                raw_header_by_title[preferred] = _unique_csv_header(preferred, key, seen_headers)
                raw_headers.append(raw_header_by_title[preferred])
            raw_header_by_dir_key[(rec.directory_id, key)] = raw_header_by_title[preferred]

    parsed_payloads = {rec.record_id: _parsed_json_payload(rec) for rec in records}
    parsed_keys_present: set[str] = set()
    for payload in parsed_payloads.values():
        parsed_keys_present.update(payload.keys())

    parsed_columns: list[tuple[str, str]] = []
    seen_parsed_keys: set[str] = set()
    for rec in records:
        payload = parsed_payloads[rec.record_id]
        for col in dynamic_by_dir.get(rec.directory_id, []):
            if col.column_key in payload and col.column_key not in seen_parsed_keys:
                header = _unique_csv_header(col.column_title, col.column_key, seen_headers)
                parsed_columns.append((col.column_key, header))
                seen_parsed_keys.add(col.column_key)
    for key in sorted(parsed_keys_present - seen_parsed_keys):
        header = _unique_csv_header(key, key, seen_headers)
        parsed_columns.append((key, header))

    headers: list[str] = []
    if include_directory_context:
        headers.extend(["目录ID", "目录名称"])
    headers.extend(raw_headers)
    headers.extend(header for _, header in parsed_columns)

    sio = StringIO()
    writer = csv.DictWriter(sio, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        out: dict[str, str] = {}
        if include_directory_context:
            directory = directories_by_id.get(rec.directory_id)
            out["目录ID"] = rec.directory_id
            out["目录名称"] = directory.directory_name if directory else rec.directory_id
        raw = rec.raw_row_json or {}
        normalized = rec.normalized_row_json or {}
        cols = questionnaire_by_dir.get(rec.directory_id) or _fallback_raw_columns(rec)
        for key, title in cols:
            header = raw_header_by_dir_key.get((rec.directory_id, key), title)
            out[header] = _csv_cell(raw.get(key, normalized.get(key)))
        parsed_payload = parsed_payloads[rec.record_id]
        for key, header in parsed_columns:
            out[header] = _csv_cell(parsed_payload.get(key))
        writer.writerow(out)
    return ("\ufeff" + sio.getvalue()).encode("utf-8")


def _write_merged_questionnaire_csv(
    zf: zipfile.ZipFile,
    db: Session,
    records: list[DatasetQuestionnaireRecord],
    *,
    directories_by_id: dict[str, DatasetDirectory],
    include_directory_context: bool,
    arcname: str,
    written: set[str] | None = None,
) -> None:
    _write_zip_member(
        zf,
        arcname,
        _merged_questionnaire_csv_bytes(
            db,
            records,
            directories_by_id=directories_by_id,
            include_directory_context=include_directory_context,
        ),
        written=written,
    )


_DATE_IN_PATH_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?([01]?\d)[-_.]?([0-3]?\d)(?!\d)"
)


def _dates_in_member_name(name: str) -> set[str]:
    out: set[str] = set()
    for year, month, day in _DATE_IN_PATH_RE.findall(name.replace("\\", "/")):
        try:
            m = int(month)
            d = int(day)
        except ValueError:
            continue
        if 1 <= m <= 12 and 1 <= d <= 31:
            out.add(f"{int(year):04d}-{m:02d}-{d:02d}")
    return out


def _source_member_matches_patient(
    member_name: str,
    patient_id: str,
    date_filt: set[str] | None,
) -> bool:
    parts = [part for part in member_name.replace("\\", "/").split("/") if part]
    if patient_id not in parts:
        return False
    if date_filt is None:
        return True
    member_dates = _dates_in_member_name(member_name)
    return not member_dates or bool(member_dates & date_filt)


def _append_patient_members_from_source(
    zf: zipfile.ZipFile,
    storage: StorageBackend,
    db: Session,
    directory_id: str,
    patient_id: str,
    *,
    date_filt: set[str] | None,
    arc_prefix: str,
    written: set[str] | None = None,
) -> None:
    try:
        for member_name, data in _iter_directory_source_members(storage, db, directory_id):
            if _source_member_matches_patient(member_name, patient_id, date_filt):
                _write_zip_member(zf, f"{arc_prefix}/{member_name}", data, written=written)
    except ExportSourceZipMissing:
        return


def _successful_patient_directories(
    db: Session,
    patient_id: str,
) -> list[DatasetDirectory]:
    q_dirs = set(
        db.scalars(
            select(DatasetQuestionnaireRecord.directory_id)
            .join(
                DatasetDirectory,
                DatasetDirectory.directory_id == DatasetQuestionnaireRecord.directory_id,
            )
            .where(
                DatasetQuestionnaireRecord.patient_id == patient_id,
                DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
                DatasetDirectory.deleted == False,  # noqa: E712
                DatasetDirectory.import_status == "SUCCESS",
            )
            .distinct()
        ).all()
    )
    img_dirs = set(
        db.scalars(
            select(DatasetImageAsset.directory_id)
            .join(DatasetDirectory, DatasetDirectory.directory_id == DatasetImageAsset.directory_id)
            .where(
                DatasetImageAsset.patient_id == patient_id,
                DatasetImageAsset.deleted == False,  # noqa: E712
                DatasetDirectory.deleted == False,  # noqa: E712
                DatasetDirectory.import_status == "SUCCESS",
            )
            .distinct()
        ).all()
    )
    directory_ids = sorted(q_dirs | img_dirs)
    if not directory_ids:
        return []
    return db.execute(
        select(DatasetDirectory)
        .where(DatasetDirectory.directory_id.in_(directory_ids))
        .order_by(
            DatasetDirectory.imported_at,
            DatasetDirectory.created_at,
            DatasetDirectory.directory_id,
        )
    ).scalars().all()


def _patient_archive_prefix(directory: DatasetDirectory, directory_count: int) -> str:
    if directory_count <= 1:
        return "images"
    return f"images/{directory.directory_name}"


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
        include_merged_table = bool(opts.get("includeMergedTable", True))
        dirs = db.execute(
            select(DatasetDirectory).where(DatasetDirectory.directory_id.in_(ids))
        ).scalars().all()
        id_to_name = {d.directory_id: d.directory_name for d in dirs}
        directories_by_id = {d.directory_id: d for d in dirs}
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            written: set[str] = set()
            for did in ids:
                folder_name = id_to_name.get(did)
                if not folder_name:
                    raise ValueError(f"导出目录不存在: {did}")
                _append_directory_tree_from_source(
                    zf, storage, db, did, arc_prefix=folder_name, written=written
                )
                if include_merged_table:
                    q_records = _questionnaire_records(db, [did])
                    _write_questionnaire_rows(
                        zf,
                        [r.normalized_row_json or {} for r in q_records],
                        arcname=f"{folder_name}/questionnaire_rows.json",
                        written=written,
                    )
                    _write_merged_questionnaire_csv(
                        zf,
                        db,
                        q_records,
                        directories_by_id=directories_by_id,
                        include_directory_context=False,
                        arcname=f"{folder_name}/merged_questionnaire.csv",
                        written=written,
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
                            _write_zip_member(
                                zf,
                                f"{folder_name}/_parsed_derived/{zp_n}",
                                storage.get_bytes(sk),
                                written=written,
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
    mirrored = _relative_image_arc(
        directory_id, img.original_path, img.image_name
    ).replace("\\", "/")
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
        scope_dirs = _successful_patient_directories(db, pid)
        scope_ids = [d.directory_id for d in scope_dirs]
        directories_by_id = {d.directory_id: d for d in scope_dirs}

        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            written: set[str] = set()
            if scope_ids:
                q_filters[0] = DatasetQuestionnaireRecord.directory_id.in_(scope_ids)
                img_filters[0] = DatasetImageAsset.directory_id.in_(scope_ids)
            rows = db.execute(
                select(DatasetQuestionnaireRecord)
                .where(*q_filters)
                .order_by(
                    DatasetQuestionnaireRecord.directory_id,
                    DatasetQuestionnaireRecord.survey_date,
                    DatasetQuestionnaireRecord.record_id,
                )
            ).scalars().all()
            imgs = db.execute(
                select(DatasetImageAsset)
                .where(*img_filters)
                .order_by(
                    DatasetImageAsset.directory_id,
                    DatasetImageAsset.survey_date,
                    DatasetImageAsset.image_id,
                )
            ).scalars().all()
            if date_filt is not None and not rows and not imgs:
                raise ValueError("指定 surveyDates 条件下无问卷或影像可导出。")
            _write_questionnaire_rows(
                zf,
                [r.normalized_row_json or {} for r in rows],
                arcname="questionnaire_rows.json",
                written=written,
            )
            _write_merged_questionnaire_csv(
                zf,
                db,
                rows,
                directories_by_id=directories_by_id,
                include_directory_context=True,
                arcname="patient_merged_questionnaire.csv",
                written=written,
            )
            if include_original_attachments:
                for directory in scope_dirs:
                    _append_patient_members_from_source(
                        zf,
                        storage,
                        db,
                        directory.directory_id,
                        pid,
                        date_filt=date_filt,
                        arc_prefix=_patient_archive_prefix(directory, len(scope_dirs)),
                        written=written,
                    )
            for img in imgs:
                op_key = normalize_storage_path(img.original_path)
                arc = _relative_image_arc(img.directory_id, img.original_path, img.image_name)
                directory = directories_by_id.get(img.directory_id)
                image_prefix = (
                    _patient_archive_prefix(directory, len(scope_dirs))
                    if directory
                    else "images"
                )
                if include_original_attachments and storage.exists(op_key):
                    _write_zip_member(
                        zf,
                        f"{image_prefix}/{arc}",
                        storage.get_bytes(op_key),
                        written=written,
                    )
                if include_parsed_images and img.parsed_path:
                    meta_row = db.get(DatasetImageMetadata, img.image_id)
                    entries = parsed_derivative_zip_entries(
                        storage,
                        img,
                        meta_row.metadata_json if meta_row else None,
                        img.directory_id,
                    )
                    for sk, zp in entries:
                        zp_n = zp.replace("\\", "/")
                        _write_zip_member(
                            zf,
                            f"{image_prefix}/parsed/{zp_n}",
                            storage.get_bytes(sk),
                            written=written,
                        )
                if include_original_attachments and img.source_type in (
                    "PARSED_DAT",
                    "PARSED_OCT_DAT",
                ):
                    jkey = _oct_json_logical_path(op_key)
                    if jkey and storage.exists(jkey):
                        json_arc = Path(arc.replace("\\", "/")).with_suffix(".json").as_posix()
                        _write_zip_member(
                            zf,
                            f"{image_prefix}/oct_json/{json_arc}",
                            storage.get_bytes(jkey),
                            written=written,
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
