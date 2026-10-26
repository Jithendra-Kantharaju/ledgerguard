
"""LedgerGuard application configuration.

Settings are read from environment variables or
the local .env file.

AWS ECS uses task environment variables and
AWS Secrets Manager.

Do not commit actual API keys or credentials.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --------------------------------------------------
    # Environment
    # --------------------------------------------------

    environment: str = "local"

    aws_region: str = "us-east-2"

    # --------------------------------------------------
    # Authentication
    # --------------------------------------------------

    # Local: api_key
    # AWS: cognito
    auth_mode: Literal[
        "api_key",
        "cognito",
    ] = "api_key"

    cognito_user_pool_id: str | None = None
    cognito_client_id: str | None = None

    # --------------------------------------------------
    # PostgreSQL
    # --------------------------------------------------

    # Non-owner database role used by the API and worker.
    # Row-level security is enforced for tenant data.
    database_url: str = (
        "postgresql://ap_app:ap_app"
        "@localhost:5432/apagent"
    )

    # Administrator connection.
    # Only migration and provisioning tasks should use it.
    admin_database_url: str = (
        "postgresql://postgres:postgres"
        "@localhost:5432/apagent"
    )

    # --------------------------------------------------
    # Document storage
    # --------------------------------------------------

    # local: shared development filesystem
    # s3: AWS S3 or S3-compatible storage
    storage_backend: Literal[
        "local",
        "s3",
    ] = "local"

    storage_dir: Path = ROOT / ".storage"

    s3_bucket: str = "ap-documents"

    # For AWS S3, leave these values unset.
    # boto3 uses the ECS task IAM role.
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # --------------------------------------------------
    # AI extraction
    # --------------------------------------------------

    # Options: rules, openai, anthropic
    extractor: str = "rules"

    secondary_extractor: str = "rules_alt"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-5-5"

    # --------------------------------------------------
    # Document parsing and OCR
    # --------------------------------------------------

    use_docling: bool = False

    ocr_backend: Literal[
        "none",
        "tesseract",
    ] = "none"

    # --------------------------------------------------
    # Accounts payable provider
    # --------------------------------------------------

    # mock: local testing
    # quickbooks: external sandbox integration
    ap_provider: Literal[
        "mock",
        "quickbooks",
    ] = "mock"

    ap_api_url: str = "http://localhost:8001"

    ap_api_token: str = "dev-ap-token"

    ap_timeout_seconds: float = 10.0

    # Never send external AP actions when enabled.
    shadow_mode: bool = False

    # --------------------------------------------------
    # QuickBooks sandbox
    # --------------------------------------------------

    qbo_company_id: str | None = None

    qbo_access_token: str | None = None

    qbo_tenant_slug: str = "acme"

    qbo_vendor_id: str | None = None

    qbo_vendor_name: str | None = None

    qbo_expense_account_id: str | None = None

    # --------------------------------------------------
    # Worker and job queue
    # --------------------------------------------------

    worker_id: str = "worker-1"

    job_lease_seconds: int = 300

    poll_interval_seconds: float = 1.0

    outbox_max_attempts: int = 5

    # --------------------------------------------------
    # Observability
    # --------------------------------------------------

    # OpenTelemetry OTLP endpoint.
    otel_exporter_otlp_endpoint: str | None = None

    otel_exporter_otlp_headers: str | None = None

    trace_console: bool = False

    # ECS stdout/stderr logs are collected by CloudWatch
    # when awslogs is configured in the ECS task.
    # --------------------------------------------------
    # HTTP API
    # --------------------------------------------------

    cors_origins: str = "http://localhost:5173"

    # --------------------------------------------------
    # Customer policies and calibration
    # --------------------------------------------------

    customer_config: Path = (
        ROOT / "app" / "config" / "customer_a.yaml"
    )

    calibration_file: Path = (
        ROOT / "eval" / "results" / "calibration.json"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
