"""Schema der config.yaml: Defaults und Validierungsregeln (ARCHITECTURE §6)."""

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from shared.config import Config, config_to_dict
from shared.models import AuthMode, SubmitMode

# Vollstaendiger Sollzustand aus HANDOFF v2 §7 inklusive der drei
# Projekt-Ergaenzungen, die §7 nicht auflistet (acoustid.index.query_hashes,
# auth.allow_known_client_keys, mb.keep_submitted_mbid — K7/E16).
# Bewusst als Literal ausgeschrieben: der Test soll die Doku nachbilden,
# nicht den Code.
EXPECTED_DEFAULTS: dict[str, Any] = {
    "auth": {
        "mode": "none",
        "allow_known_client_keys": False,
    },
    "acoustid": {
        "submit": {
            "mode": "local",
            "upstream_app_key": "",
        },
        "update": {"time": "04:00"},
        "index": {"query_hashes": 120},
    },
    "discogs": {
        "update": {"check_time": "05:00"},
        "token": "",
    },
    "caa": {"crawl": {"enabled": False, "rate_per_s": 2}},
    "covers": {"negative_retry_days": 30},
    "tadb": {"api_key": ""},
    "wake": {"hold_timeout_s": 90},
    "idle": {"timeout_min": 15},
    "disk": {"min_free_gb": 100},
    "cache": {
        "enabled": True,
        "max_size_mb": 512,
    },
    "ratelimit": {"per_ip_per_min": 120},
    "metrics": {"enabled": False},
    "notify": {
        "ntfy": {"url": ""},
        "smtp": {
            "host": "",
            "port": 587,
            "user": "",
            "pass": "",
            "from": "",
            "to": "",
        },
    },
    "backup": {
        "dir": "",
        "time": "04:45",
        "include_covers": False,
    },
    "mb": {"dsn": "", "keep_submitted_mbid": False},
}


def _nested(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """`("acoustid", "submit", "mode"), "off"` -> das passende Schachtel-dict.

    Seit M2 sind die Schluessel mehrstufig; die parametrisierten Tests
    beschreiben deshalb einen Pfad statt eines Sektion/Feld-Paares.
    """
    result: Any = value
    for part in reversed(path):
        result = {part: result}
    return result


def _at(config: Config, path: tuple[str, ...]) -> Any:
    value: Any = config
    for part in path:
        value = getattr(value, part)
    return value


def test_defaults_match_architecture_section_6() -> None:
    assert config_to_dict(Config()) == EXPECTED_DEFAULTS


def test_defaults_are_complete_without_input() -> None:
    """Eine leere Datei ergibt exakt dieselbe Config wie gar keine Angabe."""
    assert Config.model_validate({}) == Config()


def test_section_order_follows_the_documentation() -> None:
    assert list(config_to_dict(Config())) == list(EXPECTED_DEFAULTS)


# --- Enums -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["none", "apikey"])
def test_auth_mode_accepts_documented_values(value: str) -> None:
    assert Config.model_validate({"auth": {"mode": value}}).auth.mode == AuthMode(value)


@pytest.mark.parametrize("value", ["off", "local"])
def test_submit_mode_accepts_documented_values(value: str) -> None:
    config = Config.model_validate({"acoustid": {"submit": {"mode": value}}})
    assert config.acoustid.submit.mode == SubmitMode(value)


def test_submit_mode_local_upstream_needs_a_key() -> None:
    config = Config.model_validate(
        {"acoustid": {"submit": {"mode": "local+upstream", "upstream_app_key": "abc"}}}
    )
    assert config.acoustid.submit.upstream_enabled is True

    with pytest.raises(ValidationError, match="upstream_app_key"):
        Config.model_validate({"acoustid": {"submit": {"mode": "local+upstream"}}})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("auth", "mode"), "basic"),
        (("acoustid", "submit", "mode"), "upstream"),
    ],
)
def test_unknown_enum_value_is_rejected(path: tuple[str, ...], value: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Config.model_validate(_nested(path, value))
    assert excinfo.value.errors()[0]["loc"] == path


# --- Uhrzeiten -------------------------------------------------------------


TIME_KEYS = [
    ("acoustid", "update", "time"),
    ("discogs", "update", "check_time"),
    ("backup", "time"),
]


@pytest.mark.parametrize("path", TIME_KEYS)
@pytest.mark.parametrize("value", ["00:00", "04:00", "09:05", "23:59"])
def test_valid_times_are_accepted(path: tuple[str, ...], value: str) -> None:
    assert _at(Config.model_validate(_nested(path, value)), path) == value


@pytest.mark.parametrize("value", ["24:00", "4:00", "0400", "23:60", "", "abends", "04:00:00"])
def test_invalid_times_are_rejected(value: str) -> None:
    path = ("acoustid", "update", "time")
    with pytest.raises(ValidationError) as excinfo:
        Config.model_validate(_nested(path, value))
    assert excinfo.value.errors()[0]["loc"] == path


def test_yaml_sexagesimal_time_is_repaired() -> None:
    """`time: 14:30` ohne Anfuehrungszeichen liest PyYAML als 870."""
    path = ("acoustid", "update", "time")
    assert _at(Config.model_validate(_nested(path, 870)), path) == "14:30"
    with pytest.raises(ValidationError):
        Config.model_validate(_nested(path, 24 * 60))


# --- Zahlen ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("wake", "hold_timeout_s"), 1),
        (("idle", "timeout_min"), 1),
        (("disk", "min_free_gb"), 0),
        (("cache", "max_size_mb"), 1),
        (("ratelimit", "per_ip_per_min"), 1),
        (("acoustid", "index", "query_hashes"), 80),
        (("caa", "crawl", "rate_per_s"), 1),
        (("covers", "negative_retry_days"), 1),
    ],
)
def test_lower_bounds_are_accepted(path: tuple[str, ...], value: int) -> None:
    assert _at(Config.model_validate(_nested(path, value)), path) == value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("wake", "hold_timeout_s"), 0),
        (("idle", "timeout_min"), 0),
        (("disk", "min_free_gb"), -1),
        (("cache", "max_size_mb"), 0),
        (("ratelimit", "per_ip_per_min"), 0),
        (("acoustid", "index", "query_hashes"), 0),
        (("caa", "crawl", "rate_per_s"), 0),
        (("covers", "negative_retry_days"), 0),
        (("notify", "smtp"), {"port": 0, "host": "mail", "from": "a@b", "to": "c@d"}),
    ],
)
def test_values_below_the_lower_bound_are_rejected(path: tuple[str, ...], value: object) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(_nested(path, value))


