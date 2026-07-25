"""Laufzeit-Konfiguration von acoustid-offline (ARCHITECTURE §6).

Enthaelt das vollstaendige Schema der `config.yaml` (Cache-Volume des
Waechters, editierbar ueber die Admin-UI) sowie das verlustfreie Laden und
atomare Schreiben der Datei.

Grundregeln (aus ARCHITECTURE §6 und den DECISIONS vom 2026-07-25):

* **Leerer String = "aus".** `notify.ntfy.url`, `notify.smtp.host`,
  `backup.dir`, `mb.dsn` und `submit.upstream_app_key` sind per Default
  leer; die zugehoerige Funktion ist damit abgeschaltet. Die Sektionen
  bieten dafuer `enabled`/`configured`-Properties, damit der Aufrufer die
  Regel nicht nachbauen muss.
* **Unbekannte Schluessel werden ignoriert** (mit Warnung im Log), damit
  ein Downgrade oder eine handgeschriebene Datei aus einer neueren Version
  den Start nicht verhindert.
* **Secrets** (`submit.upstream_app_key`, `notify.smtp.pass`, `mb.dsn`)
  sind `SecretStr`: in `repr()`, `str()` und Log-Ausgaben maskiert, im
  Klartext nur ueber `get_secret_value()` bzw. beim Schreiben der Datei.
* **Bootstrap-Werte** (Pfade, Ports, DB-Zugaenge) stehen bewusst NICHT
  hier, sondern in den `AOFF_`-Env-Variablen (siehe `shared.env`).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from pydantic.fields import FieldInfo

from shared.models import AuthMode, SubmitMode

__all__ = [
    "AuthConfig",
    "BackupConfig",
    "CacheConfig",
    "Config",
    "ConfigError",
    "ConfigFileError",
    "ConfigValidationError",
    "IdleConfig",
    "IndexConfig",
    "MbConfig",
    "MetricsConfig",
    "NotifyConfig",
    "NtfyConfig",
    "RatelimitConfig",
    "SmtpConfig",
    "SubmitConfig",
    "UpdateConfig",
    "WakeConfig",
    "config_to_dict",
    "load_config",
    "save_config",
]

_LOG = logging.getLogger(__name__)

# Kopfzeilen der geschriebenen Datei. Kommentare des Nutzers ueberleben ein
# Schreiben durch die Admin-UI nicht (yaml.safe_dump kennt keine Kommentare) —
# die Werte selbst bleiben verlustfrei.
_FILE_HEADER = (
    "# config.yaml — Laufzeit-Konfiguration von acoustid-offline (ARCHITECTURE §6).\n"
    "# Von der Admin-UI geschrieben; handgeschriebene Kommentare gehen dabei verloren.\n"
    "# Unbekannte Schluessel werden beim Laden mit einer Warnung ignoriert.\n"
    "# Leerer Wert bedeutet 'aus' (ntfy, smtp, backup.dir, mb.dsn, upstream_app_key).\n"
)

# Dateirechte der geschriebenen config.yaml: sie enthaelt Secrets im Klartext
# (ARCHITECTURE §8.10 — Secrets gehoeren in .env/config.yaml, nie ins Repo).
_FILE_MODE = 0o600


# --- Bausteine -------------------------------------------------------------

_HHMM_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _coerce_hhmm(value: Any) -> Any:
    """Faengt die YAML-1.1-Sexagesimalfalle ab.

    PyYAML liest `time: 14:30` (ohne Anfuehrungszeichen) als Zahl 870 —
    Minuten seit Mitternacht. Selbst geschriebene Dateien sind davon nicht
    betroffen (der Emitter quotet solche Werte), handgeschriebene schon.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        hours, minutes = divmod(value, 60)
        if 0 <= hours <= 23:
            return f"{hours:02d}:{minutes:02d}"
    return value


def _validate_hhmm(value: str) -> str:
    if not _HHMM_RE.fullmatch(value):
        raise ValueError(
            f"Uhrzeit muss im Format HH:MM (00:00-23:59) angegeben werden, nicht {value!r}"
        )
    return value


def _validate_url(value: str) -> str:
    if value and not value.startswith(("http://", "https://")):
        raise ValueError(f"URL muss mit http:// oder https:// beginnen, nicht {value!r}")
    return value


