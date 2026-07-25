"""MusicBrainz-Query-Schicht (Phase 10, ARCHITECTURE §5.4).

Die Metadaten einer AcoustID kommen nicht aus unserem Bestand, sondern aus
der **lokalen MusicBrainz-Spiegel-Datenbank** des Betreibers (Zugang:
``mb.dsn``). Das ist ein fremder Dienst — read-only, jederzeit weg,
jaehrliches Schema-Update — und genau darum liegt der Zugriff hier hinter
einer schmalen Fassade:

* :mod:`shared.mb.queries` — die **einzige** Datei mit MB-Tabellennamen:
  elf Batch-Abfragen mit expliziten Spaltenlisten, schema-qualifiziert.
* :mod:`shared.mb.metadata` — die Choreografie daraus (``lookup_metadata``),
  inklusive Redirect-Aufloesung fuer zusammengefuehrte Aufnahmen.
* :mod:`shared.mb.client` — Pool, Verbindungsoptionen, Selfcheck,
  Staleness und die Uebersetzung jedes Treiberfehlers.
* :mod:`shared.mb.breaker` — Circuit-Breaker, damit ein toter Spiegel nicht
  jede Anfrage ausbremst.
* :mod:`shared.mb.errors` — vier Fehlerbilder, vier Reaktionen.

Typischer Ablauf im API-Dienst::

    client = MbClient.from_config(config)  # None, wenn mb.dsn leer ist
    if client is not None:
        client.open()
        client.startup_check()  # wirft nie
        result = client.lookup_metadata(mbids, load_releases=True)

Bewusst **nicht** in :mod:`shared` re-exportiert (gleiche Regel wie
:mod:`shared.db` und :mod:`shared.fpindex`): der Waechter braucht in
Phase 25 nur den Verbindungstest und soll den Rest nicht mitladen.
"""

from __future__ import annotations

from shared.mb.breaker import CircuitBreaker
from shared.mb.client import (
    CONNECT_TIMEOUT_S,
    STALE_CRIT_HOURS,
    STALE_WARN_HOURS,
    STATEMENT_TIMEOUT_MS,
    MbClient,
    MbStatus,
)
from shared.mb.errors import MbError, MbQueryError, MbSchemaMismatch, MbStale, MbUnavailable
from shared.mb.metadata import MetadataResult, duration_seconds, lookup_metadata
from shared.mb.queries import DEFAULT_ROW_LIMIT, EXPECTED_COLUMNS, SCHEMA, MbHealth

__all__ = [
    "CONNECT_TIMEOUT_S",
    "DEFAULT_ROW_LIMIT",
    "EXPECTED_COLUMNS",
    "SCHEMA",
    "STALE_CRIT_HOURS",
    "STALE_WARN_HOURS",
    "STATEMENT_TIMEOUT_MS",
    "CircuitBreaker",
    "MbClient",
    "MbError",
    "MbHealth",
    "MbQueryError",
    "MbSchemaMismatch",
    "MbStale",
    "MbStatus",
    "MbUnavailable",
    "MetadataResult",
    "duration_seconds",
    "lookup_metadata",
]
