from __future__ import annotations

import io
import zipfile
from pathlib import Path

from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.db.models import (
    DatasetDirectory,
    DatasetDynamicColumn,
    DatasetImageAsset,
    DatasetMergedFile,
    DatasetQuestionnaireRecord,
)
from backend.app.db.session import get_session_factory
from backend.app.storage.backend import get_storage
from scripts.backfill_newvision_analysis_results import run_backfill


def _sample_json(name: str) -> bytes:
    repo = Path(__file__).resolve().parents[1]
    return (
        repo
        / "test-data/upload-samples/newvision/样例数据/院外导入样例数据/患者原始数据示例"
        / name
    ).read_bytes()


def _add_directory_with_record(
    directory_id: str,
    patient_id: str = "P_BACKFILL",
    survey_date: str = "2026-03-03",
    cells: dict | None = None,
) -> None:
    db = get_session_factory()()
    try:
        db.add(
            DatasetDirectory(
                directory_id=directory_id,
                directory_name=f"backfill-{directory_id}",
                description="",
                import_status="SUCCESS",
                record_count=1,
                warning_count=0,
                deleted=False,
            )
        )
        db.add(
            DatasetQuestionnaireRecord(
                record_id=f"rec_{directory_id}",
                directory_id=directory_id,
                patient_id=patient_id,
                survey_date=survey_date,
                raw_row_json={"patientId": patient_id, "surveyDate": survey_date},
                normalized_row_json={
                    "patientId": patient_id,
                    "surveyDate": survey_date,
                    **(cells or {}),
                },
                deleted=False,
            )
        )
        db.commit()
    finally:
        db.close()


def test_backfill_asset_adds_missing_cells_without_overwriting_existing_values():
    directory_id = "dir_backfill_asset"
    existing_key = "od_DiscinfoValue_Cup_Area"
    _add_directory_with_record(directory_id, cells={existing_key: 999.0})
    storage = get_storage(get_settings())
    original_path = (
        f"/dataset/import/raw_tree/{directory_id}/OCT/P_BACKFILL/2026-03-03/"
        "od-3dscan-disc-20251021-090036-001_AnalysisRlt.json"
    )
    storage.put_bytes(original_path, _sample_json("od-3dscan-disc-20251021-090036-001_AnalysisRlt.json"))

    db = get_session_factory()()
    try:
        db.add(
            DatasetImageAsset(
                image_id="img_backfill_asset_json",
                directory_id=directory_id,
                patient_id="P_BACKFILL",
                survey_date="2026-03-03",
                image_type="OCT",
                image_name="od-3dscan-disc-20251021-090036-001_AnalysisRlt.json",
                source_type="OCT_JSON",
                thumbnail_path="/api/v1/dataset-files/img_backfill_asset_json/thumbnail",
                preview_path="/api/v1/dataset-files/img_backfill_asset_json/preview",
                original_path=original_path,
                parsed_path=None,
                deleted=False,
            )
        )
        db.commit()

        summary = run_backfill(db, storage, directory_ids={directory_id}, source="assets", apply=True)
        assert summary.parsed_sources == 1
        assert summary.records_changed == 1
        assert summary.fields_added > 0
        assert summary.fields_skipped_existing >= 1

        rec = db.execute(
            select(DatasetQuestionnaireRecord).where(
                DatasetQuestionnaireRecord.directory_id == directory_id
            )
        ).scalar_one()
        cells = rec.normalized_row_json
        assert cells[existing_key] == 999.0
        assert cells["od_DiscRNFLClockAvg_RNFL上部厚度"] == 94.675
        assert cells["od__disc_json"] == "od-3dscan-disc-20251021-090036-001_AnalysisRlt.json"

        column_keys = set(
            db.execute(
                select(DatasetDynamicColumn.column_key).where(
                    DatasetDynamicColumn.directory_id == directory_id
                )
            ).scalars()
        )
        assert "od_DiscRNFLClockAvg_RNFL上部厚度" in column_keys
        assert "od__disc_json" in column_keys
    finally:
        db.close()


def test_backfill_source_zip_dry_run_rolls_back_and_unique_record_fallback_matches():
    directory_id = "dir_backfill_zip"
    _add_directory_with_record(directory_id, patient_id="ONLY_PATIENT", survey_date="2026-05-01")
    storage = get_storage(get_settings())
    zip_buf = io.BytesIO()
    rel = "患者原始数据示例/os-radialscan-macular-20251021-090634-001_AnalysisRlt.json"
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(rel, _sample_json("os-radialscan-macular-20251021-090634-001_AnalysisRlt.json"))
    source_zip_path = f"/dataset/import/raw_zip/{directory_id}/source.zip"
    storage.put_bytes(source_zip_path, zip_buf.getvalue())

    db = get_session_factory()()
    try:
        directory = db.get(DatasetDirectory, directory_id)
        assert directory is not None
        directory.raw_zip_file_id = "mf_backfill_zip"
        db.add(
            DatasetMergedFile(
                file_id="mf_backfill_zip",
                business_type="DATASET_IMPORT",
                file_name="source.zip",
                file_size=len(zip_buf.getvalue()),
                file_hash="hash_backfill_zip",
                ftp_path=source_zip_path,
                consumed=True,
                directory_id=directory_id,
            )
        )
        db.commit()

        dry_summary = run_backfill(db, storage, directory_ids={directory_id}, source="zips", apply=False)
        assert dry_summary.dry_run is True
        assert dry_summary.parsed_sources == 1
        assert dry_summary.records_changed == 1

        rec = db.execute(
            select(DatasetQuestionnaireRecord).where(
                DatasetQuestionnaireRecord.directory_id == directory_id
            )
        ).scalar_one()
        assert "os_MacularGCC_Avg_Thickness_Six_Temporal_Superior" not in rec.normalized_row_json

        apply_summary = run_backfill(db, storage, directory_ids={directory_id}, source="zips", apply=True)
        assert apply_summary.dry_run is False
        assert apply_summary.parsed_sources == 1
        assert apply_summary.records_changed == 1

        rec = db.execute(
            select(DatasetQuestionnaireRecord).where(
                DatasetQuestionnaireRecord.directory_id == directory_id
            )
        ).scalar_one()
        assert rec.normalized_row_json["os_MacularGCC_Avg_Thickness_Six_Temporal_Superior"] == 87.82137390330409
    finally:
        db.close()
