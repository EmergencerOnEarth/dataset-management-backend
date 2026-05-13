from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from backend.app.core.config import Settings, get_settings
from backend.app.db.session import get_db as _get_db
from backend.app.storage.backend import StorageBackend, get_storage


def get_db() -> Generator[Session, None, None]:
    yield from _get_db()


def storage_dep(settings: Annotated[Settings, Depends(get_settings)]) -> StorageBackend:
    return get_storage(settings)


DbSession = Annotated[Session, Depends(get_db)]
StorageDep = Annotated[StorageBackend, Depends(storage_dep)]
