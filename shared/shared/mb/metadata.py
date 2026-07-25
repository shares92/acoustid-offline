"""Choreografie der MB-Abfragen — der Nachbau von ``lookup_metadata``.

Hier wird aus den Einzelabfragen in :mod:`shared.mb.queries` genau die
flache Zeilenliste, die der Original-Server im Modul ``acoustid/data/
musicbrainz`` seinem Antwortaufbau vorlegt: **eine Zeile je (Aufnahme,
Track, Medium, Release)**, mit denselben Schluesselnamen. Dadurch bleibt
der Antwortaufbau im API-Dienst eine direkte Uebersetzung des Originals,
und die Frage „ist das kompatibel?" laesst sich Zeile fuer Zeile
beantworten.

**Reihenfolge der Aufrufe** (Bericht, Abschnitt „Choreografie")::

    meta=recordings           3 -> 1 (nur Misses) -> 3 -> 4
    meta=recordingids         2 -> 1 (nur Misses) -> 2
    meta=releases  (+tracks)  3 -> 1 -> 3 -> 5 -> 6, 7 -> 4
    meta=releasegroups        3 -> 1 -> 3 -> 5 -> 6, 7, 8, 9 -> 4

Die Nummern sind die Funktionen aus :mod:`shared.mb.queries`. Schritt 1 ist
die **Redirect-Aufloesung**: MBIDs, die es nicht (mehr) gibt, werden gegen
``recording_gid_redirect`` geprueft und der Durchlauf mit den kanonischen
MBIDs wiederholt. Ohne diesen zweiten Anlauf liefert die Instanz fuer jede
in MusicBrainz zusammengefuehrte Aufnahme dauerhaft leere Metadaten — der
realistische Haupt-Fehlerfall (DECISIONS „MB-Query-Schicht").

**Drei bewusste Abweichungen von der Referenz** (Fallstricke des Berichts):

1. Die Basiszeile entsteht immer, auch wenn die Aufnahme in keiner
   Veroeffentlichung steckt. Die INNER JOINs der Referenz lassen solche
   Aufnahmen komplett verschwinden — mit ihnen Titel, Laenge und Kuenstler.
   Bei uns bleiben die Release-Felder dann leer.
2. Fehlende Nebenzeilen (Artist-Credit, Medienzahl, Release-Gruppe) werden
   mit ``.get()`` gelesen. Die Referenz indiziert direkt und stirbt an
   einem ``KeyError``, sobald der Spiegel eine Inkonsistenz hat.
3. Sekunden entstehen per **Ganzzahldivision** aus Millisekunden — genau
   wie im SQL des Originals (``length / 1000``), also **abschneiden, nie
   runden**. 209 999 ms sind 209 Sekunden, nicht 210.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import psycopg

from shared.mb import queries
from shared.mb.queries import DEFAULT_ROW_LIMIT, ArtistRow, RecordingRow, ReleaseRow

__all__ = ["MetadataResult", "duration_seconds", "lookup_metadata"]

_LOG = logging.getLogger(__name__)

#: Wie oft eine Redirect-Kette verfolgt wird. ``recording_gid_redirect``
#: zeigt immer direkt auf die kanonische Zeile (MusicBrainz schreibt die
#: Kette beim Merge um); ein Durchlauf genuegt.
REDIRECT_PASSES: Final = 1


@dataclass(slots=True)
class MetadataResult:
    """Ergebnis eines Metadaten-Abrufs.

    Attributes:
        rows: Flache Zeilen im Format des Originals (siehe Modul-Docstring).
        redirects: Eingereichte MBID -> kanonische MBID, soweit aufgeloest.
        truncated: Die Release-Zeilen wurden gekappt (Zeilenobergrenze).
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    redirects: dict[str, str] = field(default_factory=dict)
    truncated: bool = False


def duration_seconds(length_ms: int | None) -> int | None:
    """Millisekunden -> Sekunden, **abgeschnitten** (nie gerundet).

    Das Original rechnet in SQL (``recording.length / 1000`` auf einer
    Integer-Spalte) und damit ganzzahlig. Wer hier rundet, liefert fuer
    jeden zweiten Titel eine Sekunde mehr als api.acoustid.org — sichtbar
    fuer jeden Client, der Laengen vergleicht.

    Returns:
        Sekunden oder ``None``, wenn keine Laenge hinterlegt ist.
    """
    if length_ms is None:
        return None
    return length_ms // 1000


def _artists(credits: dict[int, list[ArtistRow]], credit_id: int | None) -> list[dict[str, Any]]:
    """Artist-Credit in die Antwortstruktur uebersetzen (``joinphrase`` optional)."""
    result: list[dict[str, Any]] = []
    for artist in credits.get(credit_id, ()) if credit_id is not None else ():
        entry: dict[str, Any] = {"id": artist.gid, "name": artist.name}
        if artist.join_phrase:
            entry["joinphrase"] = artist.join_phrase
        result.append(entry)
    return result


