"""Index-Feed: neue Fingerprints in den acoustid-index (ARCHITECTURE §5.3).

Nach dem DB-Import stehen die Fingerprint-Vektoren in Postgres, aber der
Suchindex kennt sie noch nicht. Dieses Modul schliesst die Luecke:

1. **Arbeitsvorrat** ist der Partialindex ``fingerprint_idx_unindexed``
   (``WHERE indexed_at IS NULL``) — abgearbeitet **aufsteigend nach
   ``fingerprint.id``**. Die Reihenfolge ist keine Kosmetik: der Index wird
   dadurch rund 15 % kleiner (§5.3).
2. **Query-Extrakt statt Vollvektor.** In den Index geht nie der volle
   Vektor, sondern das Ergebnis von
   :func:`shared.fpindex.extract_query` (Offset 80, ``acoustid.index.query_hashes``
   Hashes, 28-Bit-Maske, Silence-Hash gefiltert, unsigned). Vollvektoren im
   Index bedeuten dokumentiert ~50 s statt ~50 ms je Suche.
3. **Batches à :data:`DEFAULT_BATCH_SIZE`** per ``_update`` — der Server
   schreibt einen Batch atomar (ein Segment, ein fsync, eine neue Version).
4. **Erst der Index, dann die Datenbank.** ``indexed_at`` wird gesetzt,
   *nachdem* der Batch angenommen wurde. Bricht der Lauf dazwischen ab,
   werden dieselben Dokumente beim naechsten Mal noch einmal geschickt —
   das ist folgenlos (gleiche ID, gleiche Hashes). Andersherum waere es ein
   stiller Datenverlust: als indexiert markierte Fingerprints, die der
   Index nie gesehen hat, tauchen in keinem Lookup mehr auf.

**Zeilen ohne Vektor** (nur ``track_fingerprint`` war da) bleiben im
Arbeitsvorrat: sie bekommen kein ``indexed_at``, weil ihr Vektor noch
kommen kann. Damit sie den Lauf nicht blockieren, wird nicht nach
``indexed_at IS NULL`` allein geblaettert, sondern zusaetzlich nach
``id > <zuletzt gesehen>``.

**Zeilen mit leerem Query-Extrakt** (Vektor nur aus Stille oder leer)
bekommen dagegen ``indexed_at`` gesetzt, obwohl nichts uebergeben wird:
Es gibt nichts zu indexieren, und ohne die Markierung laege die Zeile bei
jedem kuenftigen Lauf wieder im Arbeitsvorrat. Sie werden gezaehlt und
gemeldet.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Final

from psycopg import Connection

from acoustid_importer.errors import IndexFeedError
from shared.fpindex import (
    RECOMMENDED_BATCH_SIZE,
    FpIndexClient,
    FpIndexVersionMismatchError,
    Insert,
    extract_query,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "METADATA_LAST_ID",
    "IndexFeedReport",
    "feed_index",
]

_LOG = logging.getLogger(__name__)

#: Dokumente je ``_update``-Batch (ARCHITECTURE §5.3; identisch mit der
#: Empfehlung des offiziellen Consumers).
DEFAULT_BATCH_SIZE: Final = RECOMMENDED_BATCH_SIZE

#: Schluessel im Metadaten-Bereich des Index: zuletzt uebergebene
#: ``fingerprint.id``. Rein informativ (fuer Diagnose und Admin-UI) — die
#: verbindliche Buchfuehrung ist ``fingerprint.indexed_at`` in Postgres.
METADATA_LAST_ID: Final = "last_fp_id"

_SELECT_FIRST: Final = """
SELECT id, fingerprint
  FROM fingerprint
 WHERE indexed_at IS NULL
 ORDER BY id
 LIMIT %s
"""

_SELECT_AFTER: Final = """
SELECT id, fingerprint
  FROM fingerprint
 WHERE indexed_at IS NULL AND id > %s
 ORDER BY id
 LIMIT %s
