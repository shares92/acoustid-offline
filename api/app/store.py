"""Datenbankzugriffe der API auf die AcoustID-Postgres (ARCHITECTURE §5.2).

Alles, was API-Anfragen aus der eigenen Datenbank brauchen, steht in diesem
Modul. Bis Phase 10 waren das ausschliesslich Lesezugriffe des Lookups; seit
Phase 11 kommt der eine Schreibpfad des Submit dazu (``local_submission``,
ganz unten). Zuerst der Lookup:

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

**Lokale Einreichungen (Phase 11).** Eigene Submissions duerfen nicht in
``track``/``fingerprint`` stehen — der Delta-Importer wuerde sie beim
naechsten Tagesdelta ueberschreiben (ARCHITECTURE §5.2, Import-Regel 2).
Sie leben in ``local_submission`` und sind trotzdem auffindbar, weil sie im
Suchindex einen **reservierten Dokument-ID-Bereich** belegen:

=========================  =================================================
``[0, 2^31-1]``            Delta-Bestand — die Dokument-ID **ist** die
                           ``fingerprint.id`` (Spaltentyp ``integer``).
``[2^31, 2^32-1]``         lokale Einreichungen — Dokument-ID ist
                           :data:`LOCAL_DOC_ID_BASE` + ``local_track_id``.
=========================  =================================================

Der Index nimmt **u32**-Dokument-IDs (empirisch verifiziert, Phase 11); die
beiden Bereiche teilen ihn also lueckenlos und nachweisbar ueberschneidungs-
frei auf. Jede Abfrage, die Dokument- oder Track-IDs entgegennimmt, trennt
die beiden Welten deshalb zuerst per :func:`split_doc_ids` — und zwar nicht
nur der Ordnung halber: die Delta-Abfragen casten nach ``integer[]``, eine
lokale ID wuerde dort schlicht einen Bereichsfehler ausloesen.

Alle Anweisungen sind Raw-SQL mit expliziten Spaltenlisten; die Vektoren
kommen als ``list[int]`` (signed int32) aus psycopg und werden erst im
Rescoring vorzeichenlos gelesen.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import psycopg

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from acoustid_api.params import Submission

__all__ = [
    "LOCAL_DOC_ID_BASE",
    "MAX_LOCAL_TRACK_ID",
    "MAX_MERGE_DEPTH",
    "MAX_META_IDS_PER_TRACK",
    "Candidate",
    "MetaRow",
    "PendingSubmission",
    "StoredSubmission",
    "load_candidates",
    "load_pending_submissions",
    "lookup_mbids",
    "lookup_meta",
    "lookup_meta_ids",
    "mark_submissions_indexed",
    "resolve_track_gid",
    "resolve_tracks",
    "split_doc_ids",
    "store_submission",
]

_LOG = logging.getLogger(__name__)

#: Erste Dokument-ID des reservierten Bereichs fuer lokale Einreichungen.
#: Direkt oberhalb des Wertebereichs von ``fingerprint.id`` (``integer``) —
#: eine Kollision mit dem Delta-Bestand ist damit typbedingt unmoeglich.
LOCAL_DOC_ID_BASE: Final = 2**31

#: Groesste ``local_track_id``, die noch eine u32-Dokument-ID ergibt. Die
#: Sequenz in der Migration endet beim selben Wert (``NO CYCLE``), diese
#: Konstante ist die zweite Bremse davor.
MAX_LOCAL_TRACK_ID: Final = 2**31 - 1

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
class StoredSubmission:
    """Eine gespeicherte Einreichung — das Ergebnis von :func:`store_submission`.

    Attributes:
        local_track_id: Gruppenschluessel der Einreichung; ``+
            LOCAL_DOC_ID_BASE`` ergibt die Dokument-ID im Suchindex.
        local_track_gid: Die AcoustID, unter der Lookups sie ausliefern.
        row_ids: Die Submission-IDs der Antwort — eine je MBID, mindestens
            eine.
    """

    local_track_id: int
    local_track_gid: UUID
    row_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PendingSubmission:
    """Eine Einreichung, die der Suchindex noch nicht kennt (Status ``new``)."""

    local_track_id: int
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

# --- Lokale Einreichungen ---------------------------------------------------
#
# `DISTINCT ON (local_track_id)` ueberall dort, wo eine Einreichung gemeint
# ist und nicht eine Zeile: eine eingereichte Aufnahme mit drei MBIDs steht
# als drei Zeilen in der Tabelle, teilt sich aber Vektor, Laenge und GID.

_LOCAL_CANDIDATES_SQL: Final = """
SELECT DISTINCT ON (local_track_id) local_track_id, fingerprint
FROM local_submission
WHERE local_track_id = ANY(%(ids)s::integer[])
  AND length BETWEEN %(duration)s - %(diff)s AND %(duration)s + %(diff)s