def _recording_fields(recording: RecordingRow | None, gid: str) -> dict[str, Any]:
    """Die Basisfelder einer Zeile; ``None`` heisst „nur Existenz geprueft"."""
    if recording is None:
        return {"recording_id": gid, "recording_title": "", "recording_duration": None}
    return {
        "recording_id": recording.gid,
        "recording_title": recording.name or "",
        "recording_duration": duration_seconds(recording.length_ms),
    }


def _release_fields(row: ReleaseRow | None) -> dict[str, Any]:
    """Track-, Medium- und Release-Felder einer Zeile (leer ohne Zeile)."""
    if row is None:
        return dict.fromkeys(
            (
                "track_id",
                "track_position",
                "track_title",
                "track_duration",
                "medium_position",
                "medium_track_count",
                "medium_title",
                "medium_format",
                "release_id",
                "release_title",
                "release_medium_count",
                "release_track_count",
            )
        ) | {"release_events": []}
    return {
        "track_id": row.track_gid,
        "track_position": row.track_position,
        "track_title": row.track_name,
        "track_duration": duration_seconds(row.track_length_ms),
        "medium_position": row.medium_position,
        "medium_track_count": row.medium_track_count,
        "medium_title": row.medium_name,
        "medium_format": row.medium_format,
        "release_id": row.release_gid,
        "release_title": row.release_name,
    }


def _load_recordings(
    connection: psycopg.Connection, mbids: Sequence[str], *, only_ids: bool
) -> tuple[dict[str, RecordingRow | None], dict[str, str]]:
    """Schritte 3/2 und 1: Aufnahmen holen, Misses ueber Redirects nachreichen.

    Returns:
        ``(gefunden, redirects)`` — ``gefunden`` ist nach **kanonischer**
        MBID indiziert, ``redirects`` bildet die eingereichte MBID darauf
        ab. Bei ``only_ids`` sind die Werte ``None`` (nur Existenz geprueft).
    """
    found = _fetch_recordings(connection, mbids, only_ids=only_ids)
    missing = [mbid for mbid in mbids if mbid not in found]
    if not missing:
        return found, {}

    redirects = queries.resolve_recording_redirects(connection, missing)
    if not redirects:
        return found, {}

    canonical = sorted({new for new in redirects.values() if new not in found})
    if canonical:
        found.update(_fetch_recordings(connection, canonical, only_ids=only_ids))
    resolved = {old: new for old, new in redirects.items() if new in found}
    if resolved:
        _LOG.info(
            "Recording-MBIDs ueber recording_gid_redirect aufgeloest",
            extra={"mb_redirects": len(resolved)},
        )
    return found, resolved


def _fetch_recordings(
    connection: psycopg.Connection, mbids: Sequence[str], *, only_ids: bool
) -> dict[str, RecordingRow | None]:
    if only_ids:
        return dict.fromkeys(queries.existing_recording_mbids(connection, mbids))
    return dict(queries.recordings_by_mbids(connection, mbids))


def lookup_metadata(
    connection: psycopg.Connection,
    mbids: Sequence[str],
    *,
    load_releases: bool = False,
    load_release_groups: bool = False,
    only_ids: bool = False,
    row_limit: int = DEFAULT_ROW_LIMIT,
    release_event_view: bool = True,
) -> MetadataResult:
    """Metadaten zu Recording-MBIDs holen.

    Args:
        mbids: Recording-MBIDs aus unserer ``track_mbid``-Tabelle.
        load_releases: Veroeffentlichungen mitladen (Schritte 5, 6, 7).
        load_release_groups: zusaetzlich Release-Gruppen (Schritte 8, 9);
            setzt ``load_releases`` voraus, so wie im Original.
        only_ids: ``meta=recordingids`` — nur pruefen, ob es die Aufnahme
            gibt (Schritt 2 statt 3). Spart die Nutzdaten.
        row_limit: Zeilenobergrenze der Release-Abfrage.
        release_event_view: View ``release_event`` benutzen (sonst
            Rueckfall auf die beiden Basistabellen).

    Returns:
        :class:`MetadataResult`. Eine leere Zeilenliste ist der Normalfall
        fuer eine Aufnahme, die der Spiegel nicht kennt — kein Fehler.
    """
    unique = sorted(set(mbids))
    if not unique:
        return MetadataResult()

    found, redirects = _load_recordings(connection, unique, only_ids=only_ids)
    if not found:
        return MetadataResult(redirects=redirects)

    gids = sorted(found)
    release_rows: list[ReleaseRow] = []
    truncated = False
    if load_releases:
        batch = queries.recording_release_rows(connection, gids, limit_rows=row_limit)
        release_rows = batch.rows
        truncated = batch.truncated
        if truncated:
            _LOG.warning(
                "MB-Release-Zeilen gekappt — Antwort ist unvollstaendig",
                extra={"mb_row_limit": row_limit, "mb_recordings": len(gids)},
            )

    release_ids = {row.release_id for row in release_rows}
    counts = queries.release_counts(connection, release_ids) if release_ids else {}
    events = (
        queries.release_events(connection, release_ids, use_view=release_event_view)
        if release_ids
        else {}
    )

    groups: dict[int, queries.ReleaseGroupRow] = {}
    secondary: dict[int, list[str]] = {}
    if load_release_groups and release_rows:
        group_ids = {row.release_group_id for row in release_rows}
        groups = queries.release_groups(connection, group_ids)
        secondary = queries.release_group_secondary_types(connection, group_ids)

    credits = queries.artist_credits(
        connection,
        _artist_credit_ids(found, release_rows, groups, load_releases=load_releases),
    )

    rows = _build_rows(
        found,
        release_rows,
        counts=counts,
        events=events,
        groups=groups,
        secondary=secondary,
        credits=credits,
        load_releases=load_releases,
        load_release_groups=load_release_groups,
    )
    return MetadataResult(rows=rows, redirects=redirects, truncated=truncated)


