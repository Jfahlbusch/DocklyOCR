from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "sqlite:////data/ocr.db"

    redis_url: str = "redis://redis:6379/0"

    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "glm-ocr"
    ollama_request_timeout_s: int = 120

    admin_username: str = "admin"
    admin_password_hash: str = ""
    session_secret: str = "change-me-in-production-" + "x" * 32

    storage_dir: str = "/data/storage"

    allowed_origins: str = ""
    max_upload_mb: int = 100
    result_ttl_days: int = 30

    rate_limit_per_minute: int = 10

    sync_timeout_s: int = 300

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def allowed_origins_list(self) -> list[str]:
        if not self.allowed_origins.strip():
            return []
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