#: Uhrzeit als `HH:MM` in lokaler Zeit (`update.time`, `backup.time`).
TimeOfDay = Annotated[str, BeforeValidator(_coerce_hhmm), AfterValidator(_validate_hhmm)]

#: Ganzzahl >= 1 — fuer Zeitfenster, Groessen und Limits, bei denen 0 keinen
#: sinnvollen Betrieb ergibt (abgeschaltet wird ueber den jeweiligen Schalter).
PositiveInt = Annotated[int, Field(ge=1)]

#: Ganzzahl >= 0 — dort, wo 0 fachlich zulaessig ist (z. B. keine Reserve).
NonNegativeInt = Annotated[int, Field(ge=0)]

#: Optionale URL: leer = aus.
OptionalUrl = Annotated[str, AfterValidator(_validate_url)]


class _Section(BaseModel):
    """Basisklasse aller Config-Sektionen.

    `extra="ignore"`: unbekannte Schluessel brechen das Laden nicht ab; die
    Warnung dazu erzeugt `load_config` (mit vollem Schluesselpfad), bevor
    validiert wird.
    """

    model_config = ConfigDict(
        extra="ignore",
        validate_assignment=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# --- Sektionen (Reihenfolge wie ARCHITECTURE §6) ---------------------------


class AuthConfig(_Section):
    """`auth.*` — Durchsetzung im Waechter, nie im API-Service (§7)."""

    mode: AuthMode = AuthMode.NONE
    #: Projekt-Ergaenzung (DECISIONS 2026-07-25): oeffentlich bekannte Keys
    #: von Picard/beets im apikey-Modus zulassen. Default bewusst aus.
    allow_known_client_keys: bool = False


class SubmitConfig(_Section):
    """`submit.*` — Verhalten von `/v2/submit` (§7)."""

    mode: SubmitMode = SubmitMode.LOCAL
    #: Eigener Application-Key fuer api.acoustid.org; leer = keiner.
    upstream_app_key: SecretStr = SecretStr("")

    @property
    def upstream_enabled(self) -> bool:
        return self.mode is SubmitMode.LOCAL_UPSTREAM

    @model_validator(mode="after")
    def _upstream_needs_key(self) -> Self:
        if self.upstream_enabled and not self.upstream_app_key.get_secret_value():
            raise ValueError(
                "submit.mode 'local+upstream' erfordert einen submit.upstream_app_key "
                "(eigener Application-Key von acoustid.org)"
            )
        return self


class WakeConfig(_Section):
    """`wake.*` — Halten von Anfragen waehrend des Weckens (§7)."""

    hold_timeout_s: PositiveInt = 90


class IdleConfig(_Section):
    """`idle.*` — Auto-Stopp des Stacks (§8.5)."""

    timeout_min: PositiveInt = 15


class UpdateConfig(_Section):
    """`update.*` — taeglicher Delta-Import (§8.8)."""

    time: TimeOfDay = "04:00"
    #: Plattenplatz-Guard; 0 = kein Mindestbestand gefordert.
    min_free_gb: NonNegativeInt = 50


class CacheConfig(_Section):
    """`cache.*` — Lookup-Cache im Waechter (§8.6)."""

    enabled: bool = True
    max_size_mb: PositiveInt = 512


class RatelimitConfig(_Section):
    """`ratelimit.*` — IP-Limit am Proxy (§7)."""

    per_ip_per_min: PositiveInt = 120


class MetricsConfig(_Section):
    """`metrics.*` — Prometheus-Endpoint `/metrics` (§7)."""

    enabled: bool = False


class NtfyConfig(_Section):
    """`notify.ntfy.*` — Webhook-Ziel; leere URL = aus."""

    url: OptionalUrl = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url)


class SmtpConfig(_Section):
    """`notify.smtp.*` — Mailversand; leerer Host = aus.

    `pass` und `from` sind Python-Schluesselwoerter bzw. belegt, deshalb
    heissen die Felder `password`/`from_addr` und tragen die YAML-Namen als
    Alias. Geschrieben und gelesen wird immer `pass` bzw. `from`.
    """

    host: str = ""
    #: Kein "leerer" Port moeglich — 587 (Submission) als Standard; der
    #: Schalter fuer SMTP ist `host`.
    port: int = Field(default=587, ge=1, le=65535)
    user: str = ""
    password: SecretStr = Field(default=SecretStr(""), alias="pass")
    from_addr: str = Field(default="", alias="from")
    to: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    @model_validator(mode="after")
    def _sender_and_recipient_required(self) -> Self:
        if self.enabled and not (self.from_addr and self.to):
            raise ValueError(
                "notify.smtp.host gesetzt: 'from' und 'to' muessen ebenfalls gefuellt sein"
            )
        return self


