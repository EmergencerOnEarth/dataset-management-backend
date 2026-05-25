#!/usr/bin/env python3
"""Manually expire an export record and trigger the expiry sweep (local / test use).

Examples:
  # Mark exp_xxx as expired in DB and run one sweep batch
  python3 scripts/trigger_export_expiry_test.py --export-record-id exp_abc123

  # Only move expire_at to the past (sweep runs on next startup / interval)
  python3 scripts/trigger_export_expiry_test.py --export-record-id exp_abc123 --set-expired-only

  # Sweep all currently overdue DONE exports
  python3 scripts/trigger_export_expiry_test.py --sweep-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.app.core.config import get_settings
from backend.app.db.models import ExportRecord
from backend.app.db.session import get_session_factory, init_db
from backend.app.workers.export_expiry import sweep_expired_exports


def _backdate_export(export_record_id: str) -> None:
    factory = get_session_factory()
    db = factory()
    try:
        rec = db.get(ExportRecord, export_record_id)
        if not rec:
            raise SystemExit(f"export record not found: {export_record_id}")
        rec.expire_at = dt.datetime.utcnow() - dt.timedelta(minutes=1)
        db.commit()
        print(f"backdated expire_at for {export_record_id} -> {rec.expire_at}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Trigger export expiry cleanup for testing.")
    parser.add_argument("--export-record-id", help="Target exportRecordId to backdate")
    parser.add_argument(
        "--set-expired-only",
        action="store_true",
        help="Only set expire_at to the past; do not run sweep",
    )
    parser.add_argument(
        "--sweep-only",
        action="store_true",
        help="Run expiry sweep without backdating a specific record",
    )
    args = parser.parse_args()

    get_settings.cache_clear()
    init_db()

    if args.export_record_id:
        _backdate_export(args.export_record_id)
        if args.set_expired_only:
            return

    if args.export_record_id or args.sweep_only:
        cleaned = sweep_expired_exports()
        print(f"sweep cleaned {cleaned} record(s)")
        return

    parser.error("provide --export-record-id and/or --sweep-only")


if __name__ == "__main__":
    main()
