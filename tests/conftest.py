"""
Pytest bootstrap.

普通模式（默认）：SQLite + local storage，每次删旧库重建，隔离性最强。
集成模式（PYTEST_INTEGRATION=1）：使用外部注入的 MySQL+FTP 环境变量，不改写 DATABASE_URL
  / MYSQL_COMPOSE_ONLY / STORAGE_BACKEND，由调用方负责设置（见 scripts/run_local_mysql_ftp_regression.sh）。
"""

from __future__ import annotations

import os
from pathlib import Path

_INTEGRATION = os.environ.get("PYTEST_INTEGRATION") == "1"

if not _INTEGRATION:
    # 普通 pytest：强制 SQLite + local，不受 .secrets/local.env 中 TiDB DSN 影响
    os.environ.setdefault("DATABASE_URL", "sqlite:///./.runtime/pytest_dataset.db")
    os.environ.setdefault("STORAGE_BACKEND", "local")
    os.environ["MYSQL_COMPOSE_ONLY"] = "false"
else:
    # 集成模式：外部已设 MYSQL_COMPOSE_ONLY=true / STORAGE_BACKEND=ftp 等，这里不覆盖
    os.environ.setdefault("STORAGE_BACKEND", "local")  # 万一调用方忘设也有 fallback

Path(".runtime").mkdir(parents=True, exist_ok=True)

if not _INTEGRATION:
    # 结构变更时删旧 SQLite，避免缺列导致 ORM 写入失败
    _pytest_db = Path(".runtime/pytest_dataset.db")
    if _pytest_db.exists():
        _pytest_db.unlink()

from backend.app.core.config import get_settings

get_settings.cache_clear()

import backend.app.db.session as session_mod

session_mod._engine = None
session_mod._SessionLocal = None

from backend.app.db.session import init_db
from backend.app.services.seed_demo import ensure_demo_seed

init_db()
ensure_demo_seed()
