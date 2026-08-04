"""Bootstrap ueber AOFF_-Umgebungsvariablen (ARCHITECTURE §6)."""

import logging
import re
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from shared.env import ENV_PREFIX, EnvError, EnvSettings, env_var_name

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_defaults_without_any_environment() -> None:
    settings = EnvSettings.from_env({})
    assert settings.port == 8080
    assert settings.data_dir == Path("/data")
    assert settings.config_path == Path("/data/config.yaml")
    assert settings.dump_dir == Path("/data/dumps")
    assert settings.db_host == "acoustid-db"
    assert settings.db_port == 5432
    assert settings.db_name == "acoustid"
    assert settings.db_user == "acoustid"
    assert settings.db_password.get_secret_value() == ""
    assert settings.api_base_url == "http://acoustid-api:8080"
    assert settings.api_health_url == "http://acoustid-api:8080/_health"
    assert settings.api_port == 8080
    assert settings.index_url == "http://acoustid-index:6081"
    assert settings.log_level == "INFO"


def test_every_variable_can_be_overridden() -> None:
    environ = {
        "AOFF_PORT": "9090",
        "AOFF_DATA_DIR": "/mnt/cache/acoustid",
        "AOFF_CONFIG_PATH": "/etc/acoustid/config.yaml",
        "AOFF_DUMP_DIR": "/mnt/dumps",
        "AOFF_DB_HOST": "db.example",
        "AOFF_DB_PORT": "6432",
        "AOFF_DB_NAME": "aoff",
        "AOFF_DB_USER": "aoff_user",
        "AOFF_DB_PASSWORD": "geheim",
        "AOFF_API_BASE_URL": "http://127.0.0.1:8081",
        "AOFF_API_HEALTH_URL": "http://127.0.0.1:8081/_health",
        "AOFF_API_PORT": "8081",
        "AOFF_INDEX_URL": "http://index:6081",
        "AOFF_LOG_LEVEL": "debug",
    }
    settings = EnvSettings.from_env(environ)
    assert settings.port == 9090
    assert settings.data_dir == Path("/mnt/cache/acoustid")
    assert settings.config_path == Path("/etc/acoustid/config.yaml")
    assert settings.dump_dir == Path("/mnt/dumps")
    assert settings.db_host == "db.example"
    assert settings.db_port == 6432
    assert settings.db_name == "aoff"
    assert settings.db_user == "aoff_user"
    assert settings.db_password.get_secret_value() == "geheim"
    assert settings.api_base_url == "http://127.0.0.1:8081"
    assert settings.api_health_url == "http://127.0.0.1:8081/_health"
    assert settings.api_port == 8081
    assert settings.index_url == "http://index:6081"
    assert settings.log_level == "DEBUG"


def test_paths_follow_the_data_dir() -> None:
    settings = EnvSettings.from_env({"AOFF_DATA_DIR": "/srv/aoff"})
    assert settings.config_path == Path("/srv/aoff/config.yaml")
    assert settings.dump_dir == Path("/srv/aoff/dumps")


def test_explicit_paths_win_over_the_data_dir() -> None:
    settings = EnvSettings.from_env(
        {"AOFF_DATA_DIR": "/srv/aoff", "AOFF_CONFIG_PATH": "/etc/config.yaml"}
    )
    assert settings.config_path == Path("/etc/config.yaml")
    assert settings.dump_dir == Path("/srv/aoff/dumps")


def test_the_health_url_follows_the_api_base_url() -> None:
    """Wer die API umzieht, soll den Healthcheck nicht nachpflegen muessen.

    Sonst entstuende die halb umgezogene Umgebung: der Proxy spraeche mit
    dem neuen Dienst, die Bereitschaftsfrage noch mit dem alten.
    """
    settings = EnvSettings.from_env({"AOFF_API_BASE_URL": "http://127.0.0.1:8081"})
    assert settings.api_health_url == "http://127.0.0.1:8081/_health"


