"""Test di configurazione: caricamento settings da variabili d'ambiente."""

from app.config import Settings


def test_settings_defaults() -> None:
    """I default sono sensati anche senza env."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.kimi_base_url == "https://api.moonshot.ai/v1"
    assert s.kimi_model == "kimi-k3"
    assert s.kimi_reasoning_effort == "high"
    assert s.moonshot_api_key == ""
    assert not s.llm_configured
    assert s.database_url.startswith("sqlite+aiosqlite:///")
    assert s.jwt_secret  # generato casualmente in dev


def test_settings_from_env(monkeypatch) -> None:
    """Le variabili d'ambiente sovrascrivono i default."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test-123")
    monkeypatch.setenv("KIMI_MODEL", "kimi-k3-turbo")
    monkeypatch.setenv("JWT_SECRET", "segreto-fisso-test")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("MAX_AGENT_ITERATIONS", "5")
    monkeypatch.setenv("CORS_ORIGINS", "http://a.local, http://b.local")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.moonshot_api_key == "sk-test-123"
    assert s.llm_configured
    assert s.kimi_model == "kimi-k3-turbo"
    assert s.jwt_secret == "segreto-fisso-test"
    assert s.jwt_expire_minutes == 60
    assert s.max_agent_iterations == 5
    assert s.cors_origins == ["http://a.local", "http://b.local"]


def test_jwt_secret_persistito_e_condiviso(tmp_path, monkeypatch) -> None:
    """Senza JWT_SECRET il segreto è generato UNA VOLTA e persistito in
    data_dir/.jwt_secret (permessi 600): due istanze consecutive lo condividono."""
    import os

    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s1 = Settings(_env_file=None)  # type: ignore[call-arg]
    s2 = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s1.jwt_secret
    assert s1.jwt_secret == s2.jwt_secret

    secret_file = tmp_path / "data" / ".jwt_secret"
    assert secret_file.is_file()
    assert secret_file.read_text(encoding="utf-8").strip() == s1.jwt_secret
    assert os.stat(secret_file).st_mode & 0o777 == 0o600


def test_jwt_secret_da_env_ha_precedenza_e_non_tocca_il_file(
    tmp_path, monkeypatch
) -> None:
    """JWT_SECRET da env vince e non crea né modifica data_dir/.jwt_secret."""
    monkeypatch.setenv("JWT_SECRET", "segreto-da-env")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.jwt_secret == "segreto-da-env"
    assert not (tmp_path / "data" / ".jwt_secret").exists()


def test_database_url_inside_data_dir(tmp_path, monkeypatch) -> None:
    """Il database SQLite vive dentro data_dir."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "dati"))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert str(tmp_path / "dati" / "virtualis.db") in s.database_url
