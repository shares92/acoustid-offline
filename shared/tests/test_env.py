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
    # Ein Container, ein Netzwerk-Namensraum: alle Adressen sind Loopback,
    # und die Pfade sind die Mounts aus HANDOFF v2 §3.
    assert settings.data_dir == Path("/config")
    assert settings.config_path == Path("/config/config.yaml")
    assert settings.dump_dir == Path("/import")
    assert settings.db_host == "127.0.0.1"
    assert settings.db_port == 5432
    assert settings.db_name == "acoustid"
    assert settings.db_user == "acoustid"
    assert settings.db_password.get_secret_value() == ""
    assert settings.db_password_file == Path("/config/db-password")
    assert settings.db_data_root == Path("/data/db")
    assert settings.pg_major == 18
    assert settings.api_base_url == "http://127.0.0.1:8081"
    assert settings.api_health_url == "http://127.0.0.1:8081/_health"
    assert settings.api_port == 8081
    assert settings.index_url == "http://127.0.0.1:6081"
    assert settings.log_level == "INFO"


def test_the_watchdog_data_dir_never_lands_on_the_array() -> None:
    """Risiko R1 der M0-Analyse, als Test.

    ``/data`` ist in v2 das **Array**. Laegen SQLite, Keys und Lookup-Cache
    dort, schriebe der Waechter im laufenden Betrieb auf die Spindeln — das
    Array schliefe nie, und kein anderer Test wuerde es merken (die
    Waechter-Suite laeuft auf ``tmp_path``). Deshalb steht die Zusage hier
    als eigener Satz.
    """
    settings = EnvSettings.from_env({})

    for path in (settings.data_dir, settings.config_path):
        assert not path.is_relative_to("/data"), path
    # Und die Gegenprobe: der Datenbestand gehoert sehr wohl dorthin.
    assert settings.db_data_root.is_relative_to("/data")


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
        "AOFF_DB_PASSWORD_FILE": "/run/secrets/db",
        "AOFF_API_BASE_URL": "http://127.0.0.1:8081",
        "AOFF_API_HEALTH_URL": "http://127.0.0.1:8081/_health",
        "AOFF_API_PORT": "8081",
        "AOFF_INDEX_URL": "http://index:6081",
        "AOFF_DB_DATA_ROOT": "/mnt/array/db",
        "AOFF_PG_MAJOR": "19",
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
    assert settings.db_password_file == Path("/run/secrets/db")
    assert settings.api_base_url == "http://127.0.0.1:8081"
    assert settings.api_health_url == "http://127.0.0.1:8081/_health"
    assert settings.api_port == 8081
    assert settings.index_url == "http://index:6081"
    assert settings.db_data_root == Path("/mnt/array/db")
    assert settings.pg_major == 19
    assert settings.log_level == "DEBUG"


def test_the_config_path_follows_the_data_dir() -> None:
    settings = EnvSettings.from_env({"AOFF_DATA_DIR": "/srv/aoff"})
    assert settings.config_path == Path("/srv/aoff/config.yaml")
    assert settings.db_password_file == Path("/srv/aoff/db-password")


def test_explicit_paths_win_over_the_data_dir() -> None:
    settings = EnvSettings.from_env(
        {"AOFF_DATA_DIR": "/srv/aoff", "AOFF_CONFIG_PATH": "/etc/config.yaml"}
    )
    assert settings.config_path == Path("/etc/config.yaml")


def test_the_dump_dir_does_not_follow_the_data_dir() -> None:
    """Seit M1b ein eigener Mount (v2 §3) — und das mit Absicht.

    ``data_dir`` liegt auf dem Cache, die Tagesdateien des Importers sind
    mehrere GB gross und gehoeren aufs Array. Eine Ableitung haette sie
    lautlos auf den Cache-Pool gelegt.
    """
    settings = EnvSettings.from_env({"AOFF_DATA_DIR": "/srv/aoff"})
    assert settings.dump_dir == Path("/import")


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


def test_an_empty_health_url_still_follows_the_base_url() -> None:
    """Genau so reicht Compose sie durch (`${AOFF_API_HEALTH_URL:-}`).

    Der Container bekommt die Variable **gesetzt, aber leer** — nur dann
    greift die Ableitung. Stuende in der Compose-Datei derselbe Default wie
    im Schema, liefe ein Betreiber, der nur die Basis-URL umzieht, lautlos
    mit dem alten Healthcheck weiter.
    """
    settings = EnvSettings.from_env(
        {"AOFF_API_BASE_URL": "http://127.0.0.1:8081", "AOFF_API_HEALTH_URL": ""}
    )
    assert settings.api_health_url == "http://127.0.0.1:8081/_health"


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


# --- Datenbank-Passwort aus einer Datei (M1b) ------------------------------


def test_the_password_file_is_read_when_the_variable_is_empty(tmp_path: Path) -> None:
    """Der Weg, den der Entrypoint benutzt (E16).

    Er erzeugt das Passwort beim ersten Start. Exportieren allein genuegt
    nicht: ein `docker compose exec` startet einen Prozess **neben** ihm und
    erbt seine Umgebung nicht — der Importer-Lauf haenge dann daran, wie man
    in den Container gekommen ist.
    """
    secret = tmp_path / "db-password"
    secret.write_text("aus-der-datei\n", encoding="utf-8")
    settings = EnvSettings.from_env({"AOFF_DB_PASSWORD_FILE": str(secret)})

    assert "aus-der-datei" in settings.db_dsn().get_secret_value()


def test_the_variable_wins_over_the_file(tmp_path: Path) -> None:
    """Ein gesetzter Wert gewinnt — so wird ein v1-Bestand uebernommen."""
    secret = tmp_path / "db-password"
    secret.write_text("aus-der-datei", encoding="utf-8")
    settings = EnvSettings.from_env(
        {"AOFF_DB_PASSWORD": "aus-der-umgebung", "AOFF_DB_PASSWORD_FILE": str(secret)}
    )

    assert "aus-der-umgebung" in settings.db_dsn().get_secret_value()


def test_a_missing_password_file_names_both_ways(tmp_path: Path) -> None:
    """Die Fehlermeldung nennt Variable **und** Datei — sonst sucht man falsch."""
    settings = EnvSettings.from_env({"AOFF_DB_PASSWORD_FILE": str(tmp_path / "gibtsnicht")})

    with pytest.raises(EnvError, match="gibtsnicht"):
        settings.db_dsn()


def test_the_password_file_never_appears_in_a_repr(tmp_path: Path) -> None:
    """Auch der Weg ueber die Datei fuehrt nicht zu einem Klartext im Log."""
    secret = tmp_path / "db-password"
    secret.write_text("streng-geheim", encoding="utf-8")
    settings = EnvSettings.from_env({"AOFF_DB_PASSWORD_FILE": str(secret)})

    assert "streng-geheim" not in f"{settings!r} {settings!s}"
    assert "streng-geheim" not in str(settings.db_dsn())
