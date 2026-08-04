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

#: Datenverzeichnis des Waechters — der **Cache**-Mount (HANDOFF v2 §3).
#: Bis M1a war das ``/data``; im Ein-Container-Betrieb ist ``/data`` das
#: Array, und SQLite, Keys und Lookup-Cache duerfen dort nicht liegen: der
#: Waechter schreibt im laufenden Betrieb und wuerde die Spindeln nie
#: schlafen lassen (Risiko R1 der M0-Analyse).
_DEFAULT_DATA_DIR = Path("/config")

#: Arbeitsverzeichnis der Dump-Downloads — ein **eigener** Mount (v2 §3),
#: bewusst nicht mehr von ``data_dir`` abgeleitet: die Tagesdateien sind
#: mehrere GB gross und gehoeren aufs Array, ``data_dir`` liegt auf dem
#: Cache. Ein abgeleiteter Vorgabewert wuerde sie lautlos auf den Cache
#: legen.
_DEFAULT_DUMP_DIR = Path("/import")

#: Wurzel der Postgres-Datenverzeichnisse; darunter liegt je Major-Version
#: ein Verzeichnis (``/data/db/18``). Array-Mount.
_DEFAULT_DB_DATA_ROOT = Path("/data/db")

#: Alle Dienste teilen sich im Ein-Container-Betrieb einen
#: Netzwerk-Namensraum; erreichbar ist nur der Waechter-Port.
_DEFAULT_API_PORT = 8081
_DEFAULT_API_BASE_URL = f"http://127.0.0.1:{_DEFAULT_API_PORT}"

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

    #: AOFF_PORT — ein Port fuer API-Proxy und Admin-UI (/admin). Der
    #: einzige veroeffentlichte Port des Containers.
    port: int = Field(default=8080, ge=1, le=65535)
    #: AOFF_DATA_DIR — Datenverzeichnis des Waechters (Cache-Mount
    #: ``/config``; **nie** unter ``/data``, das ist das Array).
    data_dir: Path = _DEFAULT_DATA_DIR
    #: AOFF_CONFIG_PATH — Default: <data_dir>/config.yaml.
    config_path: Path = _DEFAULT_DATA_DIR / "config.yaml"
    #: AOFF_DUMP_DIR — Arbeitsverzeichnis des Importers (Mount ``/import``,
    #: Array). Folgt bewusst **nicht** `data_dir` (siehe Modulkonstante).
    dump_dir: Path = _DEFAULT_DUMP_DIR

    #: AOFF_DB_* — Zugang zur AcoustID-Postgres (containerintern, Loopback).
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "acoustid"
    db_user: str = "acoustid"
    #: Wird seit M1b vom Entrypoint erzeugt (E16) — kein `.env`-Pflichtwert
    #: mehr. Leer bleibt zulaessig, damit der Waechter (der keine Postgres
    #: braucht) ohne ihn startet; `db_dsn()` meldet das Fehlen genau dann,
    #: wenn der Zugang gebraucht wird.
    db_password: SecretStr = SecretStr("")
    #: AOFF_DB_PASSWORD_FILE — Datei, aus der das Passwort gelesen wird,
    #: wenn `db_password` leer ist. Default: <data_dir>/db-password.
    #:
    #: **Warum eine Datei und nicht nur die Umgebung.** Der Entrypoint
    #: erzeugt das Passwort beim ersten Start und koennte es exportieren —
    #: aber nur an *seine* Kinder. Ein `docker compose exec` (Bootstrap,
    #: Importer-Lauf, Admin-Skript) startet einen Prozess **neben** ihm und
    #: saehe davon nichts; der Zugang haenge dann daran, wie man
    #: hineingekommen ist. Die Datei ist fuer alle dieselbe Quelle — und
    #: taugt zugleich als Docker-Secret (§8.10 „Secrets nie im Repo").
    db_password_file: Path = _DEFAULT_DATA_DIR / "db-password"
    #: AOFF_DB_DATA_ROOT — Wurzel der Postgres-Datenverzeichnisse
    #: (``<root>/<major>``). Der Waechter braucht sie fuer den
    #: Versions-Drift-Guard (E14), sonst niemand: die Datenbank spricht er
    #: nie an (Invariante §8.2).
    db_data_root: Path = _DEFAULT_DB_DATA_ROOT
    #: AOFF_PG_MAJOR — Major-Version, die **dieses Image** mitbringt. Der
    #: Wert wird im Dockerfile gesetzt und ist keine Betreiber-Einstellung:
    #: er beschreibt das Artefakt, gegen das der Drift-Guard prueft.
    pg_major: int = Field(default=18, ge=1, le=999)

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
    #: AOFF_API_PORT — Port, auf dem der API-Dienst lauscht. Seit M1b
    #: bindend: Waechter und API teilen sich einen Netzwerk-Namensraum, und
    #: der Waechter belegt `port` (8080) bereits. Der Dienst lauscht damit
    #: auf ``127.0.0.1:<api_port>`` und ist von aussen nicht erreichbar.
    api_port: int = Field(default=_DEFAULT_API_PORT, ge=1, le=65535)

    #: AOFF_INDEX_URL — acoustid-index, nur containerintern erreichbar.
    index_url: str = "http://127.0.0.1:6081"
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

        `config_path` folgt `data_dir`, `api_health_url` folgt
        `api_base_url`. Zweck ist in beiden Faellen derselbe: wer die
        Wurzel umzieht, muss den abgeleiteten Wert nicht mitpflegen — und
        eine halb umgezogene Umgebung (neue Basis-URL, alter Healthcheck)
        kann gar nicht erst entstehen.

        **`dump_dir` folgt seit M1b nicht mehr** (v2 §3): es ist ein eigener
        Mount auf dem Array, `data_dir` liegt auf dem Cache. Die Ableitung
        haette mehrere GB Tagesdateien lautlos auf den Cache-Pool gelegt —
        genau die Art Fehler, gegen die die Ableitung sonst schuetzt.
        """
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        data_dir = Path(values.get("data_dir") or _DEFAULT_DATA_DIR)
        if not values.get("config_path"):
            values["config_path"] = data_dir / "config.yaml"
        if not values.get("db_password_file"):
            values["db_password_file"] = data_dir / "db-password"
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

        Das Passwort kommt aus der Variablen oder — wenn sie leer ist — aus
        `db_password_file`. Gelesen wird **hier** und nicht beim Bauen der
        Einstellungen: der Waechter braucht den Zugang nie, und eine
        fehlende Datei duerfte seinen Start nicht verhindern.

        Raises:
            EnvError: Weder Variable noch Datei liefern ein Passwort.
        """
        password = self.db_password.get_secret_value() or self._password_from_file()
        if not password:
            raise EnvError(
                f"{env_var_name('db_password')} ist nicht gesetzt und "
                f"{self.db_password_file} nicht lesbar — "
                "ohne Passwort ist kein Postgres-Zugang moeglich"
            )
        user = quote(self.db_user, safe="")
        secret = quote(password, safe="")
        return SecretStr(
            f"postgresql://{user}:{secret}@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    def _password_from_file(self) -> str:
        """Das Passwort aus `db_password_file`, oder leer.

        Jeder Lesefehler heisst dasselbe wie „keine Datei": der Aufrufer
        bekommt gleich eine Meldung, die beide Wege nennt. Ein Stacktrace
        ueber fehlende Rechte waere hier keine bessere Auskunft.
        """
        try:
            return self.db_password_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def env_var_name(field_name: str) -> str:
    """Feldname -> Umgebungsvariable (`db_password` -> `AOFF_DB_PASSWORD`)."""
    return f"{ENV_PREFIX}{field_name.upper()}"
