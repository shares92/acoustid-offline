"""Die **einzige** Datei, die MusicBrainz-Tabellennamen kennt (§5.4).

Warum diese Regel so hart ist: das MB-Schema gehoert uns nicht. Es aendert
sich etwa einmal im Jahr (Schema-Sequenz aktuell 31, Mitte Mai), und der
Spiegel wird von einem fremden Compose-Stack betrieben. Solange alle
Tabellennamen an genau einer Stelle stehen, ist ein Schema-Upgrade eine
Diff-Ansicht dieser Datei und kein Suchlauf durchs Projekt. Ein Test haelt
die Regel fest (``musicbrainz.`` darf in keinem anderen Modul vorkommen).

**Designprinzipien** (aus docs/research/phase1-mb-schema.md):

* Alles **schema-qualifiziert** (``musicbrainz.recording``) — der
  ``search_path`` der Verbindung ist zusaetzlich gesetzt, aber nichts
  verlaesst sich darauf.
* **Explizite Spaltenlisten.** Die MB-Schema-Aenderungen 26 -> 32 waren fuer
  unsere Tabellen rein additiv (``artist_credit.gid``, ``medium.gid``);
  gegen additive Aenderungen ist eine explizite Liste ein No-Op, ein
  ``SELECT *`` dagegen ein Formatbruch.
* **Batch-first.** Jede Funktion nimmt eine Menge und liefert eine
  Abbildung. ``artist_credit`` ist in MB dedupliziert und wird von vielen
  Entitaeten geteilt — pro Recording zu joinen waere Faktor 10 mehr Zeilen
  (Fallstrick 6).
* **Keine Fachlogik.** Hier stehen Abfragen und ihre Zeilen; das Umrechnen
  von Millisekunden in Sekunden, das Gruppieren und das Antwortformat
  liegen in :mod:`shared.mb.metadata` bzw. im API-Dienst.
* ``= ANY(%(x)s::typ[])`` statt ``IN`` — mit **explizitem Cast**, weil an
  dieser Stelle kein Spaltenkontext existiert (LEARNINGS „psycopg3 schickt
  Python-Strings als *unknown*").

**Getrennte Abfragen statt der Referenz-Joins.** ``acoustid-server`` holt
Recording und Releases in **einer** Query mit INNER JOINs — Recordings ohne
Release verschwinden dadurch vollstaendig aus der Antwort (Fallstruck 1 des
Berichts). Hier ist die Basis (:func:`recordings_by_mbids`) immer eine
eigene Abfrage; die Release-Zeilen kommen getrennt dazu.

Die Funktionen bekommen eine offene ``psycopg``-Verbindung und fassen sie
nur lesend an. Die Uebersetzung von Treiberfehlern in
:mod:`shared.mb.errors` macht der Aufrufer (:class:`shared.mb.MbClient`);
dieses Modul bleibt frei davon, damit es ohne Client testbar ist.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg

__all__ = [
    "EXPECTED_COLUMNS",
    "RELEASE_EVENT_VIEW",
    "SCHEMA",
    "ArtistRow",
    "MbHealth",
    "RecordingRow",
    "ReleaseCounts",
    "ReleaseEventRow",
    "ReleaseGroupRow",
    "ReleaseRow",
    "ReleaseRows",
    "artist_credits",
    "existing_recording_mbids",
    "mb_health",
    "mb_selfcheck",
    "recording_release_rows",
    "recordings_by_mbids",
    "release_counts",
    "release_events",
    "release_group_secondary_types",
    "release_groups",
    "resolve_recording_redirects",
]

#: Schema des MusicBrainz-Spiegels (fest, vgl. musicbrainz-docker).
SCHEMA: Final = "musicbrainz"

#: Name der View, die ``release_country`` und ``release_unknown_country``
#: vereinigt. Ein Standard-``createdb.sh`` legt sie an; fehlt sie, faellt
#: :func:`release_events` auf die beiden Basistabellen zurueck.
RELEASE_EVENT_VIEW: Final = "release_event"

#: Was der Selfcheck erwartet: je Relation die Spalten, die in den
#: Abfragen unten wirklich vorkommen. **Nur diese** — zusaetzliche Spalten
#: im Spiegel sind kein Mismatch (die MB-Aenderungen waren bisher additiv).
#:
#: ``area`` steht bewusst nicht drin, obwohl der Bericht es zu den 17
#: Tabellen zaehlt: wir joinen ``iso_3166_1.area`` direkt gegen
#: ``release_event.country`` und brauchen die Area-Zeile nie.
EXPECTED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "recording": frozenset({"id", "gid", "name", "length", "artist_credit"}),
    "artist_credit": frozenset({"id", "name"}),
    "artist_credit_name": frozenset({"artist_credit", "position", "name", "join_phrase", "artist"}),
    "artist": frozenset({"id", "gid", "name"}),
    "track": frozenset(
        {"id", "gid", "recording", "medium", "position", "name", "artist_credit", "length"}
    ),
    "medium": frozenset({"id", "release", "position", "format", "name", "track_count"}),
    "medium_format": frozenset({"id", "name"}),
    "release": frozenset({"id", "gid", "name", "artist_credit", "release_group"}),
    "release_group": frozenset({"id", "gid", "name", "artist_credit", "type"}),
    "release_group_primary_type": frozenset({"id", "name"}),
    "release_group_secondary_type_join": frozenset({"release_group", "secondary_type"}),
    "release_group_secondary_type": frozenset({"id", "name", "child_order"}),
    "release_country": frozenset({"release", "country", "date_year", "date_month", "date_day"}),
    "release_unknown_country": frozenset({"release", "date_year", "date_month", "date_day"}),
    "iso_3166_1": frozenset({"area", "code"}),
    "recording_gid_redirect": frozenset({"gid", "new_id"}),
    "replication_control": frozenset(
        {"id", "current_schema_sequence", "current_replication_sequence", "last_replication_date"}
    ),
}

#: Zeilenobergrenze von :func:`recording_release_rows` (DoS-Schutz, s. u.).
DEFAULT_ROW_LIMIT: Final = 5000


# --- Zeilenformate ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MbHealth:
    """``replication_control`` — Schema-Stand und Replikationsalter.

    Attributes:
        schema_sequence: ``current_schema_sequence`` (2026: 31).
        replication_sequence: Laufende Nummer der letzten Replikation.
        last_replication_date: Zeitpunkt der letzten Replikation; ``None``
            bei einem Spiegel, der noch nie repliziert hat.
    """

    schema_sequence: int
    replication_sequence: int | None
    last_replication_date: datetime | None


@dataclass(frozen=True, slots=True)
class RecordingRow:
    """Eine Aufnahme — die Basiszeile jeder Metadaten-Antwort.

    Attributes:
        recording_id: Interne ``recording.id`` (nur fuer Folgeabfragen).
        gid: Kanonische Recording-MBID als Zeichenkette.
        name: Titel; in MB nicht null, kann aber leer sein.
        length_ms: Laenge in **Millisekunden** oder ``None``. Die Umrechnung
            in Sekunden (abschneiden!) macht :mod:`shared.mb.metadata`.
        artist_credit: ID fuer :func:`artist_credits`.
        artist_credit_name: Der zusammengesetzte Kuenstlername als Text.
    """

    recording_id: int
    gid: str
    name: str
    length_ms: int | None
    artist_credit: int
    artist_credit_name: str | None


@dataclass(frozen=True, slots=True)
class ArtistRow:
    """Ein Glied eines Artist-Credits, in Anzeigereihenfolge."""

    gid: str
    name: str
    join_phrase: str


@dataclass(frozen=True, slots=True)
class ReleaseRow:
    """Eine Zeile aus ``track -> medium -> release`` zu einer Aufnahme."""

    recording_gid: str
    track_gid: str
    track_position: int
    track_name: str
    track_artist_credit: int
    track_length_ms: int | None
    medium_position: int
    medium_track_count: int
    medium_name: str | None
    medium_format: str | None
    release_id: int
    release_gid: str
    release_name: str
    release_artist_credit: int
    release_group_id: int


@dataclass(frozen=True, slots=True)
class ReleaseRows:
    """Ergebnis von :func:`recording_release_rows` samt Kappungs-Anzeige.

    Attributes:
        rows: Die gelesenen Zeilen, deterministisch sortiert.
        truncated: ``True``, wenn die Zeilenobergrenze gegriffen hat — die
            Antwort ist dann unvollstaendig und das Ereignis gehoert ins Log.
    """

    rows: list[ReleaseRow]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ReleaseCounts:
    """Medien- und Trackzahl eines Release.

    ``track_count`` zaehlt Data-Tracks mit — das tut die Referenz auch, und
    Clients kennen den Wert so (Fallstrick 4).
    """

    medium_count: int
    track_count: int


@dataclass(frozen=True, slots=True)
class ReleaseEventRow:
    """Ein Veroeffentlichungsereignis (Land + unvollstaendiges Datum).

    ``country`` ist ``None``, wenn das Ereignis kein Land traegt — der
    LEFT JOIN auf ``iso_3166_1`` ist deshalb Pflicht (Fallstrick 5).
    """

    country: str | None
    date_year: int | None
    date_month: int | None
    date_day: int | None


@dataclass(frozen=True, slots=True)
class ReleaseGroupRow:
    """Eine Release-Gruppe mit ihrem Primaertyp."""

    gid: str
    name: str
    artist_credit: int
    primary_type: str | None


# --- Abfragen --------------------------------------------------------------

_HEALTH_SQL: Final = """
SELECT current_schema_sequence, current_replication_sequence, last_replication_date
FROM musicbrainz.replication_control
WHERE id = 1
"""

# Der Selfcheck liest den Ist-Zustand aus dem Systemkatalog. `pg_attribute`
# statt `information_schema.columns`, weil letzteres nur Relationen zeigt,
# auf die der aufrufende Benutzer Rechte hat — eine fehlende Berechtigung
# saehe dann wie eine fehlende Spalte aus, und wir wollen beides
# unterscheiden koennen (die Abfrage darunter faellt sonst auf
# `permission denied` und damit auf MbQueryError).
_SELFCHECK_SQL: Final = """
SELECT c.relname, a.attname
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s
  AND c.relname = ANY(%(relations)s::name[])
  AND a.attnum > 0
  AND NOT a.attisdropped