def test_smtp_port_upper_bound() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Config.model_validate(
            {"notify": {"smtp": {"host": "mail", "port": 70000, "from": "a@b", "to": "c@d"}}}
        )
    assert excinfo.value.errors()[0]["loc"] == ("notify", "smtp", "port")


def test_non_numeric_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"acoustid": {"index": {"query_hashes": "viele"}}})


# --- "leer = aus" ----------------------------------------------------------


def test_empty_values_mean_disabled() -> None:
    config = Config()
    assert config.notify.ntfy.enabled is False
    assert config.notify.smtp.enabled is False
    assert config.notify.enabled is False
    assert config.backup.enabled is False
    assert config.backup.directory is None
    assert config.mb.configured is False
    assert config.acoustid.submit.upstream_enabled is False
    # Die Quellen der Scope-Erweiterung sind per Vorgabe aus (v2 §2).
    assert config.discogs.configured is False
    assert config.tadb.configured is False
    assert config.caa.crawl.enabled is False


def test_filled_values_mean_enabled() -> None:
    config = Config.model_validate(
        {
            "notify": {
                "ntfy": {"url": "https://ntfy.example/acoustid"},
                "smtp": {"host": "mail.example", "from": "a@example", "to": "b@example"},
            },
            "backup": {"dir": "/mnt/backup"},
            "mb": {"dsn": "postgresql://ro@mb/musicbrainz"},
            "discogs": {"token": "abc"},
            "tadb": {"api_key": "def"},
        }
    )
    assert config.notify.ntfy.enabled is True
    assert config.notify.smtp.enabled is True
    assert config.notify.enabled is True
    assert config.backup.enabled is True
    assert str(config.backup.directory) == "/mnt/backup"
    assert config.mb.configured is True
    assert config.discogs.configured is True
    assert config.tadb.configured is True


