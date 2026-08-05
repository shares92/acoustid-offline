"""Bootstrap ueber MMO_-Umgebungsvariablen (ARCHITECTURE §6)."""

import logging
import re
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from shared.env import (
    ENV_PREFIX,
    LEGACY_ENV_PREFIX,
    EnvError,
    EnvSettings,
    env_var_name,
    legacy_env_var_name,
)

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
        "MMO_PORT": "9090",
        "MMO_DATA_DIR": "/mnt/cache/acoustid",
        "MMO_CONFIG_PATH": "/etc/acoustid/config.yaml",
        "MMO_DUMP_DIR": "/mnt/dumps",
        "MMO_DB_HOST": "db.example",
        "MMO_DB_PORT": "6432",
        "MMO_DB_NAME": "aoff",
        "MMO_DB_USER": "aoff_user",
        "MMO_DB_PASSWORD": "geheim",
        "MMO_DB_PASSWORD_FILE": "/run/secrets/db",
        "MMO_API_BASE_URL": "http://127.0.0.1:8081",
        "MMO_API_HEALTH_URL": "http://127.0.0.1:8081/_health",
        "MMO_API_PORT": "8081",
        "MMO_INDEX_URL": "http://index:6081",
        "MMO_DB_DATA_ROOT": "/mnt/array/db",
        "MMO_PG_MAJOR": "19",
        "MMO_LOG_LEVEL": "debug",
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
    settings = EnvSettings.from_env({"MMO_DATA_DIR": "/srv/aoff"})
    assert settings.config_path == Path("/srv/aoff/config.yaml")
    assert settings.db_password_file == Path("/srv/aoff/db-password")


def test_explicit_paths_win_over_the_data_dir() -> None:
    settings = EnvSettings.from_env(
        {"MMO_DATA_DIR": "/srv/aoff", "MMO_CONFIG_PATH": "/etc/config.yaml"}
    )
    assert settings.config_path == Path("/etc/config.yaml")


def test_the_dump_dir_does_not_follow_the_data_dir() -> None:
    """Seit M1b ein eigener Mount (v2 §3) — und das mit Absicht.

    ``data_dir`` liegt auf dem Cache, die Tagesdateien des Importers sind
    mehrere GB gross und gehoeren aufs Array. Eine Ableitung haette sie
    lautlos auf den Cache-Pool gelegt.
    """
    settings = EnvSettings.from_env({"MMO_DATA_DIR": "/srv/aoff"})
    assert settings.dump_dir == Path("/import")


def test_the_health_url_follows_the_api_base_url() -> None:
    """Wer die API umzieht, soll den Healthcheck nicht nachpflegen muessen.

    Sonst entstuende die halb umgezogene Umgebung: der Proxy spraeche mit
    dem neuen Dienst, die Bereitschaftsfrage noch mit dem alten.
    """
    settings = EnvSettings.from_env({"MMO_API_BASE_URL": "http://127.0.0.1:8081"})
    assert settings.api_health_url == "http://127.0.0.1:8081/_health"


def test_a_trailing_slash_in_the_base_url_does_not_double_up() -> None:
    settings = EnvSettings.from_env({"MMO_API_BASE_URL": "http://api:8081/"})
    assert settings.api_health_url == "http://api:8081/_health"


def test_an_explicit_health_url_wins_over_the_base_url() -> None:
    settings = EnvSettings.from_env(
        {"MMO_API_BASE_URL": "http://api:8081", "MMO_API_HEALTH_URL": "http://api:9/gesund"}
    )
    assert settings.api_health_url == "http://api:9/gesund"


def test_an_empty_health_url_still_follows_the_base_url() -> None:
    """Genau so reicht Compose sie durch (`${MMO_API_HEALTH_URL:-}`).

    Der Container bekommt die Variable **gesetzt, aber leer** — nur dann
    greift die Ableitung. Stuende in der Compose-Datei derselbe Default wie
    im Schema, liefe ein Betreiber, der nur die Basis-URL umzieht, lautlos
    mit dem alten Healthcheck weiter.
    """
    settings = EnvSettings.from_env(
        {"MMO_API_BASE_URL": "http://127.0.0.1:8081", "MMO_API_HEALTH_URL": ""}
    )
    assert settings.api_health_url == "http://127.0.0.1:8081/_health"


