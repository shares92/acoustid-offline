"""Lesezugriffe der API auf die AcoustID-Postgres (ARCHITECTURE §5.2).

Alles, was der Lookup aus der eigenen Datenbank braucht, steht in diesem
Modul — sechs Abfragen, alle rein lesend:

1. :func:`load_candidates` holt zu den Kandidaten-IDs des Suchindex die
   **Vollvektoren**. Nur sie erlauben den echten AcoustID-Score; der Index
   kennt je Fingerprint nur den Query-Extrakt. Der Laengenfilter
   (``length`` ± ``maxdurationdiff``) sitzt bewusst schon hier: er wirft die
   meisten Kandidaten weg, bevor irgendetwas gerechnet wird.
2. :func:`resolve_tracks` uebersetzt ``fingerprint.track_id`` in die
   oeffentliche AcoustID (``track.gid``) und folgt dabei der
   **Merge-Verkettung** ueber ``track.new_id``.
3. :func:`resolve_track_gid` ist der Weg fuer den Parameter ``trackid``:
   dieselbe Verkettung, nur von einer GID aus.

Ab Phase 10 kommen die drei Abfragen des ``meta``-Parameters dazu, die
**nicht** aus MusicBrainz stammen:

4. :func:`lookup_mbids` — die Recording-MBIDs einer AcoustID samt
   ``submission_count``. Letzterer ist der ``sources``-Wert der Antwort;
   Picard gewichtet damit sein Ranking. Er kommt aus unserem eigenen
   Delta-Bestand, MusicBrainz kennt ihn gar nicht.
5. :func:`lookup_meta_ids` und 6. :func:`lookup_meta` — der
   ``usermeta``-Rueckfall: die von Nutzern eingereichten Textmetadaten aus
   ``track_meta``/``meta``. Sie greifen nur, wenn MusicBrainz zu **keiner**
   MBID etwas liefert (Original-Verhalten).

**Zur Merge-Verkettung.** Der Original-Server verbindet Fingerprint und
Track direkt (``JOIN track t ON f.track_id = t.id``) und folgt ``new_id``
nur beim ``trackid``-Nachschlagen. Das kann er sich leisten, weil er beim
Zusammenfuehren zweier Tracks auch die Fingerprints umhaengt. Wir bauen den
Bestand dagegen aus den Tagesdeltas nach — dort bleibt ``fingerprint.track_id``
auf dem alten Track stehen, und nur ``track.new_id`` verraet das Ziel. Ohne
die Verkettung wuerde ein Lookup die **zurueckgezogene** AcoustID liefern
(bewusste Abweichung, DoD Phase 9).

Alle Anweisungen sind Raw-SQL mit expliziten Spaltenlisten; die Vektoren
kommen als ``list[int]`` (signed int32) aus psycopg und werden erst im
Rescoring vorzeichenlos gelesen.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import psycopg

__all__ = [
    "MAX_MERGE_DEPTH",
    "MAX_META_IDS_PER_TRACK",
    "Candidate",
    "MetaRow",
    "load_candidates",
    "lookup_mbids",
    "lookup_meta",
    "lookup_meta_ids",
    "resolve_track_gid",
    "resolve_tracks",
]

_LOG = logging.getLogger(__name__)

#: Wie weit einer Merge-Kette gefolgt wird. Ketten sind in der Praxis ein
#: bis zwei Glieder lang; die Grenze schuetzt vor einem Zyklus in den Daten
#: (der Rekursionsschritt haette sonst kein Abbruchkriterium).
MAX_MERGE_DEPTH: Final = 10

#: ``MAX_META_IDS_PER_TRACK`` des Originals: so viele Nutzer-Metadatensaetze
#: werden je AcoustID hoechstens ausgeliefert (``usermeta``).
MAX_META_IDS_PER_TRACK: Final = 10


@dataclass(frozen=True, slots=True)
class Candidate:
    """Ein Kandidat aus der Datenbank, bereit fuers Rescoring.

    Attributes:
        fingerprint_id: ``fingerprint.id`` — zugleich die Dokument-ID im
            Suchindex und das Sortierkriterium bei Score-Gleichstand.
        track_id: ``fingerprint.track_id``, noch **ohne** Merge-Aufloesung.
        hashes: Vollvektor (signed int32, wie in der Spalte).
    """

    fingerprint_id: int
    track_id: int
    hashes: Sequence[int]


@dataclass(frozen=True, slots=True)
class MetaRow:
    """Ein eingereichter Textmetadatensatz aus der Tabelle ``meta`` (§5.2).

    Alle Felder ausser :attr:`meta_id` sind optional: der Submit-Endpunkt
    des Originals nimmt jede Teilmenge entgegen.
    """

    meta_id: int
    track: str | None
    artist: str | None
    album: str | None
    album_artist: str | None
    track_no: int | None
    disc_no: int | None
    year: int | None


_CANDIDATES_SQL: Final = """
SELECT id, track_id, fingerprint
FROM fingerprint
WHERE id = ANY(%(ids)s)
  AND track_id IS NOT NULL
  AND fingerprint IS NOT NULL
  AND length BETWEEN %(duration)s - %(diff)s AND %(duration)s + %(diff)s
