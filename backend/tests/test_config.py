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
