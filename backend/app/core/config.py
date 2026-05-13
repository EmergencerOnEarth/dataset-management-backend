from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local default only; shared/test environments must override MYSQL_* / FTP_* via secrets.
_DEFAULT_PEER_HOST = "127.0.0.1"


class Settings(BaseSettings):
    app_env: str = Field(default="local", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8091, alias="APP_PORT")
    app_debug: bool = Field(default=False, alias="APP_DEBUG")
    app_auth_disabled: bool = Field(default=True, alias="APP_AUTH_DISABLED")
    auth_allowed_tokens: str = Field(default="", alias="AUTH_ALLOWED_TOKENS")

    dataset_max_file_size: int = Field(default=8_388_608_000, alias="DATASET_MAX_FILE_SIZE")
    dataset_chunk_size: int = Field(default=5 * 1024 * 1024, alias="DATASET_CHUNK_SIZE")
    dataset_runtime_dir: Path = Field(default=Path(".runtime"), alias="DATASET_RUNTIME_DIR")
    # ``local`` mirrors paths under ``ftp_mirror``; ``ftp`` uses ftplib against ``FTP_*``.
    storage_backend: str = Field(default="local", alias="STORAGE_BACKEND")

    # ``DATABASE_URL`` 非空时优先；否则用 ``MYSQL_*`` 拼装（默认连 ``_DEFAULT_PEER_HOST``）。
    database_url: Optional[str] = Field(default=None, alias="DATABASE_URL")
    mysql_host: str = Field(default=_DEFAULT_PEER_HOST, alias="MYSQL_HOST")
    mysql_port: int = Field(default=3306, alias="MYSQL_PORT")
    mysql_user: str = Field(default="root", alias="MYSQL_USER")
    mysql_password: str = Field(default="", alias="MYSQL_PASSWORD")
    mysql_database: str = Field(default="eye_research_dataset", alias="MYSQL_DATABASE")
    mysql_compose_only: bool = Field(
        default=False,
        alias="MYSQL_COMPOSE_ONLY",
        description="true 时强制用 MYSQL_* 拼装 DSN，忽略 DATABASE_URL（含 .env 内配置）。",
    )

    ftp_host: str = Field(default=_DEFAULT_PEER_HOST, alias="FTP_HOST")
    ftp_port: int = Field(default=21, alias="FTP_PORT")
    ftp_user: str = Field(default="", alias="FTP_USER")
    ftp_password: str = Field(default="", alias="FTP_PASSWORD")
    ftp_root: str = Field(default="/dataset", alias="FTP_ROOT")

    model_config = SettingsConfigDict(
        env_file=(".env", ".secrets/local.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _database_url_from_mysql(self):  # noqa: N802 pydantic naming
        auth = f"{quote_plus(self.mysql_user)}:{quote_plus(self.mysql_password)}@"
        composed = (
            f"mysql+pymysql://{auth}{self.mysql_host}:{self.mysql_port}/"
            f"{self.mysql_database}?charset=utf8mb4"
        )
        if self.mysql_compose_only:
            object.__setattr__(self, "database_url", composed)
            return self
        raw = self.database_url
        if isinstance(raw, str) and raw.strip():
            object.__setattr__(self, "database_url", raw.strip())
            return self
        object.__setattr__(self, "database_url", composed)
        return self


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.dataset_runtime_dir.mkdir(parents=True, exist_ok=True)
    return settings
