from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=(WORKSPACE_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    database_url: str = "sqlite+pysqlite:///./memory_agent.db"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    auth_mode: Literal["local", "wechat"] = "local"
    local_user_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
    local_user_email: str = "local@memory-agent.test"
    wechat_app_id: str | None = None
    wechat_legacy_owner_openid: SecretStr | None = None
    wechat_claim_local_user: bool = False
    model_provider: str = "fake"
    model_base_url: str = "http://127.0.0.1:8317/v1"
    model_api_key: SecretStr | None = None
    model_name: str = "gpt-5.4-mini"
    model_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "low"
    model_timeout_seconds: float = Field(default=180, ge=5, le=600)
    model_max_output_tokens: int = Field(default=8_000, ge=1_000, le=100_000)
    model_quick_source_max_chars: int = Field(default=600, ge=0, le=5_000)
    model_quick_reasoning_effort: Literal["none", "low", "medium"] = "none"
    model_quick_max_output_tokens: int = Field(default=2_500, ge=1_000, le=20_000)
    model_verify_ssl: bool = True
    answer_evaluation_timeout_seconds: float = Field(default=15, ge=3, le=60)
    answer_evaluation_max_output_tokens: int = Field(default=1_000, ge=300, le=4_000)
    answer_evaluation_reasoning_effort: Literal["none", "low", "medium"] = "none"
    web_access_default: bool = False
    agent_max_tool_calls: int = Field(default=12, ge=1, le=100)
    agent_max_web_searches: int = Field(default=2, ge=0, le=20)
    agent_max_revisions: int = Field(default=2, ge=0, le=20)
    agent_max_seconds: int = Field(default=120, ge=10, le=3600)
    auto_create_schema: bool = False
    inline_worker: bool = True
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    reminder_delivery_enabled: bool = False
    reminder_dispatch_token: SecretStr | None = None
    wechat_subscribe_template_id: str | None = None
    wechat_subscribe_page: str = "pages/review/review"
    reminder_batch_size: int = Field(default=50, ge=1, le=200)
    reminder_lease_seconds: int = Field(default=120, ge=30, le=600)
    skill_root: Path = WORKSPACE_ROOT / ".agents" / "skills"

    @field_validator("database_url", mode="before")
    @classmethod
    def use_psycopg3_for_postgresql(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        if value.startswith("postgresql+psycopg2://"):
            return value.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
        return value

    @field_validator("model_base_url", mode="before")
    @classmethod
    def validate_model_base_url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().rstrip("/")
        if any(character in normalized for character in (",", "，", "。", "\n", "\r")):
            raise ValueError("model base URL contains punctuation or a line break")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("model base URL must be an absolute HTTP(S) URL")
        if parsed.path.rstrip("/") != "/v1":
            raise ValueError("CLIProxy model base URL must end with /v1")
        return normalized

    @field_validator("wechat_app_id", mode="before")
    @classmethod
    def normalize_wechat_app_id(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_wechat_auth_settings(self) -> "Settings":
        if self.auth_mode == "wechat" and not self.wechat_app_id:
            raise ValueError("APP_WECHAT_APP_ID is required when APP_AUTH_MODE=wechat")
        if self.wechat_claim_local_user and self.wechat_legacy_owner_openid:
            raise ValueError(
                "use either APP_WECHAT_CLAIM_LOCAL_USER or "
                "APP_WECHAT_LEGACY_OWNER_OPENID, not both"
            )
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def model_api_key_value(self) -> str | None:
        return self.model_api_key.get_secret_value() if self.model_api_key else None

    @property
    def wechat_legacy_owner_openid_value(self) -> str | None:
        return (
            self.wechat_legacy_owner_openid.get_secret_value()
            if self.wechat_legacy_owner_openid
            else None
        )

    @property
    def reminder_dispatch_token_value(self) -> str | None:
        return (
            self.reminder_dispatch_token.get_secret_value()
            if self.reminder_dispatch_token
            else None
        )

@lru_cache
def get_settings() -> Settings:
    return Settings()
