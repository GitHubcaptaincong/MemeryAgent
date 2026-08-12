from memory_agent.config import Settings


def test_cors_origins_are_trimmed_and_normalized() -> None:
    settings = Settings(cors_origins=" https://example.github.io/, http://localhost:5173 ,, ")

    assert settings.cors_origin_list == [
        "https://example.github.io",
        "http://localhost:5173",
    ]
