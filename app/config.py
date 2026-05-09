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

    # OCR backend (vLLM with OpenAI-compatible /v1/chat/completions API)
    backend_url: str = "http://127.0.0.1:8000"
    backend_model: str = "qwen2.5-vl-7b"
    backend_request_timeout_s: int = 120

    admin_username: str = "admin"
    admin_password_hash: str = ""
    session_secret: str = "change-me-in-production-" + "x" * 32

    storage_dir: str = "/data/storage"

    allowed_origins: str = ""
    max_upload_mb: int = 100
    result_ttl_days: int = 7

    rate_limit_per_minute: int = 10

    sync_timeout_s: int = 300

    # Scaleway on-demand GPU (optional). If all four are set, the worker
    # will power on the GPU instance when a job arrives.
    scw_access_key: str = ""
    scw_secret_key: str = ""
    scw_gpu_server_id: str = ""
    scw_gpu_zone: str = "fr-par-2"

    # Optional fallback GPU: used when the primary returns out_of_stock.
    # If set, both ``scw_gpu_server_id_fallback`` and ``backend_url_fallback``
    # must be configured together. Zone defaults to the primary's zone.
    scw_gpu_server_id_fallback: str = ""
    scw_gpu_zone_fallback: str = ""
    backend_url_fallback: str = ""

    # Human-readable labels stored on each Job so the admin UI can show
    # which hardware served a given job. Defaults are the ``primary`` /
    # ``fallback`` role names; setting these to e.g. ``H100-1-80G`` /
    # ``L40S-1-48G`` makes the Job table self-explanatory.
    scw_gpu_instance_label: str = "primary"
    scw_gpu_instance_label_fallback: str = "fallback"

    @property
    def gpu_candidates(self) -> list[tuple[str, str, str, str, str]]:
        """Ordered list of ``(label, server_id, zone, backend_url, instance_label)``
        for GPU selection.

        ``label`` is the role (primary/fallback). ``instance_label`` is the
        human-readable hardware name persisted to ``Job.backend_instance``.
        Primary first, then fallback if configured. Entries without a
        server_id are skipped.
        """
        out: list[tuple[str, str, str, str, str]] = []
        if self.scw_gpu_server_id:
            out.append(
                (
                    "primary",
                    self.scw_gpu_server_id,
                    self.scw_gpu_zone,
                    self.backend_url,
                    self.scw_gpu_instance_label,
                )
            )
        if self.scw_gpu_server_id_fallback and self.backend_url_fallback:
            zone = self.scw_gpu_zone_fallback or self.scw_gpu_zone
            out.append(
                (
                    "fallback",
                    self.scw_gpu_server_id_fallback,
                    zone,
                    self.backend_url_fallback,
                    self.scw_gpu_instance_label_fallback,
                )
            )
        return out

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def allowed_origins_list(self) -> list[str]:
        if not self.allowed_origins.strip():
            return []
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