"""

#: Der Cast ist Absicht: ohne ihn muesste Postgres den Typ des Arrays aus
#: dem Vergleich raten, und das ist je nach Treiberversion eine Fehlerquelle.
_MARK_INDEXED: Final = "UPDATE fingerprint SET indexed_at = now() WHERE id = ANY(%s::integer[])"


@dataclass(frozen=True, slots=True)
class IndexFeedReport:
    """Ergebnis eines :func:`feed_index`-Laufs."""

    #: An den Index uebergebene Dokumente.
    documents: int
    #: Zahl der ``_update``-Batches.
    batches: int
    #: Betrachtete Zeilen des Arbeitsvorrats.
    scanned: int
    #: Zeilen ohne Vektor — bleiben im Arbeitsvorrat (§5.2, unvollstaendige Zeilen).
    incomplete: int
    #: Zeilen, deren Vektor keinen einzigen Query-Hash ergibt.
    empty_queries: int
    #: Hoechste uebergebene ``fingerprint.id`` (``None``, wenn nichts lief).
    last_id: int | None
    #: Version des Index nach dem letzten Batch.
    version: int | None
    duration_s: float
    #: Der Arbeitsvorrat wurde leer gelesen. ``False`` heisst: ``max_rows``
    #: hat vorher gestoppt (ob danach noch etwas uebrig ist, weiss der Lauf
    #: dann nicht — er hat nicht mehr nachgesehen).
    exhausted: bool = True


def feed_index(
    conn: Connection,
    client: FpIndexClient,
    *,
    max_hashes: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rows: int | None = None,
    ensure: bool = True,
    guard_version: bool = True,
) -> IndexFeedReport:
    """Uebergibt alle noch nicht indexierten Fingerprints an den Index.

    Args:
        conn: Verbindung zur AcoustID-Postgres. Jeder Batch bekommt seine
            eigene kurze Transaktion; ueber den HTTP-Aufruf hinweg wird
            bewusst keine offen gehalten. Gemeint ist eine freie Verbindung —
            innerhalb einer fremden Transaktion werden die Batch-Commits zu
            Savepoints. Schlimm ist das nicht (ein spaeterer Lauf schickt
            dieselben Dokumente noch einmal, gleiche ID und gleiche Hashes),
            nur eben nutzlos.
        client: Index-Client (:class:`shared.fpindex.FpIndexClient`).
        max_hashes: ``acoustid.index.query_hashes`` aus der Konfiguration (Default
            120). **Muss** derselbe Wert sein wie beim spaeteren Suchen —
            eine Aenderung erfordert einen Index-Neuaufbau (DECISIONS).
        batch_size: Dokumente je ``_update``.
        max_rows: Hoechstens so viele Zeilen des Arbeitsvorrats betrachten
            (Probelauf, Tests). ``None`` heisst: bis er leer ist.
        ensure: Den Index vorher anlegen. Der Importer ist laut Compose der
            Dienst, der ihn anlegt (der Healthcheck wird erst danach gruen).
        guard_version: Jeden Batch mit ``expected_version`` absichern. Faellt
            damit auf, wenn ein zweiter Schreiber am selben Index arbeitet —
            der Feed bricht dann laut ab, statt sich mit ihm zu verzahnen.

    Returns:
        :class:`IndexFeedReport` mit Zahlen fuer das Lauf-Protokoll.

    Raises:
        IndexFeedError: Ein anderer Schreiber hat den Index veraendert.
        ValueError: ``batch_size`` oder ``max_rows`` sind kleiner als 1.
        shared.fpindex.FpIndexError: Der Index ist nicht erreichbar oder
            lehnt einen Batch ab.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size muss mindestens 1 sein, war {batch_size}")
    if max_rows is not None and max_rows < 1:
        raise ValueError(f"max_rows muss mindestens 1 sein, war {max_rows}")

    started = time.monotonic()
    version = client.ensure_index().version if ensure else client.index_info().version

    documents = batches = scanned = incomplete = empty_queries = 0
    last_id: int | None = None
    after: int | None = None
    exhausted = True

    while True:
        wanted = batch_size if max_rows is None else min(batch_size, max_rows - scanned)
        if wanted < 1:
            exhausted = False
            break

        with conn.transaction():
            if after is None:
                rows = conn.execute(_SELECT_FIRST, (wanted,)).fetchall()
            else:
                rows = conn.execute(_SELECT_AFTER, (after, wanted)).fetchall()
        if not rows:
            break

        scanned += len(rows)
        after = rows[-1][0]

        changes: list[Insert] = []
        handled: list[int] = []
        for fp_id, vector in rows:
            if vector is None:
                # Nur `track_fingerprint` war bisher da — der Vektor kommt
                # noch, die Zeile bleibt im Arbeitsvorrat.
                incomplete += 1
                continue
            query = extract_query(vector, max_hashes=max_hashes)
            handled.append(fp_id)
            if not query:
                empty_queries += 1
                continue
            changes.append(Insert(doc_id=fp_id, hashes=query))

        if changes:
            version = _write_batch(client, changes, version, guard_version=guard_version)
            documents += len(changes)
            batches += 1
            last_id = changes[-1].doc_id

        if handled:
            with conn.transaction():
                conn.execute(_MARK_INDEXED, (handled,))

        if len(rows) < wanted:
            # Der Arbeitsvorrat gab weniger her als angefragt — er ist leer.
            break

    duration_s = time.monotonic() - started
    _LOG.info(
        "Index-Feed abgeschlossen",
        extra={
            "documents": documents,
            "batches": batches,
            "scanned": scanned,
            "incomplete": incomplete,
            "empty_queries": empty_queries,
            "last_id": last_id,
            "index_version": version,
            "duration_s": round(duration_s, 3),
        },
    )
    if incomplete:
        _LOG.warning(
            "Fingerprint-Zeilen ohne Vektor uebersprungen — sie bleiben im Arbeitsvorrat",
            extra={"incomplete": incomplete},
        )
    if empty_queries:
        _LOG.warning(
            "Fingerprint-Vektoren ohne indexierbare Hashes — als erledigt vermerkt",
            extra={"empty_queries": empty_queries},
        )
    return IndexFeedReport(
        documents=documents,
        batches=batches,
        scanned=scanned,
        incomplete=incomplete,
        empty_queries=empty_queries,
        last_id=last_id,
        version=version,
        duration_s=duration_s,
        exhausted=exhausted,
    )


def _write_batch(
    client: FpIndexClient,
    changes: list[Insert],
    version: int,
    *,
    guard_version: bool,
) -> int:
    """Ein ``_update``; gibt die neue Indexversion zurueck."""
    try:
        return client.update(
            changes,
            metadata={METADATA_LAST_ID: str(changes[-1].doc_id)},
            expected_version=version if guard_version else None,
        )
    except FpIndexVersionMismatchError as error:
        raise IndexFeedError(
            f"Der Index steht nicht mehr auf Version {version} — ein zweiter Schreiber "
            "hat ihn waehrend des Feeds veraendert. Der Batch wurde nicht geschrieben; "
            f"bitte nur einen Importer gleichzeitig laufen lassen ({error})"
        ) from error
