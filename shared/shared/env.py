"""Bootstrap-Einstellungen aus `AOFF_`-Umgebungsvariablen (ARCHITECTURE §6).

Bewusst getrennt von der `config.yaml`: hier stehen nur Werte, die schon
feststehen muessen, BEVOR die Laufzeit-Konfiguration gelesen werden kann —
Pfade, Ports, DB-/Index-/API-Adressen und der Log-Level. Alles andere (auth,
submit, wake, idle, update, cache, ratelimit, metrics, notify, backup,
mb.dsn, index.query_hashes) gehoert in die config.yaml und wird ueber die
Admin-UI gepflegt.

Der Variablensatz ist deckungsgleich mit `.env.example` im Repo-Wurzel-
verzeichnis; beide werden zusammen gepflegt.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

__all__ = [
    "ENV_PREFIX",
    "EnvError",
    "EnvSettings",
    "LogLevelName",
    "env_var_name",
]

_LOG = logging.getLogger(__name__)

#: Praefix aller Bootstrap-Variablen (ARCHITECTURE §6).
ENV_PREFIX = "AOFF_"

_DEFAULT_DATA_DIR = Path("/data")
_DEFAULT_API_BASE_URL = "http://acoustid-api:8080"
_LEVEL_NAMES = frozenset(logging.getLevelNamesMapping())


class EnvError(Exception):
    """Fehlerhafte oder unvollstaendige `AOFF_`-Umgebung."""


def _validate_log_level(value: str) -> str:
    level = value.upper()
    if level not in _LEVEL_NAMES:
        known = ", ".join(sorted(_LEVEL_NAMES))
        raise ValueError(f"unbekannter Log-Level {value!r}; erlaubt sind: {known}")
    return level


#: Log-Level-Name, gross geschrieben und gegen das logging-Modul geprueft.
LogLevelName = Annotated[str, AfterValidator(_validate_log_level)]


class EnvSettings(BaseModel):
    """Bootstrap-Werte eines Service-Starts.

    Wird ueber `EnvSettings.from_env()` aus der Umgebung gelesen; direkte
    Konstruktion ist fuer Tests und Defaults gedacht. Die Instanz ist
    unveraenderlich — Bootstrap-Werte aendern sich zur Laufzeit nicht.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=False,
        str_strip_whitespace=True,
    )

    #: AOFF_PORT — ein Port fuer API-Proxy und Admin-UI (/admin).
    port: int = Field(default=8080, ge=1, le=65535)
    #: AOFF_DATA_DIR — Datenverzeichnis des Waechters (SSD-Cache-Pool).
    data_dir: Path = _DEFAULT_DATA_DIR
    #: AOFF_CONFIG_PATH — Default: <data_dir>/config.yaml.
    config_path: Path = _DEFAULT_DATA_DIR / "config.yaml"
    #: AOFF_DUMP_DIR — Arbeitsverzeichnis des Importers; Default:
    #: <data_dir>/dumps.
    dump_dir: Path = _DEFAULT_DATA_DIR / "dumps"

    #: AOFF_DB_* — Zugang zur AcoustID-Postgres (Stack, Array).
    db_host: str = "acoustid-db"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "acoustid"
    db_user: str = "acoustid"
    #: Pflichtwert vor dem ersten Start; leer bleibt zulaessig, damit der
    #: Waechter (der keine Postgres braucht) ohne ihn startet. `db_dsn()`
    #: meldet das Fehlen genau dann, wenn der Zugang gebraucht wird.
    db_password: SecretStr = SecretStr("")

    #: AOFF_API_BASE_URL — Basis-URL des API-Dienstes; das Ziel des
    #: Reverse-Proxys im Waechter (``/v2/*``). Bootstrap-Wert, weil der
    #: Proxy steht, bevor die config.yaml gelesen ist.
    api_base_url: str = _DEFAULT_API_BASE_URL
    #: AOFF_API_HEALTH_URL — interner Healthcheck des API-Dienstes, die
    #: Bereitschaftsfrage des Weckvorgangs (DECISIONS 2026-08-01). Kein Teil
    #: des §7-Vertrags und nicht unter ``/v2/``: er beantwortet genau eine
    #: Frage, die kein oeffentlicher Endpunkt zuverlaessig beantwortet —
    #: „sind Datenbank und Index angebunden?". Ohne eigenen Wert folgt er
    #: `api_base_url`.
    api_health_url: str = f"{_DEFAULT_API_BASE_URL}/_health"
    #: AOFF_API_PORT — Port, auf dem der API-Dienst lauscht. Heute ist er
    #: reine Dokumentation: im Compose-Stack hat jeder Dienst seine eigene
    #: Adresse, und die beiden URLs oben tragen den Port bereits. Er steht
    #: schon jetzt im Schema, weil der Ein-Container-Umbau ihn braucht —
    #: dort teilen Waechter und API einen Netzwerk-Namensraum, und der
    #: Waechter belegt `port` (8080) bereits.
    api_port: int = Field(default=8080, ge=1, le=65535)

    #: AOFF_INDEX_URL — acoustid-index, nur Compose-intern erreichbar.
    index_url: str = "http://acoustid-index:6081"
    #: AOFF_INDEX_NAME — Name des Suchindex im acoustid-index. Ein Server kann
    #: mehrere halten; wir fahren genau einen. Bootstrap-Wert, weil auch der
    #: Healthcheck des Containers (`/<name>/_health`) ihn braucht — dort gibt
    #: es keine config.yaml.
    index_name: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

    #: AOFF_LOG_LEVEL — Level fuer `shared.setup_logging`, noetig bevor die
    #: config.yaml gelesen ist (die kennt bewusst keinen Log-Level).
    log_level: LogLevelName = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _derive_defaults(cls, data: Any) -> Any:
        """Werte, die einem anderen folgen, solange sie nicht gesetzt sind.

        `config_path`/`dump_dir` folgen `data_dir`, `api_health_url` folgt
        `api_base_url`. Zweck ist in beiden Faellen derselbe: wer die
        Wurzel umzieht, muss die abgeleiteten Werte nicht mitpflegen — und
        eine halb umgezogene Umgebung (neue Basis-URL, alter Healthcheck)
        kann gar nicht erst entstehen.
        """
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        data_dir = Path(values.get("data_dir") or _DEFAULT_DATA_DIR)
        if not values.get("config_path"):
            values["config_path"] = data_dir / "config.yaml"
        if not values.get("dump_dir"):
            values["dump_dir"] = data_dir / "dumps"
        if not values.get("api_health_url"):
            base = str(values.get("api_base_url") or _DEFAULT_API_BASE_URL)
            values["api_health_url"] = f"{base.rstrip('/')}/_health"
        return values

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EnvSettings:
        """Liest alle `AOFF_`-Variablen; leere Werte gelten als nicht gesetzt.

        Unbekannte `AOFF_`-Variablen werden mit einer Warnung ignoriert
        (Tippfehler-Erkennung, gleiche Regel wie in der config.yaml).

        Raises:
            EnvError: eine Variable hat einen unzulaessigen Wert.
        """
        source = os.environ if environ is None else environ
        by_env = {env_var_name(name): name for name in cls.model_fields}

        values: dict[str, str] = {}
        for key, raw in source.items():
            if not key.startswith(ENV_PREFIX):
                continue
            field_name = by_env.get(key)
            if field_name is None:
                _LOG.warning("Unbekannte Umgebungsvariable wird ignoriert", extra={"env_var": key})
                continue
            if raw.strip() == "":
                continue
            values[field_name] = raw

        try:
            return cls(**values)
        except ValidationError as exc:
            details = "; ".join(
                f"{env_var_name(str(item['loc'][0])) if item['loc'] else '<umgebung>'}: "
                f"{item['msg']}"
                for item in exc.errors()
            )
            raise EnvError(f"Ungueltige Bootstrap-Umgebung: {details}") from exc

    def db_dsn(self) -> SecretStr:
        """Postgres-DSN aus den `AOFF_DB_*`-Werten.

        Raises:
            EnvError: `AOFF_DB_PASSWORD` ist nicht gesetzt.
        """
        password = self.db_password.get_secret_value()
        if not password:
            raise EnvError(
                f"{env_var_name('db_password')} ist nicht gesetzt — "
                "ohne Passwort ist kein Postgres-Zugang moeglich"
            )
        user = quote(self.db_user, safe="")
        secret = quote(password, safe="")
        return SecretStr(
            f"postgresql://{user}:{secret}@{self.db_host}:{self.db_port}/{self.db_name}"
        )


def env_var_name(field_name: str) -> str:
    """Feldname -> Umgebungsvariable (`db_password` -> `AOFF_DB_PASSWORD`)."""
    return f"{ENV_PREFIX}{field_name.upper()}"
