from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
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
    wechat_app_secret: SecretStr | None = None
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
    source_max_chars: int = Field(default=50_000, ge=10_000, le=50_000)
    source_fetch_max_bytes: int = Field(default=2_000_000, ge=100_000, le=10_000_000)
    source_fetch_timeout_seconds: float = Field(default=12.0, ge=3, le=60)
    source_fetch_max_redirects: int = Field(default=5, ge=0, le=10)
    agent_max_tool_calls: int = Field(default=12, ge=1, le=100)
    agent_max_web_searches: int = Field(default=2, ge=0, le=20)
    agent_max_revisions: int = Field(default=2, ge=0, le=20)
    agent_max_seconds: int = Field(default=120, ge=10, le=3600)
    auto_create_schema: bool = False
    inline_worker: bool = True
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    reminder_delivery_enabled: bool = False
    wechat_subscribe_template_id: str | None = None
    wechat_subscribe_page: str = "pages/review/review"
    wechat_template_field_map_json: dict[str, str] = Field(
        default_factory=lambda: {
            "plan_name": "thing2",
            "reminder_time": "time1",
            "summary": "thing3",
        },
        validation_alias=AliasChoices(
            "wechat_template_field_map_json",
            "WECHAT_TEMPLATE_FIELD_MAP_JSON",
        ),
    )
    wechat_miniprogram_state: str = Field(
        default="developer",
        validation_alias=AliasChoices(
            "wechat_miniprogram_state",
            "APP_WECHAT_MINIPROGRAM_STATE",
            "WECHAT_MINIPROGRAM_STATE",
        ),
    )
    wechat_subscribe_lang: str = Field(
        default="zh_CN",
        validation_alias=AliasChoices(
            "wechat_subscribe_lang",
            "APP_WECHAT_SUBSCRIBE_LANG",
            "WECHAT_SUBSCRIBE_LANG",
        ),
    )
    reminder_batch_size: int = Field(default=50, ge=1, le=200)
    reminder_lease_seconds: int = Field(default=120, ge=30, le=600)
    reminder_worker_interval_seconds: int = Field(default=60, ge=5, le=600)
    wechat_send_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
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

    @field_validator("wechat_template_field_map_json", mode="before")
    @classmethod
    def parse_wechat_template_field_map(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("WECHAT_TEMPLATE_FIELD_MAP_JSON must be valid JSON") from exc

    @model_validator(mode="after")
    def validate_reminder_settings(self) -> "Settings":
        if not self.wechat_template_field_map_json:
            raise ValueError("WECHAT_TEMPLATE_FIELD_MAP_JSON must contain at least one field")
        targets: set[str] = set()
        for logical_field, template_field in self.wechat_template_field_map_json.items():
            if not logical_field.strip() or not template_field.strip():
                raise ValueError("template field mapping keys and values must be non-empty")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*\d+", template_field):
                raise ValueError(
                    f"invalid WeChat template field name for {logical_field}"
                )
            if template_field in targets:
                raise ValueError(f"duplicate WeChat template field mapping: {template_field}")
            targets.add(template_field)
        if self.wechat_miniprogram_state not in {"developer", "trial", "formal"}:
            raise ValueError("WeChat miniprogram state must be developer, trial, or formal")
        if self.wechat_subscribe_lang not in {"zh_CN", "zh_TW", "en"}:
            raise ValueError("WeChat subscribe language must be zh_CN, zh_TW, or en")
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
    def wechat_app_secret_value(self) -> str | None:
        return (
            self.wechat_app_secret.get_secret_value()
            if self.wechat_app_secret
            else None
        )

    @property
    def wechat_template_field_map(self) -> dict[str, str]:
        return dict(self.wechat_template_field_map_json)

    @property
    def reminder_delivery_ready(self) -> bool:
        return bool(
            self.reminder_delivery_enabled
            and self.wechat_subscribe_template_id
            and self.wechat_app_id
            and self.wechat_app_secret_value
        )

@lru_cache
def get_settings() -> Settings:
    return Settings()