def test_empty_values_count_as_unset() -> None:
    settings = EnvSettings.from_env({"MMO_PORT": "", "MMO_DB_PASSWORD": "   "})
    assert settings.port == 8080
    assert settings.db_password.get_secret_value() == ""


def test_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_PORT", "1234")
    assert EnvSettings.from_env().port == 1234


def test_foreign_variables_are_ignored() -> None:
    settings = EnvSettings.from_env({"PATH": "/usr/bin", "PORT": "1"})
    assert settings.port == 8080


def test_unknown_aoff_variable_warns_and_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="shared.env"):
        settings = EnvSettings.from_env({"MMO_PROT": "9090"})
    assert settings.port == 8080
    assert [record.env_var for record in caplog.records] == ["MMO_PROT"]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MMO_PORT", "0"),
        ("MMO_PORT", "70000"),
        ("MMO_PORT", "acht"),
        ("MMO_DB_PORT", "-1"),
        ("MMO_API_PORT", "0"),
        ("MMO_API_PORT", "70000"),
        ("MMO_LOG_LEVEL", "LAUT"),
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
        {"MMO_DB_PASSWORD": "geheim", "MMO_DB_HOST": "db", "MMO_DB_NAME": "aoff"}
    )
    assert settings.db_dsn().get_secret_value() == "postgresql://acoustid:geheim@db:5432/aoff"


def test_db_dsn_escapes_special_characters() -> None:
    settings = EnvSettings.from_env({"MMO_DB_PASSWORD": "pa:ss/wo@rd"})
    assert "pa%3Ass%2Fwo%40rd" in settings.db_dsn().get_secret_value()


def test_db_dsn_without_password_is_a_clear_error() -> None:
    with pytest.raises(EnvError, match="MMO_DB_PASSWORD"):
        EnvSettings.from_env({}).db_dsn()


def test_db_password_and_dsn_are_masked() -> None:
    settings = EnvSettings.from_env({"MMO_DB_PASSWORD": "geheim"})
    assert "geheim" not in f"{settings!r} {settings!s}"
    assert "geheim" not in str(settings.db_dsn())
    assert isinstance(settings.db_password, SecretStr)


# --- Doku-Abgleich ---------------------------------------------------------


def test_env_var_name_maps_field_to_variable() -> None:
    assert env_var_name("db_password") == "MMO_DB_PASSWORD"
    assert env_var_name("port") == f"{ENV_PREFIX}PORT"