def _artist_credit_ids(
    found: dict[str, RecordingRow | None],
    release_rows: list[ReleaseRow],
    groups: dict[int, queries.ReleaseGroupRow],
    *,
    load_releases: bool,
) -> set[int]:
    """Alle Artist-Credit-ID einer Anfrage — ein einziger Batch (Fallstrick 6)."""
    ids = {row.artist_credit for row in found.values() if row is not None}
    if load_releases:
        for row in release_rows:
            ids.add(row.release_artist_credit)
            ids.add(row.track_artist_credit)
    ids.update(group.artist_credit for group in groups.values())
    return ids


def _build_rows(
    found: dict[str, RecordingRow | None],
    release_rows: list[ReleaseRow],
    *,
    counts: dict[int, queries.ReleaseCounts],
    events: dict[int, list[queries.ReleaseEventRow]],
    groups: dict[int, queries.ReleaseGroupRow],
    secondary: dict[int, list[str]],
    credits: dict[int, list[ArtistRow]],
    load_releases: bool,
    load_release_groups: bool,
) -> list[dict[str, Any]]:
    """Die flachen Zeilen bauen — Reihenfolge stabil nach MBID."""
    by_recording: dict[str, list[ReleaseRow]] = {}
    for row in release_rows:
        by_recording.setdefault(row.recording_gid, []).append(row)

    rows: list[dict[str, Any]] = []
    for gid in sorted(found):
        recording = found[gid]
        base = _recording_fields(recording, gid)
        base["recording_artists"] = _artists(
            credits, recording.artist_credit if recording is not None else None
        )
        if not load_releases:
            rows.append(base)
            continue
        # Fallstrick 1: eine Aufnahme ohne Veroeffentlichung behaelt ihre
        # Basiszeile (die Referenz laesst sie ganz verschwinden).
        matching = by_recording.get(gid) or [None]
        for release_row in matching:
            row = dict(base) | _release_fields(release_row)
            row["release_artists"] = _artists(
                credits, release_row.release_artist_credit if release_row else None
            )
            row["track_artists"] = _artists(
                credits, release_row.track_artist_credit if release_row else None
            )
            if release_row is not None:
                count = counts.get(release_row.release_id)
                row["release_medium_count"] = count.medium_count if count else None
                row["release_track_count"] = count.track_count if count else None
                row["release_events"] = [
                    {
                        "release_country": event.country,
                        "release_date_year": event.date_year,
                        "release_date_month": event.date_month,
                        "release_date_day": event.date_day,
                    }
                    for event in events.get(release_row.release_id, ())
                ]
            if load_release_groups:
                row.update(_release_group_fields(release_row, groups, secondary, credits))
            rows.append(row)
    return rows


def _release_group_fields(
    release_row: ReleaseRow | None,
    groups: dict[int, queries.ReleaseGroupRow],
    secondary: dict[int, list[str]],
    credits: dict[int, list[ArtistRow]],
) -> dict[str, Any]:
    group = groups.get(release_row.release_group_id) if release_row is not None else None
    if group is None:
        return {
            "release_group_id": None,
            "release_group_title": None,
            "release_group_primary_type": None,
            "release_group_secondary_types": [],
            "release_group_artists": [],
        }
    assert release_row is not None
    return {
        "release_group_id": group.gid,
        "release_group_title": group.name,
        "release_group_primary_type": group.primary_type,
        "release_group_secondary_types": secondary.get(release_row.release_group_id, []),
        "release_group_artists": _artists(credits, group.artist_credit),
    }