class NotifyConfig(_Section):
    """`notify.*` — Benachrichtigungskanaele (§6)."""

    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)

    @property
    def enabled(self) -> bool:
        return self.ntfy.enabled or self.smtp.enabled


class BackupConfig(_Section):
    """`backup.*` — Sicherung nach dem Update-Lauf; leeres Ziel = aus."""

    dir: str = ""
    time: TimeOfDay = "04:45"

    @property
    def enabled(self) -> bool:
        return bool(self.dir)

    @property
    def directory(self) -> Path | None:
        return Path(self.dir) if self.dir else None


class MbConfig(_Section):
    """`mb.*` — Read-only-Zugang zur MusicBrainz-Postgres (§5.4).

    Der DSN wird bewusst nicht auf ein Format geprueft: libpq akzeptiert
    sowohl URLs (`postgresql://…`) als auch Key-Value-Strings
    (`host=… dbname=…`).
    """

    dsn: SecretStr = SecretStr("")
    #: Projekt-Ergaenzung (Phase 10): Wird eine eingereichte Recording-MBID
    #: ueber `recording_gid_redirect` aufgeloest, traegt die Antwort per
    #: Default die **kanonische** MBID — das ist die Angabe, mit der ein
    #: Client in MusicBrainz weiterarbeiten kann. `true` reicht stattdessen
    #: die eingereichte MBID durch (fuer Bestaende, die nach der alten MBID
    #: schluesseln und den Wert wiedererkennen muessen).
    keep_submitted_mbid: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.dsn.get_secret_value())


class IndexConfig(_Section):
    """`index.*` — Projekt-Ergaenzung (DECISIONS 2026-07-25).

    `query_hashes` bestimmt, wie viele Query-Hashes je Fingerprint in den
    acoustid-index gehen (RAM-abhaengig). Eine Aenderung erfordert einen
    Index-Neuaufbau.
    """

    query_hashes: PositiveInt = 120


