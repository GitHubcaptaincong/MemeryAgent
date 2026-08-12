from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
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
    local_user_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
    local_user_email: str = "local@memory-agent.test"
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
    web_access_default: bool = False
    agent_max_tool_calls: int = Field(default=12, ge=1, le=100)
    agent_max_web_searches: int = Field(default=2, ge=0, le=20)
    agent_max_revisions: int = Field(default=2, ge=0, le=20)
    agent_max_seconds: int = Field(default=120, ge=10, le=3600)
    auto_create_schema: bool = False
    inline_worker: bool = True
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
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

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def model_api_key_value(self) -> str | None:
        return self.model_api_key.get_secret_value() if self.model_api_key else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