"""

_REDIRECT_SQL: Final = """
SELECT redirect.gid::text AS old_gid, r.gid::text AS new_gid
FROM musicbrainz.recording_gid_redirect redirect
JOIN musicbrainz.recording r ON r.id = redirect.new_id
WHERE redirect.gid = ANY(%(mbids)s::uuid[])
"""

_EXISTING_SQL: Final = """
SELECT r.gid::text
FROM musicbrainz.recording r
WHERE r.gid = ANY(%(mbids)s::uuid[])
"""

_RECORDINGS_SQL: Final = """
SELECT r.id, r.gid::text, r.name, r.length, r.artist_credit, ac.name
FROM musicbrainz.recording r
LEFT JOIN musicbrainz.artist_credit ac ON ac.id = r.artist_credit
WHERE r.gid = ANY(%(mbids)s::uuid[])
"""

_ARTIST_CREDITS_SQL: Final = """
SELECT acn.artist_credit, a.gid::text, acn.name, acn.join_phrase
FROM musicbrainz.artist_credit_name acn
JOIN musicbrainz.artist a ON a.id = acn.artist
WHERE acn.artist_credit = ANY(%(ids)s::integer[])
ORDER BY acn.artist_credit, acn.position
"""

# Der teuerste Aufruf der Schicht und der einzige, dessen Zeilenzahl der
# Anfragende beeinflusst: 20 Fingerprints x N MBIDs x jede Veroeffentlichung
# jeder Aufnahme. Deshalb die harte Obergrenze (LIMIT n+1 -> Kappung
# erkennbar) und eine vollstaendige, deterministische Sortierung.
_RELEASE_ROWS_SQL: Final = """
SELECT
    r.gid::text,
    t.gid::text,
    t.position,
    t.name,
    t.artist_credit,
    t.length,
    m.position,
    m.track_count,
    m.name,
    mf.name,
    rel.id,
    rel.gid::text,
    rel.name,
    rel.artist_credit,
    rel.release_group
