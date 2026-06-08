from __future__ import annotations

import argparse
import json
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.app.core.config import get_settings
from backend.app.db.models import (
    DatasetDirectory,
    DatasetDynamicColumn,
    DatasetImageAsset,
    DatasetMergedFile,
    DatasetQuestionnaireRecord,
)
from backend.app.db.session import get_session_factory
from backend.app.parsers.newvision_analysis import (
    is_newvision_analysis_result_path,
    parse_newvision_analysis_result_bytes,
)
from backend.app.parsers.newvision_oct import extract_oct_path_context
from backend.app.services.import_pipeline import (
    _decode_zip_member_name,
    _guess_pid_from_relative,
    _is_ignored_zip_member,
    _is_safe_zip_member,
    _survey_date_from_rel,
)
from backend.app.storage.backend import StorageBackend, get_storage

SourceMode = Literal["assets", "zips", "both"]


@dataclass
class BackfillSummary:
    dry_run: bool
    scanned_assets: int = 0
    scanned_zip_members: int = 0
    analysis_sources: int = 0
    parsed_sources: int = 0
    duplicate_sources_skipped: int = 0
    matched_records: int = 0
    unmatched_sources: int = 0
    records_changed: int = 0
    fields_added: int = 0
    fields_filled_empty: int = 0
    fields_overwritten: int = 0
    fields_skipped_existing: int = 0
    columns_added: int = 0
    source_zips_missing: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


@dataclass
class _BackfillContext:
    db: Session
    storage: StorageBackend
    overwrite_existing: bool
    max_error_samples: int
    summary: BackfillSummary
    processed_sources: set[tuple[str, str, str, str]] = field(default_factory=set)
    records_by_directory: dict[str, list[DatasetQuestionnaireRecord]] = field(default_factory=dict)
    column_keys_by_directory: dict[str, set[str]] = field(default_factory=dict)
    next_column_order_by_directory: dict[str, int] = field(default_factory=dict)
    sources_processed: int = 0


def run_backfill(
    db: Session,
    storage: StorageBackend,
    *,
    directory_ids: set[str] | None = None,
    source: SourceMode = "both",
    apply: bool = False,
    overwrite_existing: bool = False,
    limit: int | None = None,
    max_error_samples: int = 20,
) -> BackfillSummary:
    summary = BackfillSummary(dry_run=not apply)
    ctx = _BackfillContext(
        db=db,
        storage=storage,
        overwrite_existing=overwrite_existing,
        max_error_samples=max_error_samples,
        summary=summary,
    )
    try:
        if source in ("assets", "both"):
            _process_assets(ctx, directory_ids=directory_ids, limit=limit)
        if source in ("zips", "both") and not _limit_reached(ctx, limit):
            _process_source_zips(ctx, directory_ids=directory_ids, limit=limit)
        if apply:
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    return summary


def _limit_reached(ctx: _BackfillContext, limit: int | None) -> bool:
    return limit is not None and ctx.sources_processed >= limit


def _record_error(ctx: _BackfillContext, message: str) -> None:
    ctx.summary.errors += 1
    if len(ctx.summary.error_samples) < ctx.max_error_samples:
        ctx.summary.error_samples.append(message)


def _process_assets(
    ctx: _BackfillContext,
    *,
    directory_ids: set[str] | None,
    limit: int | None,
) -> None:
    stmt = select(DatasetImageAsset).where(
        DatasetImageAsset.deleted == False,  # noqa: E712
        DatasetImageAsset.source_type == "OCT_JSON",
    )
    if directory_ids:
        stmt = stmt.where(DatasetImageAsset.directory_id.in_(directory_ids))
    stmt = stmt.order_by(DatasetImageAsset.directory_id, DatasetImageAsset.patient_id, DatasetImageAsset.image_id)

    for asset in ctx.db.execute(stmt).scalars():
        if _limit_reached(ctx, limit):
            return
        ctx.summary.scanned_assets += 1
        source_path = asset.image_name or asset.original_path
        if not is_newvision_analysis_result_path(source_path):
            continue
        ctx.summary.analysis_sources += 1
        try:
            raw = ctx.storage.get_bytes(asset.original_path)
        except Exception as exc:  # noqa: BLE001
            _record_error(ctx, f"asset read failed image_id={asset.image_id}: {exc}")
            continue
        _process_analysis_payload(
            ctx,
            directory_id=asset.directory_id,
            patient_id=asset.patient_id,
            survey_date=asset.survey_date,
            source_path=source_path,
            raw=raw,
        )