def test_a_trailing_slash_in_the_base_url_does_not_double_up() -> None:
    settings = EnvSettings.from_env({"AOFF_API_BASE_URL": "http://api:8081/"})
    assert settings.api_health_url == "http://api:8081/_health"


def test_an_explicit_health_url_wins_over_the_base_url() -> None:
    settings = EnvSettings.from_env(
        {"AOFF_API_BASE_URL": "http://api:8081", "AOFF_API_HEALTH_URL": "http://api:9/gesund"}
    )
    assert settings.api_health_url == "http://api:9/gesund"


def test_empty_values_count_as_unset() -> None:
    settings = EnvSettings.from_env({"AOFF_PORT": "", "AOFF_DB_PASSWORD": "   "})
    assert settings.port == 8080
    assert settings.db_password.get_secret_value() == ""


def test_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AOFF_PORT", "1234")
    assert EnvSettings.from_env().port == 1234


def test_foreign_variables_are_ignored() -> None:
    settings = EnvSettings.from_env({"PATH": "/usr/bin", "PORT": "1"})
    assert settings.port == 8080


def test_unknown_aoff_variable_warns_and_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="shared.env"):
        settings = EnvSettings.from_env({"AOFF_PROT": "9090"})
    assert settings.port == 8080
    assert [record.env_var for record in caplog.records] == ["AOFF_PROT"]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("AOFF_PORT", "0"),
        ("AOFF_PORT", "70000"),
        ("AOFF_PORT", "acht"),
        ("AOFF_DB_PORT", "-1"),
        ("AOFF_API_PORT", "0"),
        ("AOFF_API_PORT", "70000"),
        ("AOFF_LOG_LEVEL", "LAUT"),
    ],
)
def test_invalid_values_raise_env_error_naming_the_variable(variable: str, value: str) -> None:
    with pytest.raises(EnvError) as excinfo:
        EnvSettings.from_env({variable: value})
    assert variable in str(excinfo.value)


def test_settings_are_immutable() -> None:
    settings = EnvSettings.from_env({})
    with pytest.raises(ValidationError):
        settings.port = 1


def test_unknown_field_is_rejected_on_direct_construction() -> None:
    with pytest.raises(ValidationError):
        EnvSettings(prot=9090)  # type: ignore[call-arg]


# --- DSN -------------------------------------------------------------------


def test_db_dsn_is_built_from_the_parts() -> None:
    settings = EnvSettings.from_env(
        {"AOFF_DB_PASSWORD": "geheim", "AOFF_DB_HOST": "db", "AOFF_DB_NAME": "aoff"}
    )
    assert settings.db_dsn().get_secret_value() == "postgresql://acoustid:geheim@db:5432/aoff"


def test_db_dsn_escapes_special_characters() -> None:
    settings = EnvSettings.from_env({"AOFF_DB_PASSWORD": "pa:ss/wo@rd"})
    assert "pa%3Ass%2Fwo%40rd" in settings.db_dsn().get_secret_value()


def test_db_dsn_without_password_is_a_clear_error() -> None:
    with pytest.raises(EnvError, match="AOFF_DB_PASSWORD"):
        EnvSettings.from_env({}).db_dsn()


def test_db_password_and_dsn_are_masked() -> None:
    settings = EnvSettings.from_env({"AOFF_DB_PASSWORD": "geheim"})
    assert "geheim" not in f"{settings!r} {settings!s}"
    assert "geheim" not in str(settings.db_dsn())
    assert isinstance(settings.db_password, SecretStr)


# --- Doku-Abgleich ---------------------------------------------------------


def test_env_var_name_maps_field_to_variable() -> None:
    assert env_var_name("db_password") == "AOFF_DB_PASSWORD"
    assert env_var_name("port") == f"{ENV_PREFIX}PORT"


def test_env_example_documents_exactly_the_known_variables() -> None:
    """`.env.example` und das Schema werden zusammen gepflegt."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^(AOFF_[A-Z0-9_]+)=", text, flags=re.MULTILINE))
    known = {env_var_name(name) for name in EnvSettings.model_fields}
    assert documented == known
