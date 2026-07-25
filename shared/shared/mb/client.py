"""Zugang zur MusicBrainz-Spiegel-Datenbank (ARCHITECTURE §5.4).

Der Client haelt alles, was die Abfragen aus :mod:`shared.mb.queries` vom
Betrieb trennt: einen eigenen kleinen Verbindungs-Pool mit den
Sicherheitsoptionen des Berichts, den Circuit-Breaker, den Selfcheck beim
Start und die Uebersetzung jedes Treiberfehlers in
:mod:`shared.mb.errors`.

**Warum ein eigener Pool.** Die MB-Postgres ist nicht unsere Datenbank: sie
laeuft in einem fremden Compose-Stack, kann jederzeit weg sein und darf
unseren Lookup-Pool nicht mit in die Wartezeit reissen. Deshalb ein
zweiter, kleiner Pool mit kurzen Fristen — lieber degradiert antworten als
lange warten.

**Verbindungsoptionen** (alle aus dem Phase-1-Bericht)::

    connect_timeout                     = 2 s
    statement_timeout                   = 2000 ms
    default_transaction_read_only       = on
    idle_in_transaction_session_timeout = 5000 ms
    search_path                         = musicbrainz, public

``default_transaction_read_only`` ist ein Guertel zum Hosentraeger der
Read-only-Rolle ``acoustid_ro``: selbst mit versehentlich zu weit
vergebenen Rechten kann diese Verbindung nichts schreiben.

**Eine Transaktion je Anfrage.** :meth:`MbClient.session` oeffnet genau eine
Read-only-Transaktion; alle 4 bis 7 Abfragen eines Lookups sehen denselben
Snapshot. Ohne das koennte mitten in der Antwort ein Replikationslauf
durchlaufen und die Release-Zeilen zu einer Aufnahme gehoeren, die es in
der Recording-Zeile schon nicht mehr gibt.

**Nichts hiervon wirft psycopg nach draussen.** Der API-Dienst sieht nur
:class:`~shared.mb.errors.MbUnavailable` (degradiert antworten),
:class:`~shared.mb.errors.MbSchemaMismatch` (dito, aber laut) und
:class:`~shared.mb.errors.MbQueryError` (HTTP 500).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, Self

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from shared.config import Config
from shared.mb import queries
from shared.mb.breaker import CircuitBreaker
from shared.mb.errors import MbError, MbQueryError, MbSchemaMismatch, MbStale, MbUnavailable
from shared.mb.metadata import MetadataResult, lookup_metadata
from shared.mb.queries import DEFAULT_ROW_LIMIT, MbHealth

__all__ = [
    "CONNECT_TIMEOUT_S",
    "DEFAULT_POOL_MAX_SIZE",
    "DEFAULT_POOL_TIMEOUT_S",
    "EXPECTED_SCHEMA_SEQUENCE",
    "IDLE_IN_TRANSACTION_TIMEOUT_MS",
    "STALE_CRIT_HOURS",
    "STALE_WARN_HOURS",
    "STATEMENT_TIMEOUT_MS",
    "MbClient",
    "MbStatus",
    "connection_options",
    "translate_error",
]

_LOG = logging.getLogger(__name__)

#: Verbindungsaufbau; laenger warten lohnt nicht, wir degradieren ohnehin.
CONNECT_TIMEOUT_S: Final = 2

#: Serverseitige Frist je Anweisung.
STATEMENT_TIMEOUT_MS: Final = 2000

#: Notbremse gegen eine haengende Transaktion auf der fremden Datenbank.
IDLE_IN_TRANSACTION_TIMEOUT_MS: Final = 5000

#: Obergrenze des MB-Pools. Ein Lookup braucht genau eine Verbindung; mehr
#: als eine Handvoll gleichzeitiger Lookups sieht eine Privatinstanz nicht.
DEFAULT_POOL_MAX_SIZE: Final = 4

#: So lange darf eine Anfrage auf eine freie Verbindung warten.
DEFAULT_POOL_TIMEOUT_S: Final = 2.0

#: Schema-Sequenz, gegen die Phase 1 verifiziert hat. Eine abweichende
#: Sequenz ist **kein** Fehler (die Aenderungen waren bisher additiv), nur
#: ein Hinweis fuers Log — der Selfcheck prueft die Spalten selbst.
EXPECTED_SCHEMA_SEQUENCE: Final = 31

#: Der Spiegel repliziert taeglich (03:00); ab hier ist etwas im Argen.
STALE_WARN_HOURS: Final = 36.0

#: Ab hier ist die Replikation offensichtlich stehen geblieben.
STALE_CRIT_HOURS: Final = 168.0  # 7 Tage

#: SQLSTATE-Klassen, die „der Spiegel sieht anders aus als erwartet"
#: bedeuten — fehlende Relation/Spalte oder verweigerte Rechte. Sie werden
#: zu :class:`MbSchemaMismatch` und damit zu einer degradierten Antwort;
#: alles andere ist ein :class:`MbQueryError` (HTTP 500).
_SCHEMA_ERRORS: Final = (
    psycopg.errors.UndefinedTable,
    psycopg.errors.UndefinedColumn,
    psycopg.errors.UndefinedObject,
    psycopg.errors.UndefinedFunction,
    psycopg.errors.InsufficientPrivilege,
    psycopg.errors.InvalidSchemaName,
)


def connection_options() -> str:
    """Die ``options``-Zeichenkette fuer libpq (siehe Modul-Docstring)."""
    return " ".join(
        (
            f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            "-c default_transaction_read_only=on",
            f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}",
            f"-c search_path={queries.SCHEMA},public",
        )
    )


def translate_error(exc: Exception) -> MbQueryError | MbSchemaMismatch | MbUnavailable:
    """Treiberfehler in die Fehlerhierarchie der Schicht uebersetzen.

    * Pool-Wartezeit und alles, was psycopg als ``OperationalError`` meldet
      (Verbindung weg, ``statement_timeout`` abgelaufen, Netz) ->
      :class:`MbUnavailable`.
    * Fehlende Relation/Spalte und verweigerte Rechte ->
      :class:`MbSchemaMismatch`.
    * Jeder andere Datenbankfehler -> :class:`MbQueryError`.
    """
    if isinstance(exc, PoolTimeout):
        return MbUnavailable(f"keine freie MB-Verbindung im Pool: {exc}")
    if isinstance(exc, _SCHEMA_ERRORS):
        return MbSchemaMismatch([f"<{type(exc).__name__}>: {str(exc).strip()}"])
    if isinstance(exc, psycopg.OperationalError):
        return MbUnavailable(f"MusicBrainz-Postgres nicht ansprechbar: {str(exc).strip()}")
    if isinstance(exc, psycopg.Error):
        return MbQueryError(f"MB-Abfrage gescheitert: {str(exc).strip()}")
    return MbQueryError(f"unerwarteter Fehler in der MB-Schicht: {exc!r}")


@dataclass(frozen=True, slots=True)
class MbStatus:
    """Ergebnis von :meth:`MbClient.startup_check` bzw. eines Verbindungstests.

    Attributes:
        reachable: Verbindung stand und die Abfragen liefen.
        schema_ok: Alle erwarteten Spalten vorhanden.
        missing: Fehlende Spalten als ``"relation.spalte"``.
        health: ``replication_control``-Zeile, falls lesbar.
        stale: Gesetzt, wenn die Replikation ueber der WARN-Schwelle liegt.
        release_event_view: Die View ``release_event`` ist benutzbar.
        detail: Menschenlesbare Begruendung (Admin-UI, Phase 25).
    """

    reachable: bool
    schema_ok: bool = False
    missing: tuple[str, ...] = ()
    health: MbHealth | None = None
    stale: MbStale | None = None
    release_event_view: bool = True
    detail: str = ""


@dataclass(slots=True)
class _CheckState:
    """Was der Client sich vom Selfcheck merkt."""

    done: bool = False
    schema_ok: bool = False
    missing: tuple[str, ...] = field(default=())
    release_event_view: bool = True


class MbClient:
    """Read-only-Zugang zur MusicBrainz-Postgres.

    Args:
        dsn: ``mb.dsn`` aus der Konfiguration (URL oder Key-Value-String).
        pool_max_size: Obergrenze des Pools.
        pool_timeout_s: Wartezeit auf eine freie Verbindung.
        row_limit: Zeilenobergrenze von
            :func:`~shared.mb.queries.recording_release_rows`.
        breaker: Eigener Breaker (Tests); sonst entsteht einer mit den
            Konstanten aus :mod:`shared.mb.breaker`.
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool_max_size: int = DEFAULT_POOL_MAX_SIZE,
        pool_timeout_s: float = DEFAULT_POOL_TIMEOUT_S,
        row_limit: int = DEFAULT_ROW_LIMIT,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.row_limit = row_limit
        self.breaker = breaker or CircuitBreaker()
        self._check = _CheckState()
        self._pool = ConnectionPool(
            dsn,
            # min_size 0: ein Start ohne erreichbare MB-Datenbank ist der
            # Normalfall einer frisch aufgesetzten Instanz und darf den
            # API-Dienst nicht aufhalten (Invariante §8.7).
            min_size=0,
            max_size=pool_max_size,
            timeout=pool_timeout_s,
            kwargs={
                "connect_timeout": CONNECT_TIMEOUT_S,
                "options": connection_options(),
                "autocommit": False,
            },
            # Pre-Ping: eine vom Server geschlossene Verbindung soll nicht
            # als Abfragefehler auffallen, sondern still ersetzt werden.
            check=ConnectionPool.check_connection,
            name="musicbrainz",
            open=False,
        )

    @classmethod
    def from_config(cls, config: Config, **kwargs: object) -> Self | None:
        """Baut den Client aus ``mb.dsn`` — oder ``None``, wenn er leer ist.

        Leerer DSN heisst „keine MusicBrainz-Anbindung" (Config-Grundregel
        „leerer String = aus"): der Lookup antwortet dann dauerhaft ohne
        Metadaten, ohne dass irgendwo ein Fehler entsteht.
        """
        if not config.mb.configured:
            return None
        return cls(config.mb.dsn.get_secret_value(), **kwargs)  # type: ignore[arg-type]

    # --- Lebenszyklus ------------------------------------------------------

    def open(self) -> None:
        """Oeffnet den Pool, **ohne** auf eine Verbindung zu warten."""
        self._pool.open(wait=False)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Sitzung -----------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[psycopg.Connection]:
        """Eine Read-only-Transaktion auf dem Spiegel.

        Yields:
            Offene Verbindung; alle Abfragen darin sehen denselben Snapshot.

        Raises:
            MbUnavailable: Keine Verbindung — auch, wenn der Breaker sperrt.
            MbSchemaMismatch: Relation/Spalte fehlt oder Rechte fehlen.
            MbQueryError: Alles andere.
        """
        if not self.breaker.allows():
            raise MbUnavailable(
                "MusicBrainz-Verbindung ist gesperrt (Circuit-Breaker offen nach "
                f"{self.breaker.threshold} Fehlern)"
            )
        try:
            with self._pool.connection() as connection:
                yield connection
        except MbError as error:
            # Schon uebersetzt (aus dem Rumpf der Sitzung) — nur noch der
            # Breaker; ein zweiter Durchlauf durch translate_error wuerde
            # daraus faelschlich einen MbQueryError machen.
            self._account(error)
            raise
        except Exception as exc:
            error = translate_error(exc)
            self._account(error)
            raise error from exc
        self.breaker.record_success()

    def _account(self, error: MbError) -> None:
        """Fehler dem Breaker melden — ausser Programmfehlern.

        Ein :class:`MbQueryError` ist kein Grund, den Nachbardienst
        abzuschalten: der Breaker zaehlt Erreichbarkeit, nicht Korrektheit.
        Ein Schema-Mismatch dagegen ist ein Dauerzustand — dort ist
        schnelles Scheitern genau richtig.
        """
        if isinstance(error, MbQueryError):
            return
        if self.breaker.record_failure():
            _LOG.warning(
                "MusicBrainz-Verbindung gesperrt",
                extra={"mb_error": str(error), "mb_breaker_trips": self.breaker.trips},
            )

    # --- Abfragen ----------------------------------------------------------

    def health(self) -> MbHealth:
        """``replication_control`` lesen (Schema-Sequenz, Replikationsalter)."""
        with self.session() as connection:
            try:
                return queries.mb_health(connection)
            except LookupError as exc:
                raise MbSchemaMismatch(["replication_control.<leer>"]) from exc
            except psycopg.Error as exc:
                raise translate_error(exc) from exc

    def lookup_metadata(
        self,
        mbids: Sequence[str],
        *,
        load_releases: bool = False,
        load_release_groups: bool = False,
        only_ids: bool = False,
    ) -> MetadataResult:
        """Metadaten zu Recording-MBIDs (Choreografie: :mod:`shared.mb.metadata`)."""
        with self.session() as connection:
            self._ensure_checked(connection)
            if not self._check.schema_ok:
                # Gar nicht erst fragen: die Abfrage wuerde an derselben
                # fehlenden Spalte scheitern, die der Selfcheck gemeldet hat.
                raise MbSchemaMismatch(list(self._check.missing))
            try:
                return lookup_metadata(
                    connection,
                    mbids,
                    load_releases=load_releases,
                    load_release_groups=load_release_groups,
                    only_ids=only_ids,
                    row_limit=self.row_limit,
                    release_event_view=self._check.release_event_view,
                )
            except psycopg.Error as exc:
                raise translate_error(exc) from exc

    # --- Selfcheck ---------------------------------------------------------

    def startup_check(self) -> MbStatus:
        """Schema-Guard und Staleness beim Start — wirft **nie**.

        Ein Mismatch fuehrt zu einem lauten Log und zu einem degradierten
        Start, nicht zu einem Absturz: eine Instanz ohne Metadaten ist
        brauchbar, eine, die nicht startet, nicht.
        """
        try:
            with self.session() as connection:
                status = self._check_connection(connection)
        except (MbUnavailable, MbSchemaMismatch, MbQueryError) as exc:
            _LOG.warning(
                "MusicBrainz-Spiegel beim Start nicht pruefbar — Lookups antworten "
                "vorerst ohne Metadaten",
                extra={"mb_error": str(exc)},
            )
            return MbStatus(reachable=False, detail=str(exc))
        self._log_status(status)
        return status

    def check_connection(self) -> MbStatus:
        """Verbindungstest fuer Wächter und Admin-UI (Phase 25).

        Wie :meth:`startup_check`, aber ohne Nebenwirkung auf den
        gemerkten Selfcheck-Zustand des Clients: der Test soll den Betrieb
        nicht veraendern.
        """
        try:
            with self.session() as connection:
                return self._check_connection(connection, remember=False)
        except (MbUnavailable, MbSchemaMismatch, MbQueryError) as exc:
            return MbStatus(reachable=False, detail=str(exc))

    def _check_connection(
        self, connection: psycopg.Connection, *, remember: bool = True
    ) -> MbStatus:
        try:
            health = queries.mb_health(connection)
            found = queries.mb_selfcheck(connection)
        except LookupError as exc:
            raise MbSchemaMismatch(["replication_control.<leer>"]) from exc
        except psycopg.Error as exc:
            raise translate_error(exc) from exc

        missing = tuple(_missing_columns(found))
        has_view = queries.RELEASE_EVENT_VIEW in found
        stale = _staleness(health.last_replication_date)
        if remember:
            self._check = _CheckState(
                done=True,
                schema_ok=not missing,
                missing=missing,
                release_event_view=has_view,
            )
        return MbStatus(
            reachable=True,
            schema_ok=not missing,
            missing=missing,
            health=health,
            stale=stale,
            release_event_view=has_view,
            detail="Schema vollstaendig" if not missing else f"{len(missing)} Spalten fehlen",
        )

    def _ensure_checked(self, connection: psycopg.Connection) -> None:
        """Selfcheck nachholen, falls er beim Start nicht laufen konnte.

        Er entscheidet, ob :func:`~shared.mb.queries.release_events` die
        View oder den Rueckfallweg nimmt — probiert wird nie, ein
        Fehlversuch wuerde die laufende Transaktion abbrechen.
        """
        if self._check.done:
            return
        status = self._check_connection(connection)
        self._log_status(status)

    def _log_status(self, status: MbStatus) -> None:
        health = status.health
        if not status.schema_ok:
            _LOG.error(
                "MusicBrainz-Schema weicht ab — Lookups antworten ohne Metadaten",
                extra={"mb_missing_columns": list(status.missing)[:20]},
            )
        if not status.release_event_view:
            _LOG.warning(
                "View %s.%s fehlt — Rueckfall auf die beiden Basistabellen",
                queries.SCHEMA,
                queries.RELEASE_EVENT_VIEW,
            )
        if health is None:
            return
        if health.schema_sequence != EXPECTED_SCHEMA_SEQUENCE:
            _LOG.warning(
                "MusicBrainz-Schema-Sequenz weicht von der verifizierten ab",
                extra={
                    "mb_schema_sequence": health.schema_sequence,
                    "mb_expected_schema_sequence": EXPECTED_SCHEMA_SEQUENCE,
                },
            )
        stale = status.stale
        if stale is None:
            _LOG.info(
                "MusicBrainz-Spiegel bereit",
                extra={
                    "mb_schema_sequence": health.schema_sequence,
                    "mb_replication_sequence": health.replication_sequence,
                    "mb_last_replication": _isoformat(health.last_replication_date),
                },
            )
        elif stale.critical:
            _LOG.error(str(stale), extra={"mb_age_hours": round(stale.age_hours, 1)})
        else:
            _LOG.warning(str(stale), extra={"mb_age_hours": round(stale.age_hours, 1)})


def _missing_columns(found: dict[str, frozenset[str]]) -> list[str]:
    """Diff Erwartung -> Ist, als ``"relation.spalte"``.

    Die View ``release_event`` fehlt hier bewusst: fehlt sie, gibt es den
    Rueckfallweg ueber die beiden Basistabellen, und der steht in der
    Erwartungsliste.
    """
    missing: list[str] = []
    for relation, columns in queries.EXPECTED_COLUMNS.items():
        present = found.get(relation)
        if present is None:
            missing.extend(f"{relation}.{column}" for column in sorted(columns))
            continue
        missing.extend(f"{relation}.{column}" for column in sorted(columns - present))
    return sorted(missing)


def _staleness(last_replication: datetime | None) -> MbStale | None:
    """Alter der letzten Replikation gegen die Schwellen halten."""
    if last_replication is None:
        return MbStale(float("inf"), critical=True)
    reference = last_replication
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age_hours = (datetime.now(UTC) - reference).total_seconds() / 3600
    if age_hours >= STALE_CRIT_HOURS:
        return MbStale(age_hours, critical=True)
    if age_hours >= STALE_WARN_HOURS:
        return MbStale(age_hours, critical=False)
    return None


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
