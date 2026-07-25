"""Bootstrap-Bulk-Modus: unsichere PG-Einstellungen auf Zeit (§5.2 Regel 6).

Der Erst-Import spielt 414 GB gz in eine leere Datenbank. Import-Regel 6
erlaubt dafuer zweierlei: Sekundaerindizes erst **nach** dem Massenimport
(das macht der Migrations-Runner ueber seine Gruppen ``core``/``indexes``)
und „unsichere PG-Bulk-Einstellungen nur waehrenddessen, danach
zuruecknehmen" — das ist dieses Modul.

**Nur Sitzungseinstellungen, nie ``ALTER SYSTEM``.** Alles hier wird per
``SET`` in *einer* Verbindung gesetzt und per ``RESET`` wieder
zurueckgenommen. Damit gilt die harte Zusicherung, um die es geht: selbst
wenn der Prozess mitten im Bootstrap stirbt, ueberleben die Einstellungen
ihn nicht — mit der Sitzung sind sie weg. Eine persistente Aenderung
(``ALTER SYSTEM``/``ALTER DATABASE``) haette genau dieses Netz nicht und
koennte eine Produktionsdatenbank dauerhaft unsicher zuruecklassen.

**Was gesetzt wird** (:data:`BULK_SETTINGS`):

``synchronous_commit = off``
    Der Commit wartet nicht auf den WAL-Flush. Das ist der einzige wirklich
    grosse Hebel fuer viele kleine Transaktionen auf Spindeln — und er ist
    hier vertretbar, weil er **keine Korruption** erlaubt: bei einem
    Betriebssystem- oder Stromausfall gehen hoechstens die zuletzt
    committeten Transaktionen verloren. Genau dagegen ist der Importer
    gebaut: eine Tagesdatei ist eine Transaktion **inklusive**
    ``import_state`` (§8.3). Was verloren geht, geht vollstaendig verloren
    und wird beim naechsten Lauf einfach noch einmal eingespielt (§8.4).

**Was bewusst NICHT gesetzt wird.** ``fsync=off`` und
``full_page_writes=off`` sind nicht sitzungsweit setzbar und wuerden bei
einem Absturz ein **korruptes Cluster** hinterlassen, das kein Resume
repariert; ``ALTER SYSTEM`` scheidet aus dem oben genannten Grund aus. Der
Gewinn waere gering, das Risiko unbegrenzt — beides bleibt draussen. Wer den
letzten Prozentpunkt will, stellt es bewusst am Postgres-Container ein und
nimmt es selbst wieder zurueck.

**Getrennt davon: der Indexbau.** :data:`INDEX_BUILD_SETTINGS` erhoeht
``maintenance_work_mem`` fuer die Migrationsgruppe ``indexes`` nach dem
Massenimport. Das ist keine unsichere Einstellung, sondern eine
Speichergrenze — sie steht hier, weil sie zum selben Ablauf gehoert.

Beispiel::

    with psycopg.connect(dsn, autocommit=True) as conn:
        with bulk_session(conn):
            ...  # Massenimport
        # ab hier steht synchronous_commit garantiert wieder auf dem alten Wert
        with session_settings(conn, INDEX_BUILD_SETTINGS):
            migrations.apply(conn, groups=["indexes"])
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Final

import psycopg
from psycopg import Connection, sql
from psycopg import pq as psycopg_pq

from acoustid_importer.errors import BulkModeError

__all__ = [
    "BULK_SETTINGS",
    "INDEX_BUILD_SETTINGS",
    "bulk_session",
    "current_settings",
    "flush_wal",
    "session_settings",
]

_LOG = logging.getLogger(__name__)

#: Unsichere Einstellungen des Massenimports — nur waehrend des Bootstraps.
BULK_SETTINGS: Final[Mapping[str, str]] = {"synchronous_commit": "off"}

#: Speichergrenze fuer den Indexbau nach dem Massenimport (keine unsichere
#: Einstellung; sie kostet nur RAM in genau dieser Sitzung).
INDEX_BUILD_SETTINGS: Final[Mapping[str, str]] = {"maintenance_work_mem": "1GB"}


@contextmanager
def session_settings(
    conn: Connection,
    settings: Mapping[str, str],
    *,
    enabled: bool = True,
) -> Iterator[Mapping[str, str]]:
    """Setzt Sitzungseinstellungen und nimmt sie garantiert zurueck.

    Args:
        conn: Verbindung mit ``autocommit=True`` und ohne offene Transaktion.
            Beides ist Bedingung, nicht Bequemlichkeit: ein ``SET`` innerhalb
            einer Transaktion, die spaeter zurueckrollt, ist wieder weg — der
            Massenimport liefe dann still ohne die Einstellung. Und ein
            offenes ``BEGIN`` wuerde ausserdem die „eine Datei = eine
            Transaktion"-Regel des Imports brechen (§8.3).
        settings: Name -> Wert, z. B. :data:`BULK_SETTINGS`.
        enabled: ``False`` macht den Block zum No-Op (Update-Laeufe).

    Yields:
        Die vorherigen Werte (Name -> Wert), wie ``SHOW`` sie meldet.

    Raises:
        BulkModeError: Die Verbindung passt nicht, eine Einstellung ist
            unbekannt, oder das Zuruecknehmen ist gescheitert, obwohl die
            Verbindung noch steht.
    """
    if not enabled or not settings:
        yield {}
        return

    _require_session_scope(conn)
    previous = current_settings(conn, settings)
    _apply(conn, settings, previous)
    _LOG.info(
        "Bulk-Einstellungen gesetzt (nur fuer diese Sitzung)",
        extra={"pg_settings": dict(settings), "pg_settings_before": dict(previous)},
    )
    try:
        yield previous
    finally:
        _restore(conn, previous)


def bulk_session(
    conn: Connection, *, enabled: bool = True
) -> AbstractContextManager[Mapping[str, str]]:
    """:func:`session_settings` mit :data:`BULK_SETTINGS` — der Bootstrap-Fall."""
    return session_settings(conn, BULK_SETTINGS, enabled=enabled)


def current_settings(conn: Connection, settings: Mapping[str, str]) -> dict[str, str]:
    """Liest die aktuellen Werte der genannten Einstellungen (``SHOW``).

    Raises:
        BulkModeError: Postgres kennt eine der Einstellungen nicht.
    """
    values: dict[str, str] = {}
    for name in settings:
        try:
            row = conn.execute(sql.SQL("SHOW {}").format(sql.Identifier(name))).fetchone()
        except psycopg.Error as error:
            raise BulkModeError(
                f"Einstellung {name!r} ist dieser Postgres unbekannt: {error}"
            ) from error
        values[name] = "" if row is None else str(row[0])
    return values


def flush_wal(conn: Connection) -> bool:
    """Erzwingt einen Checkpoint — der Abschluss des Bulk-Modus.

    Nach ``synchronous_commit = off`` koennen die zuletzt bestaetigten
    Transaktionen noch im Puffer stehen. Ein ``CHECKPOINT`` macht sie
    haltbar; der Bootstrap steht danach auf sicherem Grund, ohne dass ein
    spaeterer Absturz Arbeit von Stunden kostet.

    Returns:
        ``True``, wenn der Checkpoint lief. ``False``, wenn die Rolle ihn
        nicht ausfuehren darf — dann ist es kein Fehler, sondern nur ein
        entgangener Komfort: verloren gehen kann hoechstens der Schwanz des
        Laufs, und den holt das Resume ohnehin.
    """
    try:
        conn.execute("CHECKPOINT")
    except psycopg.Error as error:
        _LOG.warning(
            "CHECKPOINT nicht moeglich — der Lauf bleibt trotzdem resumierbar",
            extra={"reason": str(error).strip()},
        )
        return False
    _LOG.info("CHECKPOINT nach dem Massenimport ausgefuehrt")
    return True


# --- Innenleben -------------------------------------------------------------


def _set_one(conn: Connection, name: str, value: str) -> None:
    conn.execute(sql.SQL("SET {} = {}").format(sql.Identifier(name), sql.Literal(value)))


def _apply(conn: Connection, settings: Mapping[str, str], previous: Mapping[str, str]) -> None:
    """Setzt alle Einstellungen — oder keine."""
    done: list[str] = []
    for name, value in settings.items():
        try:
            _set_one(conn, name, value)
        except psycopg.Error as error:
            # Ein halb gesetzter Bulk-Modus waere genau das, was nicht
            # passieren darf: sofort zurueck auf die alten Werte.
            _restore(conn, {key: previous[key] for key in done}, quiet=True)
            raise BulkModeError(
                f"Einstellung {name} = {value!r} liess sich nicht setzen: {error}"
            ) from error
        done.append(name)


def _restore(conn: Connection, previous: Mapping[str, str], *, quiet: bool = False) -> None:
    """Setzt die alten Werte zurueck und prueft das Ergebnis nach.

    Bewusst ``SET <alter Wert>`` statt ``RESET``: ``RESET`` stellt den
    Startwert der *Konfiguration* wieder her, nicht den Wert, den die Sitzung
    beim Betreten hatte. Nur wenn beides scheitert, wird ``RESET`` als
    Notnagel versucht.
    """
    if not previous:
        return
    if conn.closed:
        # Die Sitzung ist weg — und mit ihr jede Sitzungseinstellung. Genau
        # deshalb wird hier per SET gearbeitet und nicht per ALTER SYSTEM.
        _LOG.info("Verbindung geschlossen — Bulk-Einstellungen sind mit der Sitzung erloschen")
        return

    failed: dict[str, str] = {}
    for name, value in previous.items():
        try:
            _set_one(conn, name, value)
        except psycopg.Error as first:
            try:
                conn.execute(sql.SQL("RESET {}").format(sql.Identifier(name)))
            except psycopg.Error as second:
                failed[name] = f"{str(first).strip()} / {str(second).strip()}"
    if failed:
        raise BulkModeError(
            "Bulk-Einstellungen liessen sich nicht zuruecknehmen, obwohl die Verbindung "
            "steht: " + "; ".join(f"{name}: {reason}" for name, reason in sorted(failed.items()))
        )

    restored = current_settings(conn, previous)
    drifted = {name: value for name, value in restored.items() if value != previous.get(name)}
    if drifted:
        _LOG.warning(
            "Einstellungen stehen nach dem Zuruecknehmen auf einem anderen Wert als vorher",
            extra={"pg_settings_now": drifted, "pg_settings_before": dict(previous)},
        )
    elif not quiet:
        _LOG.info("Bulk-Einstellungen zurueckgenommen", extra={"pg_settings": dict(restored)})


def _require_session_scope(conn: Connection) -> None:
    """Sichert zu, dass ein ``SET`` wirklich die Sitzung meint."""
    if not conn.autocommit:
        raise BulkModeError(
            "Der Bulk-Modus braucht eine Verbindung mit autocommit=True. Ohne sie "
            "liefe das SET in einer Transaktion des Aufrufers und waere nach deren "
            "Rollback wieder weg — der Massenimport liefe dann still ohne die "
            "Einstellung."
        )
    status = conn.pgconn.transaction_status
    if status != psycopg_pq.TransactionStatus.IDLE:
        raise BulkModeError(
            "Die Verbindung hat eine offene Transaktion "
            f"(Status {psycopg_pq.TransactionStatus(status).name}); Sitzungs"
            "einstellungen brauchen eine freie Verbindung."
        )