"""

# Rekursiv, weil eine Merge-Kette mehrere Glieder haben kann (a -> b -> c).
# `DISTINCT ON (start_id) … ORDER BY start_id, depth DESC` nimmt je
# Ausgangs-Track das letzte Kettenglied.
_RESOLVE_SQL: Final = """
WITH RECURSIVE chain AS (
    SELECT t.id AS start_id, t.id, t.new_id, t.gid, 0 AS depth
    FROM track t
    WHERE t.id = ANY(%(ids)s)
  UNION ALL
    SELECT c.start_id, t.id, t.new_id, t.gid, c.depth + 1
    FROM chain c
    JOIN track t ON t.id = c.new_id
    WHERE c.new_id IS NOT NULL AND c.depth < %(max_depth)s
)
SELECT DISTINCT ON (start_id) start_id, id, gid
FROM chain
ORDER BY start_id, depth DESC
"""

# `disabled` filtert zurueckgezogene Zuordnungen (§5.1: der Schluessel steht
# nur bei `true` im Dump, der Importer setzt ihn sonst explizit auf `false`).
# Sortierung wie im Original nach MBID — die Reihenfolge landet unveraendert
# in der Antwort.
_MBIDS_SQL: Final = """
SELECT track_id, mbid::text, submission_count
FROM track_mbid
WHERE track_id = ANY(%(ids)s::integer[])
  AND disabled = false
