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
