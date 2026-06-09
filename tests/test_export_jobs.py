from __future__ import annotations

import binascii
import csv
import json
import io
import struct
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.app.core.config import get_settings
from backend.app.db.models import (
    DatasetDirectory,
    DatasetDynamicColumn,
    DatasetImageAsset,
    DatasetQuestionnaireRecord,
    ExportRecord,
)
from backend.app.db.session import get_session_factory
from backend.app.services import directory_service
from backend.app.services.export_jobs import (
    ExportSourceZipMissing,
    _append_directory_tree_from_source,
    run_directory_export_job,
    run_patient_export_job,
)
from backend.app.storage.backend import get_storage


def test_append_directory_tree_requires_source_zip():
    st = MagicMock()
    st.exists.return_value = False
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        with pytest.raises(ExportSourceZipMissing):
            _append_directory_tree_from_source(
                zf, st, MagicMock(), "dir_missing", arc_prefix="缺失目录"
            )


def _stored_zip_with_raw_filename(filename_bytes: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    local = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        20,
        0,
        0,
        0,
        0,
        crc,
        len(payload),
        len(payload),
        len(filename_bytes),
        0,
    ) + filename_bytes + payload
    central_offset = len(local)
    central = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        20,
        20,
        0,
        0,
        0,
        0,
        crc,
        len(payload),
        len(payload),
        len(filename_bytes),
        0,
        0,
        0,
        0,
        0,
        0,
    ) + filename_bytes
    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        1,
        1,
        len(central),
        central_offset,
        0,
    )
    return local + central + eocd


def test_append_directory_tree_decodes_gbk_source_zip_names():
    raw_zip = _stored_zip_with_raw_filename("问卷/问卷.xlsx".encode("gbk"), b"xlsx-bytes")
    st = MagicMock()
    st.exists.return_value = True
    st.get_bytes.return_value = raw_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _append_directory_tree_from_source(
            zf,
            st,
            MagicMock(),
            "dir_gbk",
            arc_prefix="导出目录",
        )

    names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
    assert "导出目录/问卷/问卷.xlsx" in names
    assert not any("╬" in name for name in names)