ORDER BY track_id, mbid
"""

# Die besten Metadatensaetze zuerst; `meta_id` bricht den Gleichstand, damit
# die Auswahl bei gleicher Einreichungszahl reproduzierbar bleibt (das
# Original ueberlaesst sie dem Planer).
_META_IDS_SQL: Final = """
SELECT track_id, meta_id
FROM track_meta
WHERE track_id = ANY(%(ids)s::integer[])
ORDER BY track_id, submission_count DESC, meta_id
"""

_META_SQL: Final = """
SELECT id, track, artist, album, album_artist, track_no, disc_no, year
FROM meta
WHERE id = ANY(%(ids)s::integer[])
ORDER BY id
"""

_RESOLVE_GID_SQL: Final = """
WITH RECURSIVE chain AS (
    SELECT t.id, t.new_id, t.gid, 0 AS depth
    FROM track t
    WHERE t.gid = %(gid)s
  UNION ALL
    SELECT t.id, t.new_id, t.gid, c.depth + 1
    FROM chain c
    JOIN track t ON t.id = c.new_id
    WHERE c.new_id IS NOT NULL AND c.depth < %(max_depth)s
)
SELECT id, gid FROM chain ORDER BY depth DESC LIMIT 1
"""


def load_candidates(
    connection: psycopg.Connection,
    fingerprint_ids: Sequence[int],
    *,
    duration: int,
    max_duration_diff: int,
) -> list[Candidate]:
    """Vollvektoren der Kandidaten, gefiltert nach Laenge.

    Args:
        connection: Verbindung zur AcoustID-Postgres (nur lesend).
        fingerprint_ids: Dokument-IDs aus dem Suchindex.
        duration: Vom Client gemeldete Laenge in Sekunden.
        max_duration_diff: Zugelassene Abweichung in Sekunden (``±``).

    Returns:
        Die Kandidaten, die den Laengenfilter ueberstehen — in beliebiger
        Reihenfolge. Zeilen ohne Vektor, ohne Track oder ohne Laenge fallen
        heraus: ohne Vektor gibt es keinen Score, und eine unbekannte Laenge
        ist im Original ebenfalls kein Treffer (``BETWEEN`` auf ``NULL``).
    """
    if not fingerprint_ids:
        return []
    rows = connection.execute(
        _CANDIDATES_SQL,
        {"ids": list(fingerprint_ids), "duration": duration, "diff": max_duration_diff},
    ).fetchall()
    return [Candidate(fingerprint_id=row[0], track_id=row[1], hashes=row[2]) for row in rows]


def resolve_tracks(
    connection: psycopg.Connection, track_ids: Sequence[int]
) -> dict[int, tuple[int, UUID]]:
    """Track-IDs zu ``(Ziel-ID, Ziel-GID)`` aufloesen, Merges eingerechnet.

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        track_ids: IDs aus ``fingerprint.track_id``.

    Returns:
        Abbildung Ausgangs-ID -> (aufgeloeste ID, aufgeloeste GID). IDs ohne
        Track-Zeile fehlen in der Abbildung; der Aufrufer laesst sie fallen
        (im Original macht das der ``JOIN``).
    """
    if not track_ids:
        return {}
    rows = connection.execute(
        _RESOLVE_SQL, {"ids": list(set(track_ids)), "max_depth": MAX_MERGE_DEPTH}
    ).fetchall()
    resolved = {row[0]: (row[1], row[2]) for row in rows}
    missing = set(track_ids) - set(resolved)
    if missing:
        _LOG.warning(
            "Fingerprints zeigen auf unbekannte Tracks",
            extra={"track_ids": sorted(missing)[:10], "count": len(missing)},
        )
    return resolved


def lookup_mbids(
    connection: psycopg.Connection, track_ids: Iterable[int]
) -> dict[int, list[tuple[str, int]]]:
    """MusicBrainz-Recording-MBIDs je AcoustID, mit ``submission_count``.

    Das ist der Einstieg jedes ``meta``-Lookups **und** zugleich alles, was
    der degradierte Betrieb braucht: MBIDs und ``sources`` stehen in unserer
    eigenen Datenbank, dafuer muss MusicBrainz nicht erreichbar sein
    (Invariante §8.7).

    Returns:
        Track-ID -> ``[(MBID, submission_count), …]``, nach MBID sortiert.
        AcoustIDs ohne (aktive) Zuordnung fehlen in der Abbildung.
    """
    ids = sorted(set(track_ids))
    if not ids:
        return {}
    result: dict[int, list[tuple[str, int]]] = {}
    for track_id, mbid, sources in connection.execute(_MBIDS_SQL, {"ids": ids}).fetchall():
        result.setdefault(track_id, []).append((mbid, sources))
    return result


def lookup_meta_ids(
    connection: psycopg.Connection,
    track_ids: Iterable[int],
    *,
    max_ids_per_track: int = MAX_META_IDS_PER_TRACK,
) -> dict[int, list[int]]:
    """IDs der eingereichten Textmetadaten je AcoustID (``usermeta``).

    Sortiert nach Einreichungszahl absteigend und je AcoustID auf
    ``max_ids_per_track`` gekappt — die Kappung macht das Original ebenfalls
    erst in Python, damit die Reihenfolge stimmt.
    """
    ids = sorted(set(track_ids))
    if not ids:
        return {}
    result: dict[int, list[int]] = {}
    for track_id, meta_id in connection.execute(_META_IDS_SQL, {"ids": ids}).fetchall():
        found = result.setdefault(track_id, [])
        if len(found) < max_ids_per_track:
            found.append(meta_id)
    return result


def lookup_meta(connection: psycopg.Connection, meta_ids: Iterable[int]) -> list[MetaRow]:
    """Die eingereichten Textmetadaten zu den IDs aus :func:`lookup_meta_ids`."""
    ids = sorted(set(meta_ids))
    if not ids:
        return []
    return [
        MetaRow(
            meta_id=row[0],
            track=row[1],
            artist=row[2],
            album=row[3],
            album_artist=row[4],
            track_no=row[5],
            disc_no=row[6],
            year=row[7],
        )
        for row in connection.execute(_META_SQL, {"ids": ids}).fetchall()
    ]


def resolve_track_gid(connection: psycopg.Connection, gid: str) -> tuple[int, UUID] | None:
    """Eine AcoustID nachschlagen und Merges folgen (Parameter ``trackid``).

    Returns:
        ``(Track-ID, Track-GID)`` des Ziels oder ``None``, wenn die GID
        unbekannt ist.
    """
    row = connection.execute(
        _RESOLVE_GID_SQL, {"gid": gid, "max_depth": MAX_MERGE_DEPTH}
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]
