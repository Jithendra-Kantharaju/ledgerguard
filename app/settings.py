"""Runtime settings, read from environment variables (or a .env file)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres. The app connects as a non-owner role so row-level security applies.
    database_url: str = "postgresql://ap_app:ap_app@localhost:5432/apagent"
    # Owner/admin connection, used only for migrations and seeding.
    admin_database_url: str = "postgresql://postgres:postgres@localhost:5432/apagent"

    # Storage: "local" (filesystem) or "s3" (MinIO / AWS S3).
    storage_backend: str = "local"
    storage_dir: Path = ROOT / ".storage"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "ap-documents"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # Extraction: "rules" (offline, no API key), "anthropic" or "openai".
    extractor: str = "rules"
    secondary_extractor: str = "rules_alt"
    anthropic_model: str = "claude-haiku-5-5"
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # Parsing.
    use_docling: bool = False
    ocr_backend: str = "none"  # "none" or "tesseract"

    # Mock AP system the outbox dispatcher talks to.
    ap_api_url: str = "http://localhost:8001"
    ap_timeout_seconds: float = 10.0
    ap_api_token: str = "dev-ap-token"


    # AP provider.
    ap_provider: str = "mock"

    # QuickBooks sandbox.
    qbo_company_id: str | None = None
    qbo_access_token: str | None = None
    qbo_tenant_slug: str = "acme"
    qbo_vendor_id: str | None = None
    qbo_vendor_name: str | None = None
    qbo_expense_account_id: str | None = None


    # Shadow mode: decide everything, send nothing to AP. Use for the first weeks at a customer,
    # comparing the agent's decisions with what their AP team actually did.
    shadow_mode: bool = False

    # Worker.
    worker_id: str = "worker-1"
    job_lease_seconds: int = 300
    poll_interval_seconds: float = 1.0
    outbox_max_attempts: int = 5

    # Tracing (OpenTelemetry). Point at Langfuse's OTLP endpoint to export traces.
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_headers: str | None = None
    trace_console: bool = False

    # API: browser origins allowed to call the API (the review UI).
    cors_origins: str = "http://localhost:5173"

    # Customer config (tolerances, limits) and calibration model.
    customer_config: Path = ROOT / "app" / "config" / "customer_a.yaml"
    calibration_file: Path = ROOT / "eval" / "results" / "calibration.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