def _make_source_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _read_csv_member(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    text = zf.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _add_questionnaire_columns(db, directory_id: str) -> None:
    for i, title in enumerate(["患者ID", "调查日期"]):
        db.add(
            DatasetDynamicColumn(
                directory_id=directory_id,
                column_key=f"q_{i}",
                column_title=title,
                data_type="STRING",
                source_type="QUESTIONNAIRE",
                display_order=i,
            )
        )


def test_directory_export_writes_merged_questionnaire_rows():
    directory_id = "dir_test_export_merged_rows"
    export_id = "exp_test_export_merged_rows"
    source_zip_path = f"/dataset/import/raw_zip/{directory_id}/source.zip"
    storage = get_storage(get_settings())
    storage.put_bytes(source_zip_path, _make_source_zip({"问卷/source.xlsx": b"xlsx"}))

    session_factory = get_session_factory()
    with session_factory() as db:
        db.merge(
            DatasetDirectory(
                directory_id=directory_id,
                directory_name="合并字段目录",
                description="",
                import_status="SUCCESS",
                record_count=1,
                warning_count=0,
                raw_zip_file_id=None,
                deleted=False,
            )
        )
        db.merge(
            DatasetQuestionnaireRecord(
                record_id="rec_test_export_merged_rows",
                directory_id=directory_id,
                patient_id="P_MERGED",
                survey_date="2026-06-01",
                raw_row_json={"q_0": "P_MERGED", "q_1": "2026-06-01"},
                normalized_row_json={
                    "q_0": "P_MERGED",
                    "q_1": "2026-06-01",
                    "patientId": "P_MERGED",
                    "surveyDate": "2026-06-01",
                    "od_Macular_Avg_Thickness_Nine_Central_Subfield": 230.81205235860708,
                },
                deleted=False,
            )
        )
        _add_questionnaire_columns(db, directory_id)
        db.merge(
            ExportRecord(
                export_record_id=export_id,
                export_type="DATASET_DIRECTORY",
                export_status="PREPARING",
                file_name="合并字段目录.zip",
                ftp_path=None,
                expire_at=datetime.now(timezone.utc) + timedelta(days=1),
                download_count=0,
                payload_json={
                    "directoryIds": [directory_id],
                    "options": {"includeParsedImages": False},
                },
            )
        )
        db.commit()

    run_directory_export_job(storage, export_id)

    with session_factory() as db:
        rec = db.get(ExportRecord, export_id)
        assert rec and rec.export_status == "DONE", rec.failure_reason if rec else None
        assert rec.ftp_path
        blob = storage.get_bytes(rec.ftp_path)

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        assert "合并字段目录/questionnaire_rows.json" in names
        rows = json.loads(zf.read("合并字段目录/questionnaire_rows.json"))
        assert "合并字段目录/merged_questionnaire.csv" in names
        csv_rows = _read_csv_member(zf, "合并字段目录/merged_questionnaire.csv")

    assert rows == [
        {
            "q_0": "P_MERGED",
            "q_1": "2026-06-01",
            "patientId": "P_MERGED",
            "surveyDate": "2026-06-01",
            "od_Macular_Avg_Thickness_Nine_Central_Subfield": 230.81205235860708,
        }
    ]
    assert csv_rows == [
        {
            "患者ID": "P_MERGED",
            "调查日期": "2026-06-01",
            "JSON解析数据": json.dumps(
                {"od_Macular_Avg_Thickness_Nine_Central_Subfield": 230.81205235860708},
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
    ]


def test_patient_export_includes_patient_source_members_and_merged_fields():
    directory_id = "dir_test_patient_raw_members"
    export_id = "exp_test_patient_raw_members"
    source_zip_path = f"/dataset/import/raw_zip/{directory_id}/source.zip"
    storage = get_storage(get_settings())
    storage.put_bytes(
        source_zip_path,
        _make_source_zip(
            {
                "OCT/P_RAW/2026-06-01/report.bmp": b"BM-patient",
                "OCT/P_RAW/2026-06-01/capture.png": b"\x89PNG\r\n\x1a\npatient",
                "OCT/P_RAW/2026-06-02/other-day.bmp": b"BM-other-day",
                "OCT/P_OTHER/2026-06-01/report.bmp": b"BM-other-patient",
            }
        ),
    )

    session_factory = get_session_factory()
    with session_factory() as db:
        db.merge(
            DatasetDirectory(
                directory_id=directory_id,
                directory_name="患者原图目录",
                description="",
                import_status="SUCCESS",
                record_count=1,
                warning_count=0,
                raw_zip_file_id=None,
                deleted=False,
            )
        )
        db.merge(
            DatasetQuestionnaireRecord(
                record_id="rec_test_patient_raw_members",
                directory_id=directory_id,
                patient_id="P_RAW",
                survey_date="2026-06-01",
                raw_row_json={"q_0": "P_RAW", "q_1": "2026-06-01"},
                normalized_row_json={
                    "q_0": "P_RAW",
                    "q_1": "2026-06-01",
                    "patientId": "P_RAW",
                    "surveyDate": "2026-06-01",
                    "os_MacularGCC_Avg_Thickness_Six_Temporal_Superior": 86.5296483909416,
                },
                deleted=False,
            )
        )
        _add_questionnaire_columns(db, directory_id)
        db.merge(
            ExportRecord(
                export_record_id=export_id,
                export_type="DATASET_PATIENT",
                export_status="PREPARING",
                file_name="患者数据导出-P_RAW.zip",
                ftp_path=None,
                expire_at=datetime.now(timezone.utc) + timedelta(days=1),
                download_count=0,
                payload_json={
                    "directoryId": directory_id,
                    "patientId": "P_RAW",
                    "options": {
                        "includeOriginalAttachments": True,
                        "includeParsedImages": False,
                        "surveyDates": ["2026-06-01"],
                    },
                },
            )
        )
        db.commit()

    run_patient_export_job(storage, export_id)

    with session_factory() as db:
        rec = db.get(ExportRecord, export_id)
        assert rec and rec.export_status == "DONE", rec.failure_reason if rec else None
        assert rec.ftp_path
        blob = storage.get_bytes(rec.ftp_path)

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        rows = json.loads(zf.read("questionnaire_rows.json"))
        csv_rows = _read_csv_member(zf, "patient_merged_questionnaire.csv")

    assert rows[0]["os_MacularGCC_Avg_Thickness_Six_Temporal_Superior"] == 86.5296483909416
    assert csv_rows[0]["目录名称"] == "患者原图目录"
    assert csv_rows[0]["患者ID"] == "P_RAW"
    assert csv_rows[0]["调查日期"] == "2026-06-01"
    assert json.loads(csv_rows[0]["JSON解析数据"]) == {
        "os_MacularGCC_Avg_Thickness_Six_Temporal_Superior": 86.5296483909416
    }
    assert "images/OCT/P_RAW/2026-06-01/report.bmp" in names
    assert "images/OCT/P_RAW/2026-06-01/capture.png" in names
    assert "images/OCT/P_RAW/2026-06-02/other-day.bmp" not in names
    assert "images/OCT/P_OTHER/2026-06-01/report.bmp" not in names


def test_patient_export_aggregates_same_patient_across_directories():
    dir_a = "dir_test_patient_cross_a"
    dir_b = "dir_test_patient_cross_b"
    export_id = "exp_test_patient_cross_dirs"
    patient_id = "P_CROSS"
    storage = get_storage(get_settings())
    storage.put_bytes(
        f"/dataset/import/raw_zip/{dir_a}/source.zip",
        _make_source_zip({"OCT/P_CROSS/2026-06-01/a.bmp": b"BM-a"}),
    )
    storage.put_bytes(
        f"/dataset/import/raw_zip/{dir_b}/source.zip",
        _make_source_zip({"OCT/P_CROSS/2026-07-01/b.bmp": b"BM-b"}),
    )

    session_factory = get_session_factory()
    with session_factory() as db:
        for directory_id, directory_name in [(dir_a, "跨目录A"), (dir_b, "跨目录B")]:
            db.merge(
                DatasetDirectory(
                    directory_id=directory_id,
                    directory_name=directory_name,
                    description="",
                    import_status="SUCCESS",
                    record_count=1,
                    warning_count=0,
                    raw_zip_file_id=None,
                    deleted=False,
                )
            )
            _add_questionnaire_columns(db, directory_id)
        db.merge(
            DatasetQuestionnaireRecord(
                record_id="rec_test_patient_cross_a",
                directory_id=dir_a,
                patient_id=patient_id,
                survey_date="2026-06-01",
                raw_row_json={"q_0": patient_id, "q_1": "2026-06-01"},
                normalized_row_json={
                    "q_0": patient_id,
                    "q_1": "2026-06-01",
                    "patientId": patient_id,
                    "surveyDate": "2026-06-01",
                    "od_Macular_Avg_Thickness_Nine_Central_Subfield": 210.5,
                },
                deleted=False,
            )
        )
        db.merge(
            DatasetQuestionnaireRecord(
                record_id="rec_test_patient_cross_b",
                directory_id=dir_b,
                patient_id=patient_id,
                survey_date="2026-07-01",
                raw_row_json={"q_0": patient_id, "q_1": "2026-07-01"},
                normalized_row_json={
                    "q_0": patient_id,
                    "q_1": "2026-07-01",
                    "patientId": patient_id,
                    "surveyDate": "2026-07-01",
                    "os_MacularGCC_Avg_Thickness_Six_Temporal_Superior": 88.2,
                },
                deleted=False,
            )
        )
        db.merge(
            DatasetImageAsset(
                image_id="img_test_patient_cross_a",
                directory_id=dir_a,
                patient_id=patient_id,
                survey_date="2026-06-01",
                image_type="OCT",
                image_name="a.bmp",
                source_type="OCT_BMP",
                thumbnail_path="/thumb/a.jpg",
                preview_path="/preview/a.jpg",
                original_path=f"/dataset/import/raw_tree/{dir_a}/OCT/P_CROSS/2026-06-01/a.bmp",
                parsed_path=None,
                deleted=False,
            )
        )
        db.merge(
            DatasetImageAsset(
                image_id="img_test_patient_cross_b",
                directory_id=dir_b,
                patient_id=patient_id,
                survey_date="2026-07-01",
                image_type="OCT",
                image_name="b.bmp",
                source_type="OCT_BMP",
                thumbnail_path="/thumb/b.jpg",
                preview_path="/preview/b.jpg",
                original_path=f"/dataset/import/raw_tree/{dir_b}/OCT/P_CROSS/2026-07-01/b.bmp",
                parsed_path=None,
                deleted=False,
            )
        )
        db.merge(
            ExportRecord(
                export_record_id=export_id,
                export_type="DATASET_PATIENT",
                export_status="PREPARING",
                file_name="患者数据导出-P_CROSS.zip",
                ftp_path=None,
                expire_at=datetime.now(timezone.utc) + timedelta(days=1),
                download_count=0,
                payload_json={
                    "directoryId": dir_a,
                    "patientId": patient_id,
                    "options": {
                        "includeOriginalAttachments": True,
                        "includeParsedImages": False,
                    },
                },
            )
        )
        db.commit()

        timeline = directory_service.patient_timeline(db, dir_a, patient_id)
        assert [d["surveyDate"] for d in timeline["dates"]] == ["2026-07-01", "2026-06-01"]
        assert timeline["dates"][0]["directories"] == [
            {"directoryId": dir_b, "directoryName": "跨目录B", "imageCount": 1}
        ]

        image_page = directory_service.patient_images(db, dir_a, patient_id, "2026-07-01", 1, 10)
        assert image_page["records"][0]["directoryId"] == dir_b
        assert image_page["records"][0]["directoryName"] == "跨目录B"

        detail = directory_service.image_detail(db, dir_a, patient_id, "img_test_patient_cross_b")
        assert detail["directoryId"] == dir_b

    run_patient_export_job(storage, export_id)

    with session_factory() as db:
        rec = db.get(ExportRecord, export_id)
        assert rec and rec.export_status == "DONE", rec.failure_reason if rec else None
        assert rec.ftp_path
        blob = storage.get_bytes(rec.ftp_path)

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        csv_rows = _read_csv_member(zf, "patient_merged_questionnaire.csv")

    assert len(csv_rows) == 2
    assert [r["目录名称"] for r in csv_rows] == ["跨目录A", "跨目录B"]
    assert [r["调查日期"] for r in csv_rows] == ["2026-06-01", "2026-07-01"]
    assert json.loads(csv_rows[0]["JSON解析数据"]) == {
        "od_Macular_Avg_Thickness_Nine_Central_Subfield": 210.5
    }
    assert json.loads(csv_rows[1]["JSON解析数据"]) == {
        "os_MacularGCC_Avg_Thickness_Six_Temporal_Superior": 88.2
    }
    assert "images/跨目录A/OCT/P_CROSS/2026-06-01/a.bmp" in names
    assert "images/跨目录B/OCT/P_CROSS/2026-07-01/b.bmp" in names
