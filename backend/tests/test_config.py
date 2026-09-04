import pytest

from memory_agent.config import Settings


def test_cors_origins_are_trimmed_and_normalized() -> None:
    settings = Settings(cors_origins=" https://example.github.io/, http://localhost:5173 ,, ")

    assert settings.cors_origin_list == [
        "https://example.github.io",
        "http://localhost:5173",
    ]


def test_neon_postgresql_url_uses_installed_psycopg3_driver() -> None:
    settings = Settings(
        database_url="postgresql://demo:secret@example.neon.tech/memory?sslmode=require"
    )

    assert settings.database_url == (
        "postgresql+psycopg://demo:secret@example.neon.tech/memory?sslmode=require"
    )


def test_explicit_psycopg3_url_is_unchanged() -> None:
    database_url = "postgresql+psycopg://demo:secret@example.neon.tech/memory?sslmode=require"

    assert Settings(database_url=database_url).database_url == database_url


def test_model_base_url_is_normalized() -> None:
    settings = Settings(model_base_url=" https://proxy.example.test/v1/ ")

    assert settings.model_base_url == "https://proxy.example.test/v1"


def test_model_base_url_requires_v1_path() -> None:
    try:
        Settings(model_base_url="https://proxy.example.test")
    except ValueError as exc:
        assert "must end with /v1" in str(exc)
    else:
        raise AssertionError("missing /v1 must fail configuration validation")


def test_model_base_url_rejects_pasted_punctuation() -> None:
    try:
        Settings(model_base_url="https://proxy.example.test/v1，")
    except ValueError as exc:
        assert "contains punctuation" in str(exc)
    else:
        raise AssertionError("pasted punctuation must fail configuration validation")


def test_wechat_auth_requires_expected_app_id() -> None:
    try:
        Settings(auth_mode="wechat")
    except ValueError as exc:
        assert "APP_WECHAT_APP_ID is required" in str(exc)
    else:
        raise AssertionError("wechat auth without an expected AppID must fail")


def test_wechat_template_map_supports_unprefixed_server_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "WECHAT_TEMPLATE_FIELD_MAP_JSON",
        '{"due_count":"number2","reminder_time":"time3","summary":"thing1"}',
    )

    settings = Settings()

    assert settings.wechat_template_field_map == {
        "due_count": "number2",
        "reminder_time": "time3",
        "summary": "thing1",
    }


def test_wechat_template_map_uses_only_canonical_unprefixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "WECHAT_TEMPLATE_FIELD_MAP_JSON",
        '{"plan_name":"thing2","reminder_time":"time1","summary":"thing3"}',
    )
    monkeypatch.setenv("APP_WECHAT_TEMPLATE_FIELD_MAP_JSON", '{"summary":"thing9"}')

    settings = Settings()

    assert settings.wechat_template_field_map == {
        "plan_name": "thing2",
        "reminder_time": "time1",
        "summary": "thing3",
    }


def test_reminder_delivery_ready_requires_all_credentials() -> None:
    partial = Settings(
        reminder_delivery_enabled=True,
        wechat_app_id="wx1234567890",
        wechat_subscribe_template_id="template-id",
    )
    complete = Settings(
        reminder_delivery_enabled=True,
        wechat_app_id="wx1234567890",
        wechat_app_secret="app-secret",
        wechat_subscribe_template_id="template-id",
    )

    assert partial.reminder_delivery_ready is False
    assert complete.reminder_delivery_ready is True


def test_wechat_legacy_claim_modes_are_mutually_exclusive() -> None:
    try:
        Settings(
            auth_mode="wechat",
            wechat_app_id="wx1234567890abcdef",
            wechat_claim_local_user=True,
            wechat_legacy_owner_openid="owner_openid_123456",
        )
    except ValueError as exc:
        assert "use either APP_WECHAT_CLAIM_LOCAL_USER" in str(exc)
    else:
        raise AssertionError("ambiguous legacy claim configuration must fail")