ORDER BY local_track_id, id
"""

_LOCAL_RESOLVE_SQL: Final = """
SELECT DISTINCT ON (local_track_id) local_track_id, local_track_gid
FROM local_submission
WHERE local_track_id = ANY(%(ids)s::integer[])
ORDER BY local_track_id, id
"""

_LOCAL_RESOLVE_GID_SQL: Final = """
SELECT local_track_id
FROM local_submission
WHERE local_track_gid = %(gid)s
ORDER BY id
LIMIT 1
"""

# `count(*)` ist die Entsprechung zu `track_mbid.submission_count`: so oft
# wurde diese MBID fuer diese Aufnahme eingereicht (`sources` in der Antwort).
_LOCAL_MBIDS_SQL: Final = """
SELECT local_track_id, mbid::text, count(*)
FROM local_submission
WHERE local_track_id = ANY(%(ids)s::integer[])
  AND mbid IS NOT NULL
GROUP BY local_track_id, mbid
ORDER BY local_track_id, mbid
"""

_NEXT_LOCAL_TRACK_ID_SQL: Final = "SELECT nextval('local_submission_track_id_seq')"

_INSERT_SUBMISSION_SQL: Final = """
INSERT INTO local_submission (
    local_track_id, local_track_gid, fingerprint, length, bitrate, fileformat,
    mbid, puid, foreignid, track, artist, album, album_artist,
    track_no, disc_no, year, client, client_version, submitted_by
) VALUES (
    %(local_track_id)s, %(local_track_gid)s, %(fingerprint)s, %(length)s,
    %(bitrate)s, %(fileformat)s, %(mbid)s, %(puid)s, %(foreignid)s,
    %(track)s, %(artist)s, %(album)s, %(album_artist)s,
    %(track_no)s, %(disc_no)s, %(year)s, %(client)s, %(client_version)s, %(user)s
)
RETURNING id
"""

_PENDING_SUBMISSIONS_SQL: Final = """
SELECT DISTINCT ON (local_track_id) local_track_id, fingerprint
FROM local_submission
WHERE status = 'new'
ORDER BY local_track_id, id
LIMIT %(limit)s
"""

_MARK_INDEXED_SQL: Final = """
UPDATE local_submission
   SET status = 'indexed', indexed_at = now()
 WHERE local_track_id = ANY(%(ids)s::integer[])
   AND status = 'new'