def _process_source_zips(
    ctx: _BackfillContext,
    *,
    directory_ids: set[str] | None,
    limit: int | None,
) -> None:
    stmt = select(DatasetDirectory).where(DatasetDirectory.deleted == False)  # noqa: E712
    if directory_ids:
        stmt = stmt.where(DatasetDirectory.directory_id.in_(directory_ids))
    stmt = stmt.order_by(DatasetDirectory.directory_id)

    for directory in ctx.db.execute(stmt).scalars():
        if _limit_reached(ctx, limit):
            return
        zip_path = _resolve_source_zip_path(ctx.db, ctx.storage, directory)
        if not zip_path:
            ctx.summary.source_zips_missing += 1
            continue
        _process_one_source_zip(ctx, directory, zip_path, limit=limit)


def _resolve_source_zip_path(
    db: Session,
    storage: StorageBackend,
    directory: DatasetDirectory,
) -> str | None:
    canonical = f"/dataset/import/raw_zip/{directory.directory_id}/source.zip"
    try:
        if storage.exists(canonical):
            return canonical
    except Exception:
        pass
    if directory.raw_zip_file_id:
        merged = db.get(DatasetMergedFile, directory.raw_zip_file_id)
        if merged and merged.ftp_path:
            return merged.ftp_path
    return None