def test_whitespace_only_counts_as_empty() -> None:
    config = Config.model_validate({"backup": {"dir": "   "}})
    assert config.backup.dir == ""
    assert config.backup.enabled is False


def test_ntfy_url_must_be_http() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Config.model_validate({"notify": {"ntfy": {"url": "ntfy.example/topic"}}})
    assert excinfo.value.errors()[0]["loc"] == ("notify", "ntfy", "url")


def test_smtp_host_requires_sender_and_recipient() -> None:
    with pytest.raises(ValidationError, match="from"):
        Config.model_validate({"notify": {"smtp": {"host": "mail.example"}}})


def test_mb_dsn_accepts_libpq_key_value_form() -> None:
    """libpq kennt URLs UND Key-Value-Strings — deshalb keine Formatpruefung."""
    config = Config.model_validate({"mb": {"dsn": "host=mb dbname=musicbrainz user=acoustid_ro"}})
    assert config.mb.configured is True


# --- Secrets ---------------------------------------------------------------


SECRET_INPUT = {
    "acoustid": {"submit": {"mode": "local", "upstream_app_key": "geheim-app-key"}},
    "notify": {"smtp": {"host": "mail.example", "pass": "geheim-smtp", "from": "a@e", "to": "b@e"}},
    "mb": {"dsn": "postgresql://ro:geheim-dsn@mb/musicbrainz"},
    # Die neuen Quellen-Zugaenge sind ebenfalls Secrets (v2 §7).
    "discogs": {"token": "geheim-discogs"},
    "tadb": {"api_key": "geheim-tadb"},
}


def test_secrets_are_secretstr() -> None:
    config = Config.model_validate(SECRET_INPUT)
    assert isinstance(config.acoustid.submit.upstream_app_key, SecretStr)
    assert isinstance(config.notify.smtp.password, SecretStr)
    assert isinstance(config.mb.dsn, SecretStr)
    assert isinstance(config.discogs.token, SecretStr)
    assert isinstance(config.tadb.api_key, SecretStr)


def test_secrets_are_masked_in_repr_and_str() -> None:
    config = Config.model_validate(SECRET_INPUT)
    dumped = f"{config!r} {config!s}"
    for secret in (
        "geheim-app-key",
        "geheim-smtp",
        "geheim-dsn",
        "geheim-discogs",
        "geheim-tadb",
    ):
        assert secret not in dumped
    assert "**********" in dumped


def test_secrets_need_explicit_access() -> None:
    config = Config.model_validate(SECRET_INPUT)
    assert config.mb.dsn.get_secret_value() == "postgresql://ro:geheim-dsn@mb/musicbrainz"
    assert config.notify.smtp.password.get_secret_value() == "geheim-smtp"


def test_config_to_dict_masks_secrets_by_default() -> None:
    config = Config.model_validate(SECRET_INPUT)
    masked = config_to_dict(config)
    assert masked["mb"]["dsn"] == "**********"
    assert masked["notify"]["smtp"]["pass"] == "**********"
    assert masked["acoustid"]["submit"]["upstream_app_key"] == "**********"
    assert masked["discogs"]["token"] == "**********"
    assert masked["tadb"]["api_key"] == "**********"

    revealed = config_to_dict(config, reveal_secrets=True)
    assert revealed["mb"]["dsn"] == "postgresql://ro:geheim-dsn@mb/musicbrainz"


# --- Feldnamen mit Alias ---------------------------------------------------


def test_smtp_aliases_accept_yaml_names_and_field_names() -> None:
    by_alias = Config.model_validate(
        {"notify": {"smtp": {"host": "m", "pass": "p", "from": "a@e", "to": "b@e"}}}
    )
    by_name = Config.model_validate(
        {
            "notify": {
                "smtp": {"host": "m", "password": "p", "from_addr": "a@e", "to": "b@e"},
            }
        }
    )
    assert by_alias == by_name
    assert config_to_dict(by_alias)["notify"]["smtp"]["from"] == "a@e"


# --- Zuweisungen zur Laufzeit (Admin-UI) -----------------------------------


def test_assignment_is_validated() -> None:
    config = Config()
    config.acoustid.index.query_hashes = 80
    assert config.acoustid.index.query_hashes == 80
    with pytest.raises(ValidationError):
        config.acoustid.index.query_hashes = 0