"""


def split_doc_ids(doc_ids: Iterable[int]) -> tuple[list[int], list[int]]:
    """Dokument-IDs in die beiden Bereiche trennen.

    Returns:
        ``(fingerprint_ids, local_track_ids)`` — die erste Liste sind
        Dokument-IDs des Delta-Bestands (= ``fingerprint.id``), die zweite
        die um :data:`LOCAL_DOC_ID_BASE` verminderten IDs lokaler
        Einreichungen. Beide sind entdoppelt und aufsteigend sortiert.
    """
    delta: set[int] = set()
    local: set[int] = set()
    for doc_id in doc_ids:
        if doc_id >= LOCAL_DOC_ID_BASE:
            local.add(doc_id - LOCAL_DOC_ID_BASE)
        else:
            delta.add(doc_id)
    return sorted(delta), sorted(local)


def load_candidates(
    connection: psycopg.Connection,
    fingerprint_ids: Sequence[int],
    *,
    duration: int,
    max_duration_diff: int,
) -> list[Candidate]:
    """Vollvektoren der Kandidaten, gefiltert nach Laenge.

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        fingerprint_ids: Dokument-IDs aus dem Suchindex — beide Bereiche
            (Delta-Bestand und lokale Einreichungen) duerfen gemischt kommen.
        duration: Vom Client gemeldete Laenge in Sekunden.
        max_duration_diff: Zugelassene Abweichung in Sekunden (``±``).

    Returns:
        Die Kandidaten, die den Laengenfilter ueberstehen — in beliebiger
        Reihenfolge. Zeilen ohne Vektor, ohne Track oder ohne Laenge fallen
        heraus: ohne Vektor gibt es keinen Score, und eine unbekannte Laenge
        ist im Original ebenfalls kein Treffer (``BETWEEN`` auf ``NULL``).
        Lokale Einreichungen tragen ihre **Dokument-ID** als
        ``fingerprint_id`` und ``track_id``; damit sortieren sie bei
        Score-Gleichstand hinter den Delta-Bestand, und die
        Track-Aufloesung erkennt sie an derselben Grenze wieder.
    """
    delta_ids, local_ids = split_doc_ids(fingerprint_ids)
    arguments = {"duration": duration, "diff": max_duration_diff}
    candidates: list[Candidate] = []
    if delta_ids:
        rows = connection.execute(_CANDIDATES_SQL, {"ids": delta_ids, **arguments}).fetchall()
        candidates += [
            Candidate(fingerprint_id=row[0], track_id=row[1], hashes=row[2]) for row in rows
        ]
    if local_ids:
        rows = connection.execute(_LOCAL_CANDIDATES_SQL, {"ids": local_ids, **arguments}).fetchall()
        candidates += [
            Candidate(
                fingerprint_id=LOCAL_DOC_ID_BASE + row[0],
                track_id=LOCAL_DOC_ID_BASE + row[0],
                hashes=row[1],
            )
            for row in rows
        ]
    return candidates


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
        (im Original macht das der ``JOIN``). Lokale Einreichungen kennen
        keine Merges — sie loesen sich auf sich selbst auf und liefern die
        beim Einreichen vergebene GID.
    """
    if not track_ids:
        return {}
    delta_ids, local_ids = split_doc_ids(track_ids)
    resolved: dict[int, tuple[int, UUID]] = {}
    if delta_ids:
        rows = connection.execute(
            _RESOLVE_SQL, {"ids": delta_ids, "max_depth": MAX_MERGE_DEPTH}
        ).fetchall()
        resolved.update({row[0]: (row[1], row[2]) for row in rows})
    if local_ids:
        rows = connection.execute(_LOCAL_RESOLVE_SQL, {"ids": local_ids}).fetchall()
        resolved.update(
            {LOCAL_DOC_ID_BASE + row[0]: (LOCAL_DOC_ID_BASE + row[0], row[1]) for row in rows}
        )
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

    Bei lokalen Einreichungen sind es die **eingereichten** MBIDs, und
    ``sources`` zaehlt, wie oft dieselbe MBID fuer diese Aufnahme eingereicht
    wurde — dieselbe Bedeutung wie ``track_mbid.submission_count``, nur aus
    der eigenen Submit-Tabelle.

    Returns:
        Track-ID -> ``[(MBID, submission_count), …]``, nach MBID sortiert.
        AcoustIDs ohne (aktive) Zuordnung fehlen in der Abbildung.
    """
    delta_ids, local_ids = split_doc_ids(track_ids)
    result: dict[int, list[tuple[str, int]]] = {}
    if delta_ids:
        for track_id, mbid, sources in connection.execute(
            _MBIDS_SQL, {"ids": delta_ids}
        ).fetchall():
            result.setdefault(track_id, []).append((mbid, sources))
    if local_ids:
        for local_track_id, mbid, sources in connection.execute(
            _LOCAL_MBIDS_SQL, {"ids": local_ids}
        ).fetchall():
            result.setdefault(LOCAL_DOC_ID_BASE + local_track_id, []).append((mbid, sources))
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

    **Lokale Einreichungen bleiben aussen vor.** Ihre Textmetadaten stehen in
    ``local_submission`` und nicht in ``track_meta``; ``usermeta`` deckt sie
    bewusst noch nicht ab (docs/api-submit.md, „Grenzen"). Sie hier
    durchzureichen waere ausserdem ein Bereichsfehler — die Abfrage castet
    nach ``integer[]``.
    """
    ids, _ = split_doc_ids(track_ids)
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

    Findet auch die AcoustID einer lokalen Einreichung: was ein Lookup
    ausliefert, muss sich auch direkt nachschlagen lassen — sonst waere die
    lokale AcoustID eine ID zweiter Klasse.

    Returns:
        ``(Track-ID, Track-GID)`` des Ziels oder ``None``, wenn die GID
        unbekannt ist.
    """
    row = connection.execute(
        _RESOLVE_GID_SQL, {"gid": gid, "max_depth": MAX_MERGE_DEPTH}
    ).fetchone()
    if row is not None:
        return row[0], row[1]
    row = connection.execute(_LOCAL_RESOLVE_GID_SQL, {"gid": gid}).fetchone()
    if row is None:
        return None
    return LOCAL_DOC_ID_BASE + row[0], UUID(gid)


# --- Schreibpfad: lokale Einreichungen (Phase 11) ---------------------------


def store_submission(
    connection: psycopg.Connection,
    submission: Submission,
    *,
    client: str,
    client_version: str | None,
    user: str,
) -> StoredSubmission:
    """Schreibt eine eingereichte Aufnahme nach ``local_submission``.

    Je MBID entsteht eine eigene Zeile mit eigener Submission-ID (so verhaelt
    sich das Original); ohne MBID bleibt es bei einer. Alle Zeilen teilen
    sich ``local_track_id`` und ``local_track_gid`` — sie beschreiben
    dieselbe Aufnahme, und ein Lookup soll sie als **einen** Treffer
    ausliefern.

    Der Aufrufer haelt die Transaktion: eine Anfrage mit mehreren
    Teilanfragen wird ganz oder gar nicht gespeichert.

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        submission: Geprueftes Ergebnis aus :func:`~acoustid_api.params.parse_submit`.
        client: Application-Key des einreichenden Clients.
        client_version: ``clientversion``, falls mitgeschickt.
        user: ``user``-Key des Einreichenden (Phase 12 reicht ihn durch).

    Raises:
        ValueError: Der reservierte Dokument-ID-Bereich ist erschoepft.
    """
    row = connection.execute(_NEXT_LOCAL_TRACK_ID_SQL).fetchone()
    if row is None:  # pragma: no cover - nextval liefert immer eine Zeile
        raise RuntimeError("nextval('local_submission_track_id_seq') lieferte keine Zeile")
    local_track_id = int(row[0])
    if local_track_id > MAX_LOCAL_TRACK_ID:  # pragma: no cover - 2^31 Einreichungen
        raise ValueError(
            f"local_track_id {local_track_id} ueberschreitet den reservierten "
            f"Dokument-ID-Bereich (bis {MAX_LOCAL_TRACK_ID}) — der Suchindex "
            "nimmt nur u32-Dokument-IDs"
        )

    local_track_gid = uuid4()
    vector = [_to_signed_int32(value) for value in submission.hashes]
    shared = {
        "local_track_id": local_track_id,
        "local_track_gid": local_track_gid,
        "fingerprint": vector,
        "length": submission.duration,
        "bitrate": submission.bitrate,
        "fileformat": submission.fileformat,
        "puid": submission.puid,
        "foreignid": submission.foreignid,
        "track": submission.track,
        "artist": submission.artist,
        "album": submission.album,
        "album_artist": submission.album_artist,
        "track_no": submission.track_no,
        "disc_no": submission.disc_no,
        "year": submission.year,
        "client": client,
        "client_version": client_version,
        "user": user,
    }
    row_ids: list[int] = []
    for mbid in submission.mbids or (None,):
        inserted = connection.execute(_INSERT_SUBMISSION_SQL, {**shared, "mbid": mbid}).fetchone()
        if inserted is None:  # pragma: no cover - RETURNING liefert immer eine Zeile
            raise RuntimeError("INSERT INTO local_submission lieferte keine ID zurueck")
        row_ids.append(int(inserted[0]))
    return StoredSubmission(
        local_track_id=local_track_id,
        local_track_gid=local_track_gid,
        row_ids=tuple(row_ids),
    )


def load_pending_submissions(
    connection: psycopg.Connection, *, limit: int
) -> list[PendingSubmission]:
    """Einreichungen im Status ``new``, aelteste zuerst.

    Das ist der Arbeitsvorrat der Indexierung (Partialindex
    ``local_submission_idx_unindexed``). ``limit`` zaehlt **Einreichungen**,
    nicht Zeilen: eine Aufnahme mit drei MBIDs ist ein Eintrag.
    """
    rows = connection.execute(_PENDING_SUBMISSIONS_SQL, {"limit": limit}).fetchall()
    return [PendingSubmission(local_track_id=row[0], hashes=row[1]) for row in rows]


def mark_submissions_indexed(
    connection: psycopg.Connection, local_track_ids: Sequence[int]
) -> None:
    """Setzt ``new`` -> ``indexed`` fuer alle Zeilen dieser Einreichungen.

    Wird **nach** dem ``_update`` des Suchindex aufgerufen — nie davor. Die
    umgekehrte Reihenfolge waere stiller Datenverlust: als indexiert
    vermerkte Einreichungen, die der Index nie gesehen hat, taucht kein
    Lookup je wieder auf (dieselbe Regel wie beim Index-Feed des Importers,
    ARCHITECTURE §5.3).
    """
    if not local_track_ids:
        return
    connection.execute(_MARK_INDEXED_SQL, {"ids": sorted(set(local_track_ids))})


def _to_signed_int32(value: int) -> int:
    """u32-Hash in den Wertebereich der Spalte ``integer[]`` bringen.

    Der Chromaprint-Dekoder liefert vorzeichenlose Hashes, die Spalte haelt
    signed int32 — dasselbe Bitmuster, nur anders gelesen. Genau so stehen
    auch die Vektoren aus den Tagesdeltas in der Datenbank; das Rescoring
    liest beide wieder vorzeichenlos.
    """
    return value - 2**32 if value >= 2**31 else value