FROM musicbrainz.recording r
JOIN musicbrainz.track t ON t.recording = r.id
JOIN musicbrainz.medium m ON m.id = t.medium
JOIN musicbrainz.release rel ON rel.id = m.release
LEFT JOIN musicbrainz.medium_format mf ON mf.id = m.format
WHERE r.gid = ANY(%(mbids)s::uuid[])
ORDER BY r.gid, rel.id, m.position, t.position, t.id
LIMIT %(limit)s
"""

_RELEASE_COUNTS_SQL: Final = """
SELECT m.release, count(m.id), sum(m.track_count)
FROM musicbrainz.medium m
WHERE m.release = ANY(%(ids)s::integer[])
GROUP BY m.release
"""

# Sortierung bewusst deterministisch: das **erste** Ereignis wird spaeter
# flach ins Release kopiert (Original-Verhalten), und die Referenz ueberlaesst
# die Reihenfolge dem Planer. Ereignisse mit Land zuerst, dann nach Datum.
_RELEASE_EVENTS_ORDER: Final = """
ORDER BY 1, 2 NULLS LAST, 3 NULLS LAST, 4 NULLS LAST, 5 NULLS LAST
"""

_RELEASE_EVENTS_VIEW_SQL: Final = (
    """
SELECT re.release, iso.code, re.date_year, re.date_month, re.date_day
FROM musicbrainz.release_event re
LEFT JOIN musicbrainz.iso_3166_1 iso ON iso.area = re.country
WHERE re.release = ANY(%(ids)s::integer[])
"""
    + _RELEASE_EVENTS_ORDER
)

# Rueckfallweg ohne die View: exakt ihre Definition, von Hand.
_RELEASE_EVENTS_UNION_SQL: Final = (
    """
