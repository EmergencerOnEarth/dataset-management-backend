"""Optional demo rows for UI regression (``dir_demo_001``) when DB is empty."""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.db.models import (
    DatasetDirectory,
    DatasetDynamicColumn,
    DatasetImageAsset,
    DatasetImageMetadata,
    DatasetQuestionnaireRecord,
)


def ensure_demo_seed() -> None:
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    db = factory()
    try:
        if not db.get(DatasetDirectory, "dir_demo_001"):
            _insert_demo(db)
            db.commit()
        _ensure_demo_source_zip_storage()
    finally:
        db.close()


def _ensure_demo_source_zip_storage() -> None:
    """目录导出依赖 ``raw_zip/{directoryId}/source.zip``；演示数据目录需在存储上占位。"""
    from backend.app.core.config import get_settings
    from backend.app.storage.backend import get_storage

    settings = get_settings()
    storage = get_storage(settings)
    logical = "/dataset/import/raw_zip/dir_demo_001/source.zip"
    if storage.exists(logical):
        return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("demo_seed/readme.txt", b"demo zip placeholder for export smoke")
    storage.mkdir_p("/dataset/import/raw_zip/dir_demo_001")
    storage.put_bytes(logical, buf.getvalue())


def _insert_demo(db: Session) -> None:
    now = dt.datetime.utcnow()
    db.add(
        DatasetDirectory(
            directory_id="dir_demo_001",
            directory_name="中航数据2026样例目录",
            description="V1.2.7 导入结构模拟数据",
            import_status="SUCCESS",
            record_count=3,
            warning_count=2,
            imported_at=now,
            deleted=False,
        )
    )
    cols = [
        ("patientId", "患者ID", "STRING", "QUESTIONNAIRE"),
        ("surveyDate", "调查日期", "DATE", "NORMALIZED"),
        ("name", "姓名", "STRING", "QUESTIONNAIRE"),
        ("phone", "常用电话", "STRING", "QUESTIONNAIRE"),
    ]
    for i, (key, title, dtype, st) in enumerate(cols):
        db.add(
            DatasetDynamicColumn(
                directory_id="dir_demo_001",
                column_key=key,
                column_title=title,
                data_type=dtype,
                source_type=st,
                display_order=i,
            )
        )
    people = [
        ("rec_001", "郑萍", "LGTA00087", "13923779163", "2026-03-03"),
        ("rec_002", "李明", "LGTA00101", "13800001111", "2026-03-06"),
        ("rec_003", "王华", "LGTA00143", "13800002222", "2026-03-10"),
    ]
    for rid, name, pid, phone, sdate in people:
        raw = {"name": name, "patientId": pid, "phone": phone, "surveyDate": sdate}
        norm = {**raw, "patientId": pid, "surveyDate": sdate}
        db.add(
            DatasetQuestionnaireRecord(
                record_id=rid,
                directory_id="dir_demo_001",
                patient_id=pid,
                survey_date=sdate,
                raw_row_json=raw,
                normalized_row_json=norm,
            )
        )
    images = [
        ("img_001", "LGTA00087", "2026-03-03", "PARSED_FDT", "OD-Color-260303-094001.jpg"),
        ("img_002", "LGTA00101", "2026-03-06", "PARSED_DAT", "od-3dscan-macular-20260306.jpg"),
        ("img_003", "LGTA00143", "2026-03-10", "ORIGINAL_PNG", "os-3dscan-macular-Thickness.png"),
    ]
    for img_id, pid, sdate, stype, name in images:
        db.add(
            DatasetImageAsset(
                image_id=img_id,
                directory_id="dir_demo_001",
                patient_id=pid,
                survey_date=sdate,
                image_type="FUNDUS" if "fdt" in name.lower() or stype == "PARSED_FDT" else "OCT",
                image_name=name,
                source_type=stype,
                thumbnail_path=f"/api/v1/dataset-files/{img_id}/thumbnail",
                preview_path=f"/api/v1/dataset-files/{img_id}/preview",
                original_path=f"/dataset/import/raw_tree/dir_demo_001/眼底照相/{name}",
                parsed_path=(
                    f"/dataset/import/parsed/dir_demo_001/fundus/{Path(name).stem}.jpg"
                    if stype == "PARSED_FDT"
                    else (
                        f"/dataset/import/parsed/dir_demo_001/oct/{Path(name).stem}.jpg"
                        if stype == "PARSED_DAT"
                        else None
                    )
                ),
            )
        )
        db.add(
            DatasetImageMetadata(
                image_id=img_id,
                metadata_json={"width": 1024, "height": 768},
                acquisition_datetime=f"{sdate} 09:30:00",
            )
        )