def _process_one_source_zip(
    ctx: _BackfillContext,
    directory: DatasetDirectory,
    zip_path: str,
    *,
    limit: int | None,
) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix="newvision-analysis-backfill-", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        ctx.storage.fetch_to_local(zip_path, tmp_path)
        with zipfile.ZipFile(tmp_path, "r") as zf:
            for member in zf.infolist():
                if _limit_reached(ctx, limit):
                    return
                if member.is_dir():
                    continue
                decoded_name = _decode_zip_member_name(member)
                if not _is_safe_zip_member(decoded_name) or _is_ignored_zip_member(decoded_name):
                    continue
                rel = decoded_name.replace("\\", "/").lstrip("/")
                ctx.summary.scanned_zip_members += 1
                if not is_newvision_analysis_result_path(rel):
                    continue
                ctx.summary.analysis_sources += 1
                try:
                    with zf.open(member, "r") as inp:
                        raw = inp.read()
                except Exception as exc:  # noqa: BLE001
                    _record_error(ctx, f"zip read failed directory_id={directory.directory_id} rel={rel}: {exc}")
                    continue
                patient_id, survey_date = _match_patient_date_from_relative(ctx, directory.directory_id, rel)
                _process_analysis_payload(
                    ctx,
                    directory_id=directory.directory_id,
                    patient_id=patient_id,
                    survey_date=survey_date,
                    source_path=rel,
                    raw=raw,
                )
    except zipfile.BadZipFile as exc:
        _record_error(ctx, f"bad source zip directory_id={directory.directory_id} path={zip_path}: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record_error(ctx, f"source zip failed directory_id={directory.directory_id} path={zip_path}: {exc}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _match_patient_date_from_relative(
    ctx: _BackfillContext,
    directory_id: str,
    rel: str,
) -> tuple[str, str]:
    records = _records_for_directory(ctx, directory_id)
    known_pids = {r.patient_id for r in records}
    ctx_values = extract_oct_path_context(rel)
    patient_id = ctx_values.get("pid") or _guess_pid_from_relative(rel, known_pids)
    survey_date = ctx_values.get("check_date") or _survey_date_from_rel(rel)
    return patient_id, survey_date


def _process_analysis_payload(
    ctx: _BackfillContext,
    *,
    directory_id: str,
    patient_id: str,
    survey_date: str,
    source_path: str,
    raw: bytes,
) -> None:
    source_key = (
        directory_id,
        patient_id or "",
        survey_date or "",
        Path(str(source_path).replace("\\", "/")).name.lower(),
    )
    if source_key in ctx.processed_sources:
        ctx.summary.duplicate_sources_skipped += 1
        return
    ctx.processed_sources.add(source_key)

    try:
        fields = parse_newvision_analysis_result_bytes(raw, source_path=source_path)
    except Exception as exc:  # noqa: BLE001
        _record_error(ctx, f"parse failed directory_id={directory_id} source={source_path}: {exc}")
        return
    if not fields:
        return

    ctx.summary.parsed_sources += 1
    ctx.sources_processed += 1

    rec = _find_target_record(ctx, directory_id, patient_id, survey_date)
    if not rec:
        ctx.summary.unmatched_sources += 1
        _record_error(
            ctx,
            f"record not matched directory_id={directory_id} patient_id={patient_id} "
            f"survey_date={survey_date} source={source_path}",
        )
        return
    ctx.summary.matched_records += 1

    _register_missing_columns(ctx, directory_id, fields)
    added, filled_empty, overwritten, skipped = _merge_fields(
        rec,
        fields,
        overwrite_existing=ctx.overwrite_existing,
    )
    ctx.summary.fields_added += added
    ctx.summary.fields_filled_empty += filled_empty
    ctx.summary.fields_overwritten += overwritten
    ctx.summary.fields_skipped_existing += skipped
    if added or filled_empty or overwritten:
        ctx.summary.records_changed += 1
        flag_modified(rec, "normalized_row_json")


def _records_for_directory(
    ctx: _BackfillContext,
    directory_id: str,
) -> list[DatasetQuestionnaireRecord]:
    cached = ctx.records_by_directory.get(directory_id)
    if cached is not None:
        return cached
    rows = list(
        ctx.db.execute(
            select(DatasetQuestionnaireRecord)
            .where(
                DatasetQuestionnaireRecord.directory_id == directory_id,
                DatasetQuestionnaireRecord.deleted == False,  # noqa: E712
            )
            .order_by(DatasetQuestionnaireRecord.patient_id, DatasetQuestionnaireRecord.survey_date)
        ).scalars()
    )
    ctx.records_by_directory[directory_id] = rows
    return rows


def _find_target_record(
    ctx: _BackfillContext,
    directory_id: str,
    patient_id: str,
    survey_date: str,
) -> DatasetQuestionnaireRecord | None:
    records = _records_for_directory(ctx, directory_id)
    patient_records = [r for r in records if r.patient_id == patient_id]
    clean_date = (survey_date or "").strip()
    if clean_date:
        for rec in patient_records:
            if (rec.survey_date or "").strip() == clean_date:
                return rec
    if len(patient_records) == 1:
        return patient_records[0]
    if len(records) == 1:
        return records[0]
    return None


def _merge_fields(
    rec: DatasetQuestionnaireRecord,
    fields: dict[str, Any],
    *,
    overwrite_existing: bool,
) -> tuple[int, int, int, int]:
    cells = dict(rec.normalized_row_json or {})
    added = 0
    filled_empty = 0
    overwritten = 0
    skipped = 0
    for key, value in fields.items():
        if key not in cells:
            cells[key] = value
            added += 1
            continue
        existing = cells.get(key)
        if _is_empty_value(existing) and not _is_empty_value(value):
            cells[key] = value
            filled_empty += 1
            continue
        if overwrite_existing and existing != value:
            cells[key] = value
            overwritten += 1
            continue
        skipped += 1
    rec.normalized_row_json = cells
    return added, filled_empty, overwritten, skipped


def _is_empty_value(value: Any) -> bool:
    return value is None or value == ""


def _register_missing_columns(
    ctx: _BackfillContext,
    directory_id: str,
    fields: dict[str, Any],
) -> None:
    if directory_id not in ctx.column_keys_by_directory:
        existing_rows = list(
            ctx.db.execute(
                select(DatasetDynamicColumn).where(DatasetDynamicColumn.directory_id == directory_id)
            ).scalars()
        )
        ctx.column_keys_by_directory[directory_id] = {r.column_key for r in existing_rows}
        max_order = ctx.db.scalar(
            select(func.max(DatasetDynamicColumn.display_order)).where(
                DatasetDynamicColumn.directory_id == directory_id
            )
        )
        ctx.next_column_order_by_directory[directory_id] = 0 if max_order is None else int(max_order) + 1

    known_keys = ctx.column_keys_by_directory[directory_id]
    next_order = ctx.next_column_order_by_directory[directory_id]
    for key, value in fields.items():
        if key in known_keys:
            continue
        ctx.db.add(
            DatasetDynamicColumn(
                directory_id=directory_id,
                column_key=key,
                column_title=_column_title(key),
                data_type=_infer_data_type(value),
                source_type="OCT_JSON",
                display_order=next_order,
            )
        )
        known_keys.add(key)
        next_order += 1
        ctx.summary.columns_added += 1
    ctx.next_column_order_by_directory[directory_id] = next_order


def _column_title(column_key: str) -> str:
    return (column_key.replace("oct_", "").replace("_", " ").strip() or column_key)[:120]


def _infer_data_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, (int, float)):
        return "NUMBER"
    return "STRING"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill NewVision *_AnalysisRlt.json fields into questionnaire record cells."
    )
    parser.add_argument("--directory-id", action="append", default=[], help="Limit to one directory id; repeatable.")
    parser.add_argument("--source", choices=("assets", "zips", "both"), default="both")
    parser.add_argument("--apply", action="store_true", help="Commit changes. Without this flag the run is dry-run.")
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Overwrite non-empty existing cells for parsed NewVision fields.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum parsed analysis JSON sources to process.")
    parser.add_argument("--max-error-samples", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    storage = get_storage(settings)
    db = get_session_factory()()
    try:
        summary = run_backfill(
            db,
            storage,
            directory_ids=set(args.directory_id) or None,
            source=args.source,
            apply=args.apply,
            overwrite_existing=args.overwrite_existing,
            limit=args.limit,
            max_error_samples=args.max_error_samples,
        )
        print(json.dumps(asdict(summary), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