SELECT rc.release, iso.code, rc.date_year, rc.date_month, rc.date_day
FROM musicbrainz.release_country rc
LEFT JOIN musicbrainz.iso_3166_1 iso ON iso.area = rc.country
WHERE rc.release = ANY(%(ids)s::integer[])
UNION ALL
SELECT ruc.release, NULL, ruc.date_year, ruc.date_month, ruc.date_day
FROM musicbrainz.release_unknown_country ruc
WHERE ruc.release = ANY(%(ids)s::integer[])
"""
    + _RELEASE_EVENTS_ORDER
)

_RELEASE_GROUPS_SQL: Final = """
SELECT rg.id, rg.gid::text, rg.name, rg.artist_credit, pt.name
FROM musicbrainz.release_group rg
LEFT JOIN musicbrainz.release_group_primary_type pt ON pt.id = rg.type
WHERE rg.id = ANY(%(ids)s::integer[])
"""

# `child_order` ist die Anzeigereihenfolge der Typen in MusicBrainz; die
# Referenz sortiert gar nicht. Deterministisch ist hier besser: die Liste
# landet unveraendert in der Antwort.
_SECONDARY_TYPES_SQL: Final = """
SELECT j.release_group, st.name
FROM musicbrainz.release_group_secondary_type_join j
JOIN musicbrainz.release_group_secondary_type st ON st.id = j.secondary_type
WHERE j.release_group = ANY(%(ids)s::integer[])
ORDER BY j.release_group, st.child_order, st.id
"""


def _rows(connection: psycopg.Connection, sql: str, params: dict[str, Any]) -> list[Any]:
    return connection.execute(sql, params).fetchall()


def mb_health(connection: psycopg.Connection) -> MbHealth:
    """0 — Schema-Sequenz und Replikationsalter des Spiegels.

    Grundlage des Schema-Guards (passt die Sequenz zur Erwartung?) und der
    Staleness-Ueberwachung. Die Zeile mit ``id = 1`` ist die einzige.

    Raises:
        LookupError: ``replication_control`` ist leer — dann ist die
            Datenbank kein MusicBrainz-Spiegel.
    """
    row = connection.execute(_HEALTH_SQL).fetchone()
    if row is None:
        raise LookupError("musicbrainz.replication_control enthaelt keine Zeile mit id = 1")
    return MbHealth(
        schema_sequence=row[0], replication_sequence=row[1], last_replication_date=row[2]
    )


def mb_selfcheck(connection: psycopg.Connection) -> dict[str, frozenset[str]]:
    """0b — Ist-Spalten der erwarteten Relationen aus dem Systemkatalog.

    Returns:
        Abbildung Relationsname -> vorhandene Spalten. Relationen, die es
        nicht gibt (oder die dem Benutzer verborgen sind), fehlen in der
        Abbildung — der Aufrufer bildet daraus den Diff.
    """
    relations = sorted({*EXPECTED_COLUMNS, RELEASE_EVENT_VIEW})
    found: dict[str, set[str]] = {}
    for relname, attname in _rows(
        connection, _SELFCHECK_SQL, {"schema": SCHEMA, "relations": relations}
    ):
        found.setdefault(relname, set()).add(attname)
    return {name: frozenset(columns) for name, columns in found.items()}


def resolve_recording_redirects(
    connection: psycopg.Connection, mbids: Sequence[str]
) -> dict[str, str]:
    """1 — Gemergte Recording-MBIDs auf ihre kanonische MBID abbilden.

    Der realistische Haupt-Fehlerfall der Instanz: unsere ``track_mbid``
    altern mit dem Delta-Bestand, in MusicBrainz werden Aufnahmen aber
    laufend zusammengefuehrt. Ohne diese Aufloesung liefert der Lookup fuer
    gemergte Aufnahmen dauerhaft leere Metadaten.

    Args:
        mbids: **Nur die nicht gefundenen** MBIDs (der Bericht: zweiter
            Batch nach dem ersten Treffer-Durchlauf).

    Returns:
        Eingereichte MBID -> kanonische MBID. MBIDs ohne Weiterleitung
        fehlen in der Abbildung.
    """
    if not mbids:
        return {}
    return dict(_rows(connection, _REDIRECT_SQL, {"mbids": list(mbids)}))


def existing_recording_mbids(connection: psycopg.Connection, mbids: Sequence[str]) -> set[str]:
    """2 — Reine Existenzpruefung (``meta=recordingids``).

    Braucht nur den Unique-Index auf ``recording.gid`` und laedt keine
    Nutzdaten — fuer ``recordingids`` ist genau das die ganze Frage.
    """
    if not mbids:
        return set()
    return {row[0] for row in _rows(connection, _EXISTING_SQL, {"mbids": list(mbids)})}


def recordings_by_mbids(
    connection: psycopg.Connection, mbids: Sequence[str]
) -> dict[str, RecordingRow]:
    """3 — Die Basiszeilen: Titel, Laenge (ms), Artist-Credit.

    Bewusst **ohne** Join auf ``track``/``release``: die INNER JOINs der
    Referenz lassen Aufnahmen ohne Veroeffentlichung komplett verschwinden
    (Fallstrick 1).

    Returns:
        MBID -> Zeile. Nicht gefundene MBIDs fehlen (Kandidaten fuer
        :func:`resolve_recording_redirects`).
    """
    if not mbids:
        return {}
    result: dict[str, RecordingRow] = {}
    for row in _rows(connection, _RECORDINGS_SQL, {"mbids": list(mbids)}):
        result[row[1]] = RecordingRow(
            recording_id=row[0],
            gid=row[1],
            name=row[2],
            length_ms=row[3],
            artist_credit=row[4],
            artist_credit_name=row[5],
        )
    return result


def artist_credits(
    connection: psycopg.Connection, artist_credit_ids: Iterable[int]
) -> dict[int, list[ArtistRow]]:
    """4 — Alle Artist-Credits einer Anfrage in **einem** Aufruf.

    ``artist_credit`` ist in MusicBrainz dedupliziert und wird von
    Recording, Release, Release-Gruppe und Track geteilt; genau deshalb wird
    die Menge aller ID gesammelt und einmal aufgeloest (Fallstrick 6).

    Returns:
        Artist-Credit-ID -> Glieder in Anzeigereihenfolge (``position``).
    """
    ids = sorted({item for item in artist_credit_ids if item is not None})
    if not ids:
        return {}
    result: dict[int, list[ArtistRow]] = {}
    for credit_id, gid, name, join_phrase in _rows(connection, _ARTIST_CREDITS_SQL, {"ids": ids}):
        result.setdefault(credit_id, []).append(
            ArtistRow(gid=gid, name=name, join_phrase=join_phrase or "")
        )
    return result


def recording_release_rows(
    connection: psycopg.Connection,
    mbids: Sequence[str],
    *,
    limit_rows: int = DEFAULT_ROW_LIMIT,
) -> ReleaseRows:
    """5 — ``track -> medium -> release`` (+ Format) zu den Aufnahmen.

    Der **DoS-Vektor** der Schicht: eine Anfrage darf 20 Fingerprints
    mitbringen, jeder Treffer beliebig viele MBIDs, und eine populaere
    Aufnahme steckt in tausenden Veroeffentlichungen. Deshalb eine harte
    Zeilenobergrenze.

    Args:
        limit_rows: Obergrenze; gelesen wird ``limit_rows + 1``, damit die
            Kappung erkennbar ist.

    Returns:
        :class:`ReleaseRows` — Zeilen plus ``truncated``. Bei ``truncated``
        ist die Antwort unvollstaendig; der Aufrufer protokolliert das.
    """
    if not mbids:
        return ReleaseRows(rows=[], truncated=False)
    raw = _rows(connection, _RELEASE_ROWS_SQL, {"mbids": list(mbids), "limit": limit_rows + 1})
    truncated = len(raw) > limit_rows
    rows = [
        ReleaseRow(
            recording_gid=row[0],
            track_gid=row[1],
            track_position=row[2],
            track_name=row[3],
            track_artist_credit=row[4],
            track_length_ms=row[5],
            medium_position=row[6],
            medium_track_count=row[7],
            medium_name=row[8],
            medium_format=row[9],
            release_id=row[10],
            release_gid=row[11],
            release_name=row[12],
            release_artist_credit=row[13],
            release_group_id=row[14],
        )
        for row in raw[:limit_rows]
    ]
    return ReleaseRows(rows=rows, truncated=truncated)


def release_counts(
    connection: psycopg.Connection, release_ids: Iterable[int]
) -> dict[int, ReleaseCounts]:
    """6 — Medienzahl und Summe der Trackzahlen je Release."""
    ids = sorted({item for item in release_ids if item is not None})
    if not ids:
        return {}
    return {
        row[0]: ReleaseCounts(medium_count=row[1], track_count=int(row[2] or 0))
        for row in _rows(connection, _RELEASE_COUNTS_SQL, {"ids": ids})
    }


def release_events(
    connection: psycopg.Connection,
    release_ids: Iterable[int],
    *,
    use_view: bool = True,
) -> dict[int, list[ReleaseEventRow]]:
    """7 — Veroeffentlichungsereignisse je Release.

    Args:
        use_view: ``True`` liest die View ``musicbrainz.release_event``
            (Standardfall eines mit ``createdb.sh`` gebauten Spiegels);
            ``False`` bildet ihre Definition von Hand aus
            ``release_country`` und ``release_unknown_country`` nach. Den
            Schalter setzt der Client anhand des Selfchecks — probiert wird
            nie, ein Fehlversuch wuerde die Transaktion abbrechen.
    """
    ids = sorted({item for item in release_ids if item is not None})
    if not ids:
        return {}
    sql = _RELEASE_EVENTS_VIEW_SQL if use_view else _RELEASE_EVENTS_UNION_SQL
    result: dict[int, list[ReleaseEventRow]] = {}
    for release_id, country, year, month, day in _rows(connection, sql, {"ids": ids}):
        result.setdefault(release_id, []).append(
            ReleaseEventRow(country=country, date_year=year, date_month=month, date_day=day)
        )
    return result


def release_groups(
    connection: psycopg.Connection, release_group_ids: Iterable[int]
) -> dict[int, ReleaseGroupRow]:
    """8 — Release-Gruppen mit Primaertyp (``Album``, ``Single``, …)."""
    ids = sorted({item for item in release_group_ids if item is not None})
    if not ids:
        return {}
    return {
        row[0]: ReleaseGroupRow(gid=row[1], name=row[2], artist_credit=row[3], primary_type=row[4])
        for row in _rows(connection, _RELEASE_GROUPS_SQL, {"ids": ids})
    }


def release_group_secondary_types(
    connection: psycopg.Connection, release_group_ids: Iterable[int]
) -> dict[int, list[str]]:
    """9 — Sekundaertypen je Release-Gruppe (``Compilation``, ``Live``, …)."""
    ids = sorted({item for item in release_group_ids if item is not None})
    if not ids:
        return {}
    result: dict[int, list[str]] = {}
    for release_group_id, name in _rows(connection, _SECONDARY_TYPES_SQL, {"ids": ids}):
        result.setdefault(release_group_id, []).append(name)
    return result
