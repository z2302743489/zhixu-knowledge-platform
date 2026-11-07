from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZHIXU_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/zhixu.db"
    data_dir: Path = Path("data")
    qdrant_url: str = ""
    redis_url: str = "redis://redis:6379/0"
    task_mode: str = "eager"
    secret_key: str = ""
    testing: bool = False
    allowed_private_hosts: str = ""
    cookie_secure: bool = False
    max_upload_mb: int = 50
    provider_timeout: float = 90.0
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"


settings = Settings()
