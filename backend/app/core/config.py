from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="local", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8091, alias="APP_PORT")
    app_debug: bool = Field(default=False, alias="APP_DEBUG")
    app_auth_disabled: bool = Field(default=True, alias="APP_AUTH_DISABLED")

    dataset_max_file_size: int = Field(default=8_388_608_000, alias="DATASET_MAX_FILE_SIZE")
    dataset_chunk_size: int = Field(default=5 * 1024 * 1024, alias="DATASET_CHUNK_SIZE")
    dataset_runtime_dir: Path = Field(default=Path(".runtime"), alias="DATASET_RUNTIME_DIR")

    database_url: str = Field(default="", alias="DATABASE_URL")
    ftp_host: str = Field(default="127.0.0.1", alias="FTP_HOST")
    ftp_port: int = Field(default=21, alias="FTP_PORT")
    ftp_user: str = Field(default="", alias="FTP_USER")
    ftp_password: str = Field(default="", alias="FTP_PASSWORD")
    ftp_root: str = Field(default="/dataset", alias="FTP_ROOT")

    model_config = SettingsConfigDict(
        env_file=(".env", ".secrets/local.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.dataset_runtime_dir.mkdir(parents=True, exist_ok=True)
    return settings

