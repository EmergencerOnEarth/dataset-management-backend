"""
Post-``POST /dataset-directories`` asynchronous import (design §4.3, §6.8).

Runs in a background thread with its own DB session; updates ``dataset_import_task``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.app.core.errors import AppError, NotFoundError
from backend.app.db.models import (
    DatasetDirectory,
    DatasetDynamicColumn,
    DatasetImageAsset,
    DatasetImageMetadata,
    DatasetImportTask,
    DatasetImportWarning,
    DatasetMergedFile,
    DatasetQuestionnaireRecord,
)
from backend.app.parsers import image_stubs
from backend.app.parsers.newvision import (
    fundus_fdt_maybe_jpeg,
    parse_newvision_questionnaire_xlsx_bytes,
    select_newvision_questionnaire_xlsx,
)
from backend.app.parsers.newvision_oct import (
    extract_oct_path_context,
    parse_oct_dat_bytes,
    should_parse_dat,
)
from backend.app.parsers.registry import SUPPORTED_PACKAGE_LAYOUTS, detect_dataset_package_vendor
from backend.app.storage.backend import StorageBackend
from backend.app.util.ids import new_id


_SKIP_FILENAMES: frozenset[str] = frozenset({
    ".ds_store", "thumbs.db", "desktop.ini", ".gitkeep", ".gitignore",
})
_SKIP_PATH_PARTS: frozenset[str] = frozenset({
    "__macosx", ".git", "__pycache__", ".svn",
})


def _is_safe_zip_member(name: str) -> bool:
    p = Path(name.replace("\\", "/"))
    return ".." not in p.parts and not p.is_absolute()


def _is_ignored_zip_member(name: str) -> bool:
    """设计 §2：忽略 macOS/.Windows/版本管理产生的系统临时文件。"""
    p = Path(name.replace("\\", "/"))
    if p.name.lower() in _SKIP_FILENAMES:
        return True
    for part in p.parts[:-1]:
        if part.lower() in _SKIP_PATH_PARTS:
            return True
    return False


def _decode_zip_filename(raw: str) -> str:
    """
    SEC-05: zip spec says filenames are cp437 unless the UTF-8 flag (bit 11) is set.
    Python's zipfile module sets ZipInfo.flag_bits; if the UTF-8 flag is absent and
    the name contains mojibake, try GBK/GB18030 (common for Windows-made Chinese zips).
    """
    return raw


def _decode_zip_member_name(m: zipfile.ZipInfo) -> str:
    """Return a correctly decoded filename for a ZipInfo entry."""
    # Bit 11 of general purpose flags = Language Encoding Flag (UTF-8)
    if m.flag_bits & 0x800:
        return m.filename  # zipfile already decoded as UTF-8
    # Not marked UTF-8; the raw bytes are in m.filename encoded as cp437 by Python.
    # Try to recover by re-encoding as cp437 then decoding as GBK/GB18030.
    try:
        return m.filename.encode("cp437").decode("gbk")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    try:
        return m.filename.encode("cp437").decode("gb18030")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    # Fallback: use as-is (may still be mojibake but won't crash)
    return m.filename


def _extract_zip_to_storage(
    storage: StorageBackend, zip_bytes: bytes, directory_id: str
) -> list[str]:
    """Write zip members to ``raw_tree`` and return normalized relative paths (files only)."""
    base = f"/dataset/import/raw_tree/{directory_id}"
    storage.mkdir_p(base)
    extracted: list[str] = []
    buf = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buf, "r") as zf:
        for m in zf.infolist():
            if m.is_dir():
                continue
            decoded_name = _decode_zip_member_name(m)
            if not _is_safe_zip_member(decoded_name):
                raise AppError(
                    "压缩包内含不安全路径（路径穿越或非法条目）。",
                    "DATASET_IMPORT_UNSAFE_ENTRY",
                )
            if _is_ignored_zip_member(decoded_name):
                continue
            with zf.open(m, "r") as src:
                data = src.read()
            rel = decoded_name.replace("\\", "/").lstrip("/")
            storage.put_bytes(f"{base}/{rel}", data)
            extracted.append(rel)
    return extracted


def _raw_tree_prefix(directory_id: str) -> str:
    return f"/dataset/import/raw_tree/{directory_id}"


def _guess_pid_from_relative(rel_path: str, known_pids: set[str]) -> str:
    parts = Path(rel_path).parts
    for p in reversed(parts[:-1]):
        if p in known_pids:
            return p
    for p in reversed(parts[:-1]):
        if re.match(r"^[A-Za-z0-9_-]{3,32}$", p):
            return p
    return parts[-2] if len(parts) >= 2 else "unknown"


def _survey_date_from_rel(rel_path: str) -> str:
    parts = Path(rel_path).parts
    for p in parts:
        m = re.match(r"^(\d{4})[-_/]?(\d{2})[-_/]?(\d{2})$", p)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m2 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", p)
        if m2:
            return f"{int(m2.group(1)):04d}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
    return ""


def _is_oct_path(rel: str) -> bool:
    return "/oct/" in f"/{rel.replace(chr(92), '/').lower()}/"


def _oct_match_warnings(
    pid: str | None,
    check_date: str | None,
    known_pids: set[str],
    patient_dates: dict[str, set[str]],
) -> list[str]:
    if not pid:
        return ["PID_NOT_MATCHED"]
    if pid not in known_pids:
        return ["PID_NOT_MATCHED"]
    dates = patient_dates.get(pid) or set()
    if check_date and dates and check_date not in dates:
        return ["PID_DATE_NOT_MATCHED"]
    return []


def _merge_oct_fields_into_questionnaire_row(
    db: Session,
    directory_id: str,
    patient_id: str,
    survey_date: str,
    fields: dict[str, Any],
) -> None:
    if not fields:
        return
    stmt = select(DatasetQuestionnaireRecord).where(
        DatasetQuestionnaireRecord.directory_id == directory_id,
        DatasetQuestionnaireRecord.patient_id == patient_id,
        DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
    )
    if survey_date.strip():
        stmt = stmt.where(DatasetQuestionnaireRecord.survey_date == survey_date.strip())
    rec = db.execute(stmt).scalars().first()
    if not rec:
        rec = db.execute(
            select(DatasetQuestionnaireRecord)
            .where(
                DatasetQuestionnaireRecord.directory_id == directory_id,
                DatasetQuestionnaireRecord.patient_id == patient_id,
                DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
            )
            .order_by(DatasetQuestionnaireRecord.survey_date)
        ).scalars().first()
    if not rec:
        return
    cells = dict(rec.normalized_row_json or {})
    cells.update(fields)
    rec.normalized_row_json = cells
    flag_modified(rec, "normalized_row_json")


def run_import_task(storage: StorageBackend, import_task_id: str) -> None:
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    db = factory()
    try:
        task = db.get(DatasetImportTask, import_task_id)
        if not task:
            return
        directory = db.get(DatasetDirectory, task.directory_id)
        if not directory:
            return
        try:
            _run_import_core(db, storage, task, directory)
            db.commit()
        except AppError as e:
            db.rollback()
            _fail_task(import_task_id, e.message)
        except Exception as exc:  # pragma: no cover
            db.rollback()
            _fail_task(import_task_id, str(exc))
    finally:
        db.close()


def _fail_task(import_task_id: str, message: str) -> None:
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    with factory() as db:
        t = db.get(DatasetImportTask, import_task_id)
        if t:
            t.status = "FAILED"
            t.progress = 100
            t.failure_reason = message
            d = db.get(DatasetDirectory, t.directory_id)
            if d:
                d.import_status = "FAILED"
                d.failure_reason = message
        db.commit()


def _run_import_core(db: Session, storage: StorageBackend, task: DatasetImportTask, directory: DatasetDirectory) -> None:
    task.status = "IMPORTING"
    task.progress = 5
    task.stage = "READ_ZIP"
    db.flush()

    if not directory.raw_zip_file_id:
        raise AppError("目录缺少源文件引用。", "DATASET_IMPORT_STRUCTURE_INVALID")
    merged = db.get(DatasetMergedFile, directory.raw_zip_file_id)
    if not merged:
        raise NotFoundError("上传文件不存在。")

    zip_bytes = storage.get_bytes(merged.ftp_path)
    storage.put_bytes(f"/dataset/import/raw_zip/{directory.directory_id}/source.zip", zip_bytes)

    task.progress = 15
    task.stage = "UNZIP"
    db.flush()

    extracted_rels = _extract_zip_to_storage(storage, zip_bytes, directory.directory_id)
    extracted_set = set(extracted_rels)
    raw_tree_base = _raw_tree_prefix(directory.directory_id)

    if not any(r.lower().endswith(".xlsx") for r in extracted_rels):
        raise AppError("压缩包内缺失表格文件，请检查导入文件。", "DATASET_IMPORT_TABLE_MISSING")

    package_layout = detect_dataset_package_vendor(extracted_rels)
    if package_layout not in SUPPORTED_PACKAGE_LAYOUTS:
        raise AppError(
            "当前后端仅支持已实现的数据包布局，请联系开发人员扩展解析模块。",
            "DATASET_IMPORT_PACKAGE_UNSUPPORTED",
            details={
                "detectedLayout": package_layout,
                "supportedLayouts": sorted(SUPPORTED_PACKAGE_LAYOUTS),
            },
        )

    xlsx_rel = select_newvision_questionnaire_xlsx(extracted_rels)
    xlsx_bytes = storage.get_bytes(f"{raw_tree_base}/{xlsx_rel}")

    task.progress = 35
    task.stage = "PARSE_QUESTIONNAIRE"
    db.flush()

    qres = parse_newvision_questionnaire_xlsx_bytes(xlsx_bytes)

    img_ids = db.scalars(
        select(DatasetImageAsset.image_id).where(DatasetImageAsset.directory_id == directory.directory_id)
    ).all()
    if img_ids:
        db.execute(delete(DatasetImageMetadata).where(DatasetImageMetadata.image_id.in_(img_ids)))
    db.execute(delete(DatasetImageAsset).where(DatasetImageAsset.directory_id == directory.directory_id))
    db.execute(delete(DatasetQuestionnaireRecord).where(DatasetQuestionnaireRecord.directory_id == directory.directory_id))
    db.execute(delete(DatasetDynamicColumn).where(DatasetDynamicColumn.directory_id == directory.directory_id))
    db.execute(delete(DatasetImportWarning).where(DatasetImportWarning.directory_id == directory.directory_id))

    known_pids = {r["patient_id"] for r in qres.rows}
    patient_dates: dict[str, set[str]] = {}
    for r in qres.rows:
        patient_dates.setdefault(r["patient_id"], set())
        if r["survey_date"]:
            patient_dates[r["patient_id"]].add(r["survey_date"])

    order = 0
    base_cols = [
        ("patientId", "患者ID", "STRING", "QUESTIONNAIRE"),
        ("surveyDate", "调查日期", "DATE", "NORMALIZED"),
    ]
    for key, title, dtype, st in base_cols:
        db.add(
            DatasetDynamicColumn(
                directory_id=directory.directory_id,
                column_key=key,
                column_title=title,
                data_type=dtype,
                source_type=st,
                display_order=order,
            )
        )
        order += 1
    for c in qres.columns:
        db.add(
            DatasetDynamicColumn(
                directory_id=directory.directory_id,
                column_key=c["columnKey"],
                column_title=c["columnTitle"],
                data_type=c["dataType"],
                source_type=c["sourceType"],
                display_order=order,
            )
        )
        order += 1

    dyn_display_order = order
    oct_column_keys_registered: set[str] = set()

    warning_count = 0
    q_keys = [(r["patient_id"], r["survey_date"]) for r in qres.rows]
    for key, n in Counter(q_keys).items():
        if n > 1:
            warning_count += 1
            db.add(
                DatasetImportWarning(
                    import_task_id=task.import_task_id,
                    directory_id=directory.directory_id,
                    warning_type="DUPLICATE_PID_DATE",
                    message=f"问卷存在重复 PID+检查日期: {key[0]} / {key[1]}，共 {n} 行。",
                    detail={"pid": key[0], "checkDate": key[1], "rowCount": n},
                )
            )

    for row in qres.rows:
        if row["survey_date"] is None:
            warning_count += 1
            db.add(
                DatasetImportWarning(
                    import_task_id=task.import_task_id,
                    directory_id=directory.directory_id,
                    warning_type="DATE_EMPTY",
                    message=f"问卷行检查日期为空: 患者 {row['patient_id']}",
                    detail={"patientId": row["patient_id"]},
                )
            )

    for row in qres.rows:
        rid = new_id("rec")
        cells = {**row["normalized_cells"], "patientId": row["patient_id"], "surveyDate": row["survey_date"]}
        payload = row.get("newvision_import_payload")
        if payload:
            cells["newvisionImportPayload"] = payload
        db.add(
            DatasetQuestionnaireRecord(
                record_id=rid,
                directory_id=directory.directory_id,
                patient_id=row["patient_id"],
                survey_date=row["survey_date"],
                raw_row_json=row["raw_cells"],
                normalized_row_json=cells,
            )
        )

    db.flush()

    task.progress = 55
    task.stage = "INDEX_IMAGES"
    asset_count = 0

    def _register_oct_json_columns(oct_scalar: dict[str, Any]) -> None:
        nonlocal dyn_display_order
        if not oct_scalar:
            return
        for ck, cv in oct_scalar.items():
            if ck not in oct_column_keys_registered:
                oct_column_keys_registered.add(ck)
                title = (ck.replace("oct_", "").replace("_", " ").strip() or ck)[:120]
                data_type = "NUMBER"
                if isinstance(cv, bool):
                    data_type = "BOOLEAN"
                elif isinstance(cv, str):
                    data_type = "STRING"
                db.add(
                    DatasetDynamicColumn(
                        directory_id=directory.directory_id,
                        column_key=ck,
                        column_title=title,
                        data_type=data_type,
                        source_type="OCT_JSON",
                        display_order=dyn_display_order,
                    )
                )
                dyn_display_order += 1

    # ── OCT JSON 独立资产记录 ─────────────────────────────────────────────────
    # 设计 §7.4 / NV-09 / DC-07:
    #   每个 OCT JSON 文件独立建立一条 DatasetImageAsset(OCT_JSON) 记录，
    #   原始 JSON 完整写入 metadata_json.raw（不覆盖），扁平化摘要写入 .flattened。
    #   额外将扁平化标量合并到对应问卷行（last-wins，用于列表展示列；
    #   全量历史数据见各 OCT_JSON 资产的 metadata_json）。
    for rel in sorted(extracted_rels):
        if not _is_oct_path(rel) or not rel.lower().endswith(".json"):
            continue
        ctx = extract_oct_path_context(rel)
        pid = ctx.get("pid") or _guess_pid_from_relative(rel, known_pids)
        sdate = (
            ctx.get("check_date")
            or _survey_date_from_rel(rel)
            or (min(patient_dates[pid]) if pid in patient_dates and patient_dates[pid] else "")
        )
        check_key_date = ctx.get("check_date")
        for code in _oct_match_warnings(pid, check_key_date, known_pids, patient_dates):
            warning_count += 1
            db.add(
                DatasetImportWarning(
                    import_task_id=task.import_task_id,
                    directory_id=directory.directory_id,
                    warning_type=code,
                    message=f"OCT JSON 关联: {code}: {rel}",
                    detail={"path": rel, "pid": pid, "checkDate": check_key_date},
                )
            )
        try:
            jraw = storage.get_bytes(f"{raw_tree_base}/{rel}")
            scalar_for_cols = image_stubs.oct_json_scalar_columns(jraw)
        except Exception as exc:  # noqa: BLE001
            warning_count += 1
            db.add(
                DatasetImportWarning(
                    import_task_id=task.import_task_id,
                    directory_id=directory.directory_id,
                    warning_type="OCT_JSON_PARSE_FAILED",
                    message=f"OCT JSON 解析失败: {rel}",
                    detail={"path": rel, "error": str(exc)},
                )
            )
            continue

        # 独立资产记录（NV-09：每个 JSON 各有一条，不因 update() 覆盖）
        import json as _json
        try:
            raw_obj = _json.loads(jraw.decode("utf-8-sig"))
        except Exception:
            raw_obj = None
        json_img_id = new_id("img")
        json_raw_storage = f"{raw_tree_base}/{rel}"
        db.add(
            DatasetImageAsset(
                image_id=json_img_id,
                directory_id=directory.directory_id,
                patient_id=pid or "unknown",
                survey_date=sdate or "1970-01-01",
                image_type="OCT",
                image_name=Path(rel).name,
                source_type="OCT_JSON",
                thumbnail_path=f"/api/v1/dataset-files/{json_img_id}/thumbnail",
                preview_path=f"/api/v1/dataset-files/{json_img_id}/preview",
                original_path=json_raw_storage,
                parsed_path=None,
            )
        )
        db.add(
            DatasetImageMetadata(
                image_id=json_img_id,
                metadata_json={
                    "vendor": "newvision",
                    "sourceType": "OCT_JSON",
                    "sourcePath": rel,
                    "parserVersion": "newvision-v1.1.0",
                    "raw": raw_obj,
                    "flattened": scalar_for_cols,
                },
                acquisition_datetime=f"{sdate} 10:00:00" if sdate else None,
            )
        )
        asset_count += 1

        _register_oct_json_columns(scalar_for_cols)
        _merge_oct_fields_into_questionnaire_row(
            db,
            directory.directory_id,
            pid or "",
            (sdate or "").strip(),
            scalar_for_cols,
        )

    db.flush()

    for rel in sorted(extracted_rels):
        rel_lower = rel.lower()
        rel_norm = rel.replace("\\", "/")

        if rel_lower.endswith(".fdt"):
            pid = _guess_pid_from_relative(rel, known_pids)
            sdate = _survey_date_from_rel(rel) or (
                min(patient_dates[pid]) if pid in patient_dates and patient_dates[pid] else ""
            )
            img_id = new_id("img")
            raw_bytes = storage.get_bytes(f"{raw_tree_base}/{rel}")
            jpg_bytes, jpeg_ok = fundus_fdt_maybe_jpeg(raw_bytes)
            jpg_rel_posix = Path(rel_norm).with_suffix(".jpg").as_posix()
            jpg_storage: str | None = None
            if jpeg_ok and jpg_bytes is not None:
                jpg_storage = f"{raw_tree_base}/{jpg_rel_posix}"
                storage.put_bytes(jpg_storage, jpg_bytes)
            else:
                warning_count += 1
                db.add(
                    DatasetImportWarning(
                        import_task_id=task.import_task_id,
                        directory_id=directory.directory_id,
                        warning_type="FUNDUS_FDT_NOT_JPEG",
                        message=f"眼底 FDT 非 JPEG 魔数，已保留 .fdt，未生成 .jpg: {rel}",
                        detail={"path": rel, "jpegMagicMatched": False},
                    )
                )
            raw_storage = f"/dataset/import/raw_tree/{directory.directory_id}/{rel}"
            db.add(
                DatasetImageAsset(
                    image_id=img_id,
                    directory_id=directory.directory_id,
                    patient_id=pid,
                    survey_date=sdate or "1970-01-01",
                    image_type="FUNDUS",
                    image_name=Path(rel).name,
                    source_type="PARSED_FDT" if jpg_storage else "FUNDUS_FDT",
                    thumbnail_path=f"/api/v1/dataset-files/{img_id}/thumbnail",
                    preview_path=f"/api/v1/dataset-files/{img_id}/preview",
                    original_path=raw_storage,
                    parsed_path=jpg_storage,
                )
            )
            db.add(
                DatasetImageMetadata(
                    image_id=img_id,
                    metadata_json={
                        "vendor": "newvision",
                        "sourceRelPath": rel,
                        "jpegMagicMatched": jpeg_ok,
                        "sourceType": "FUNDUS_FDT",
                    },
                    acquisition_datetime=f"{sdate} 09:00:00" if sdate else None,
                )
            )
            asset_count += 1
            continue

        is_oct_dat = rel_lower.endswith(".dat") and _is_oct_path(rel)
        if is_oct_dat and should_parse_dat(rel):
            ctx = extract_oct_path_context(rel)
            pid = ctx.get("pid") or _guess_pid_from_relative(rel, known_pids)
            sdate = (
                ctx.get("check_date")
                or _survey_date_from_rel(rel)
                or (min(patient_dates[pid]) if pid in patient_dates and patient_dates[pid] else "")
            )
            check_key_date = ctx.get("check_date")
            for code in _oct_match_warnings(pid, check_key_date, known_pids, patient_dates):
                warning_count += 1
                db.add(
                    DatasetImportWarning(
                        import_task_id=task.import_task_id,
                        directory_id=directory.directory_id,
                        warning_type=code,
                        message=f"OCT DAT 关联: {code}: {rel}",
                        detail={"path": rel, "pid": pid, "checkDate": check_key_date},
                    )
                )
            img_id = new_id("img")
            try:
                raw_dat = storage.get_bytes(f"{raw_tree_base}/{rel}")
                tag = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:24]
                frames_base = (
                    f"/dataset/import/parsed/{directory.directory_id}/oct_frames/{tag}/"
                    f"{Path(rel).stem}.frames"
                )
                storage.mkdir_p(frames_base)

                def _write_frame(name: str, data: bytes) -> None:
                    storage.put_bytes(f"{frames_base}/{name}", data)

                oct_result = parse_oct_dat_bytes(
                    raw_dat,
                    write_frame=_write_frame,
                    source_path_for_meta=rel,
                )
            except Exception as exc:  # noqa: BLE001
                warning_count += 1
                db.add(
                    DatasetImportWarning(
                        import_task_id=task.import_task_id,
                        directory_id=directory.directory_id,
                        warning_type="FILE_PARSE_FAILED",
                        message=f"OCT DAT 解析失败: {rel}",
                        detail={"path": rel, "error": str(exc)},
                    )
                )
                continue

            if oct_result.get("limitExceeded"):
                warning_count += 1
                db.add(
                    DatasetImportWarning(
                        import_task_id=task.import_task_id,
                        directory_id=directory.directory_id,
                        warning_type="FILE_PARSE_LIMIT_EXCEEDED",
                        message=f"OCT DAT 超限已跳过帧导出: {rel}",
                        detail={"path": rel, "payload": oct_result},
                    )
                )
            elif (oct_result.get("warnings") or None) and "FILE_PARSE_LIMIT_EXCEEDED" in (
                oct_result.get("warnings") or []
            ):
                warning_count += 1
                db.add(
                    DatasetImportWarning(
                        import_task_id=task.import_task_id,
                        directory_id=directory.directory_id,
                        warning_type="FILE_PARSE_LIMIT_EXCEEDED",
                        message=f"OCT DAT 仅导出部分帧: {rel}",
                        detail={"path": rel},
                    )
                )

            frame_names = list(oct_result.get("frames") or [])
            first_png: str | None = None
            if frame_names:
                first_png = f"{frames_base}/{frame_names[0]}"

            json_rel = Path(rel).with_suffix(".json").as_posix()
            meta = {
                "vendor": "newvision",
                "sourceRelPath": rel,
                "octDat": oct_result,
                "parser": "newvision_oct",
            }
            scalar_for_cols: dict[str, Any] = {}
            if json_rel in extracted_set:
                try:
                    jraw = storage.get_bytes(f"{raw_tree_base}/{json_rel}")
                    scalar_for_cols = image_stubs.oct_json_scalar_columns(jraw)
                    meta = {**meta, **scalar_for_cols}
                except Exception:  # noqa: BLE001
                    warning_count += 1
                    db.add(
                        DatasetImportWarning(
                            import_task_id=task.import_task_id,
                            directory_id=directory.directory_id,
                            warning_type="OCT_JSON_PARSE_FAILED",
                            message=f"OCT json 解析失败: {json_rel}",
                            detail={"path": json_rel},
                        )
                    )
            if scalar_for_cols:
                _register_oct_json_columns(scalar_for_cols)
                _merge_oct_fields_into_questionnaire_row(
                    db,
                    directory.directory_id,
                    pid or "",
                    (sdate or "").strip(),
                    scalar_for_cols,
                )

            raw_storage = f"/dataset/import/raw_tree/{directory.directory_id}/{rel}"
            db.add(
                DatasetImageAsset(
                    image_id=img_id,
                    directory_id=directory.directory_id,
                    patient_id=pid or "unknown",
                    survey_date=sdate or "1970-01-01",
                    image_type="OCT",
                    image_name=Path(rel).name,
                    source_type="PARSED_OCT_DAT" if first_png else "PARSED_DAT",
                    thumbnail_path=f"/api/v1/dataset-files/{img_id}/thumbnail",
                    preview_path=f"/api/v1/dataset-files/{img_id}/preview",
                    original_path=raw_storage,
                    parsed_path=first_png,
                )
            )
            db.add(
                DatasetImageMetadata(
                    image_id=img_id,
                    metadata_json=meta,
                    acquisition_datetime=f"{sdate} 10:00:00" if sdate else None,
                )
            )
            asset_count += 1

    directory.record_count = len(qres.rows)
    directory.warning_count = warning_count
    directory.import_status = "SUCCESS"
    directory.imported_at = dt.datetime.utcnow()
    directory.failure_reason = None

    task.status = "SUCCESS"
    task.progress = 100
    task.stage = "DONE"
    task.record_count = len(qres.rows)
    task.asset_count = asset_count
    task.warning_count = warning_count
    task.failure_reason = None