def test_env_example_documents_exactly_the_known_variables() -> None:
    """`.env.example` und das Schema werden zusammen gepflegt."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^(MMO_[A-Z0-9_]+)=", text, flags=re.MULTILINE))
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
    settings = EnvSettings.from_env({"MMO_DB_PASSWORD_FILE": str(secret)})

    assert "aus-der-datei" in settings.db_dsn().get_secret_value()


def test_the_variable_wins_over_the_file(tmp_path: Path) -> None:
    """Ein gesetzter Wert gewinnt — so wird ein v1-Bestand uebernommen."""
    secret = tmp_path / "db-password"
    secret.write_text("aus-der-datei", encoding="utf-8")
    settings = EnvSettings.from_env(
        {"MMO_DB_PASSWORD": "aus-der-umgebung", "MMO_DB_PASSWORD_FILE": str(secret)}
    )

    assert "aus-der-umgebung" in settings.db_dsn().get_secret_value()


def test_a_missing_password_file_names_both_ways(tmp_path: Path) -> None:
    """Die Fehlermeldung nennt Variable **und** Datei — sonst sucht man falsch."""
    settings = EnvSettings.from_env({"MMO_DB_PASSWORD_FILE": str(tmp_path / "gibtsnicht")})

    with pytest.raises(EnvError, match="gibtsnicht"):
        settings.db_dsn()


def test_the_password_file_never_appears_in_a_repr(tmp_path: Path) -> None:
    """Auch der Weg ueber die Datei fuehrt nicht zu einem Klartext im Log."""
    secret = tmp_path / "db-password"
    secret.write_text("streng-geheim", encoding="utf-8")
    settings = EnvSettings.from_env({"MMO_DB_PASSWORD_FILE": str(secret)})

    assert "streng-geheim" not in f"{settings!r} {settings!s}"
    assert "streng-geheim" not in str(settings.db_dsn())


# --- Uebergangslesen AOFF_ -> MMO_ (M2, eine Release-Runde; E5) -------------
#
# Der Kern des Risikos R7 in Umgebungsform: ein blosser Praefix-Wechsel haette
# alte Variablen **ungesehen** ignoriert. Eine gesetzte `AOFF_DATA_DIR` waere
# lautlos auf `/config` zurueckgefallen — der Waechter haette eine leere
# SQLite angelegt und die Instanz saehe frisch aufgesetzt aus, obwohl der
# Bestand danebenliegt. Diese Tests halten fest, dass das nicht passiert.


def test_the_old_prefix_is_still_read() -> None:
    settings = EnvSettings.from_env({"AOFF_PORT": "9090", "AOFF_DATA_DIR": "/mnt/cache/aoff"})

    assert settings.port == 9090
    assert settings.data_dir == Path("/mnt/cache/aoff")
    # Abgeleitete Werte folgen dem uebernommenen Wert, nicht dem Vorgabewert.
    assert settings.config_path == Path("/mnt/cache/aoff/config.yaml")


def test_reading_an_old_variable_warns_and_names_the_new_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="shared.env"):
        settings = EnvSettings.from_env({"AOFF_PORT": "9090"})

    assert settings.port == 9090
    assert [(record.env_var, record.env_var_replacement) for record in caplog.records] == [
        ("AOFF_PORT", "MMO_PORT")
    ]


def test_the_new_prefix_wins_over_the_old_one(caplog: pytest.LogCaptureFixture) -> None:
    """Sonst haenge das Ergebnis an der Reihenfolge der Umgebung."""
    with caplog.at_level(logging.WARNING, logger="shared.env"):
        settings = EnvSettings.from_env({"AOFF_PORT": "9090", "MMO_PORT": "7070"})

    assert settings.port == 7070
    assert [record.env_var for record in caplog.records] == ["AOFF_PORT"]


def test_both_prefixes_mix_per_variable() -> None:
    """Eine halb umgestellte `.env` ist der Normalfall des Uebergangs."""
    settings = EnvSettings.from_env({"MMO_PORT": "7070", "AOFF_DB_NAME": "alt"})

    assert settings.port == 7070
    assert settings.db_name == "alt"


def test_an_empty_new_variable_does_not_shadow_the_old_one() -> None:
    """Compose reicht Werte als `${MMO_API_PORT:-}` durch — gesetzt, aber leer.

    Wuerde der Leerstring als „gesetzt" zaehlen, verloere ausgerechnet die
    mitgelieferte Compose-Datei jeden Altwert.
    """
    settings = EnvSettings.from_env({"MMO_API_PORT": "", "AOFF_API_PORT": "8099"})

    assert settings.api_port == 8099


def test_an_unknown_old_variable_warns_and_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="shared.env"):
        settings = EnvSettings.from_env({"AOFF_PROT": "9090"})

    assert settings.port == 8080
    assert [record.env_var for record in caplog.records] == ["AOFF_PROT"]


def test_an_invalid_old_value_is_blamed_on_the_old_name() -> None:
    """Wer `AOFF_PORT=0` gesetzt hat, soll nicht nach `MMO_PORT` suchen."""
    with pytest.raises(EnvError) as excinfo:
        EnvSettings.from_env({"AOFF_PORT": "0"})

    assert "AOFF_PORT" in str(excinfo.value)
    assert "MMO_PORT" not in str(excinfo.value)


def test_legacy_env_var_name_maps_field_to_the_old_variable() -> None:
    assert legacy_env_var_name("db_password") == "AOFF_DB_PASSWORD"
    assert legacy_env_var_name("port") == f"{LEGACY_ENV_PREFIX}PORT"


def test_the_env_example_documents_only_the_new_prefix() -> None:
    """Die Vorlage zeigt den Zielzustand — der alte Weg ist nur Uebergang."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert not re.search(r"^AOFF_[A-Z0-9_]+=", text, flags=re.MULTILINE)