class Config(_Section):
    """Vollstaendige Laufzeit-Konfiguration (ARCHITECTURE §6).

    `Config()` liefert exakt die dort dokumentierten Defaults.
    """

    auth: AuthConfig = Field(default_factory=AuthConfig)
    submit: SubmitConfig = Field(default_factory=SubmitConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    idle: IdleConfig = Field(default_factory=IdleConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    ratelimit: RatelimitConfig = Field(default_factory=RatelimitConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    mb: MbConfig = Field(default_factory=MbConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)


# --- Fehler ----------------------------------------------------------------


class ConfigError(Exception):
    """Basis aller Konfigurationsfehler."""


class ConfigFileError(ConfigError):
    """Die Datei ist nicht lesbar oder kein YAML-Mapping."""


class ConfigValidationError(ConfigError):
    """Mindestens ein Wert verletzt das Schema.

    `errors` haelt die Einzelfehler als `(schluesselpfad, meldung)`; die
    Textform nennt jeden Pfad in Punktnotation (z. B. `notify.smtp.port`).
    """

    def __init__(self, source: str, errors: list[tuple[str, str]]) -> None:
        self.source = source
        self.errors = errors
        details = "\n".join(f"  - {path}: {message}" for path, message in errors)
        super().__init__(f"Ungueltige Konfiguration in {source}:\n{details}")

    @classmethod
    def from_validation_error(cls, source: str, error: ValidationError) -> ConfigValidationError:
        errors: list[tuple[str, str]] = []
        for item in error.errors():
            path = ".".join(str(part) for part in item["loc"]) or "<root>"
            errors.append((path, item["msg"]))
        return cls(source, errors)


# --- Unbekannte Schluessel -------------------------------------------------


def _known_keys(model: type[BaseModel]) -> dict[str, FieldInfo]:
    """Feldnamen UND Aliase einer Sektion (z. B. `password` und `pass`)."""
    known: dict[str, FieldInfo] = {}
    for name, field in model.model_fields.items():
        known[name] = field
        if field.alias:
            known[field.alias] = field
    return known


def _section_model(field: FieldInfo) -> type[BaseModel] | None:
    annotation = field.annotation
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _collect_unknown_keys(
    data: Mapping[Any, Any], model: type[BaseModel], prefix: str = ""
) -> list[str]:
    """Alle Schluesselpfade in `data`, die `model` nicht kennt."""
    unknown: list[str] = []
    known = _known_keys(model)
    for raw_key, value in data.items():
        path = f"{prefix}{raw_key}"
        field = known.get(str(raw_key))
        if field is None:
            unknown.append(path)
            continue
        nested = _section_model(field)
        if nested is not None and isinstance(value, Mapping):
            unknown.extend(_collect_unknown_keys(value, nested, f"{path}."))
    return unknown


# --- Serialisierung --------------------------------------------------------


def _plain(value: Any, *, reveal_secrets: bool) -> Any:
    """YAML-taugliche Grundtypen; Secrets nur auf ausdruecklichen Wunsch."""
    if isinstance(value, SecretStr):
        return value.get_secret_value() if reveal_secrets else str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _plain(item, reveal_secrets=reveal_secrets) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_plain(item, reveal_secrets=reveal_secrets) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def config_to_dict(config: Config, *, reveal_secrets: bool = False) -> dict[str, Any]:
    """Verschachteltes dict mit den YAML-Schluesselnamen (Aliase).

    Secrets sind standardmaessig maskiert — fuer Anzeige und Logs. Nur
    `save_config` setzt `reveal_secrets=True`.
    """
    dumped = config.model_dump(by_alias=True)
    return _plain(dumped, reveal_secrets=reveal_secrets)


# --- Laden & Schreiben -----------------------------------------------------


def load_config(
    path: str | os.PathLike[str],
    *,
    create_if_missing: bool = False,
) -> Config:
    """Liest `path` und fuellt fehlende Schluessel mit den Defaults auf.

    Fehlt die Datei, entsteht eine vollstaendige Default-Config; mit
    `create_if_missing=True` wird sie zusaetzlich angelegt. Unbekannte
    Schluessel werden mit Warnung ignoriert.

    Raises:
        ConfigFileError: Datei unlesbar oder kein YAML-Mapping.
        ConfigValidationError: Werte verletzen das Schema.
    """
    config_path = Path(path)
    if not config_path.is_file():
        config = Config()
        _LOG.info(
            "Konfiguration nicht vorhanden, Defaults werden verwendet",
            extra={"config_path": str(config_path), "file_created": create_if_missing},
        )
        if create_if_missing:
            save_config(config, config_path)
        return config

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigFileError(f"{config_path}: Datei nicht lesbar: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"{config_path}: kein gueltiges YAML: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigFileError(
            f"{config_path}: erwartet wird ein YAML-Mapping, gefunden {type(data).__name__}"
        )

    for unknown in _collect_unknown_keys(data, Config):
        _LOG.warning(
            "Unbekannter Konfigurationsschluessel wird ignoriert",
            extra={"config_path": str(config_path), "config_key": unknown},
        )

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigValidationError.from_validation_error(str(config_path), exc) from exc


def save_config(config: Config, path: str | os.PathLike[str]) -> Path:
    """Schreibt `config` atomar nach `path` (Temp-Datei + `os.replace`).

    Ein abgebrochener Schreibvorgang laesst die bisherige Datei unveraendert;
    eine halb geschriebene config.yaml kann nicht entstehen. Die Datei
    enthaelt Secrets im Klartext und bekommt Modus 0600.
    """
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    body = yaml.safe_dump(
        config_to_dict(config, reveal_secrets=True),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=config_path.parent, prefix=f"{config_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(_FILE_HEADER)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(_FILE_MODE)
        tmp_path.replace(config_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # Den Verzeichniseintrag mit haltbar machen, sonst kann der Rename bei
    # einem Stromausfall verloren gehen.
    dir_fd = os.open(config_path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - nicht jedes Dateisystem kann das
        pass
    finally:
        os.close(dir_fd)

    _LOG.info("Konfiguration geschrieben", extra={"config_path": str(config_path)})
    return config_path
