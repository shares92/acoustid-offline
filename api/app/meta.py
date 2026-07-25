"""Der ``meta``-Parameter von ``/v2/lookup`` (Phase 10, ARCHITECTURE §7).

``meta`` entscheidet, wie viel Metadaten eine Antwort traegt — und ist der
komplizierteste Teil des ganzen Vertrags, weil er nicht additiv ist:

**Grammatik.** Die Werte werden an **Whitespace** getrennt; zusaetzlich gibt
es die numerische Kurzform (``0`` = nichts, ``1`` = ``recordingids``,
``2`` = ``m2``; das Zerlegen macht :mod:`acoustid_api.params`). Bekannt
sind ``recordings``, ``recordingids``, ``releases``, ``releaseids``,
``releasegroups``, ``releasegroupids``, ``tracks``, ``compress``,
``usermeta``, ``sources``, ``m2``; unbekannte Werte werden ignoriert.

**Praezedenz — genau ein Zweig.** Welcher Schluessel direkt unter einem
Treffer erscheint, entscheidet diese Reihenfolge (der erste zutreffende
gewinnt, alle weiteren wirken nur noch als Detailgrad):

1. ``m2``
2. ``recordings`` oder ``recordingids``
3. ``releasegroups`` oder ``releasegroupids``
4. ``releases`` oder ``releaseids``

Wer ``recordings releasegroups releases tracks`` schickt (so tut es Picard),
bekommt deshalb ``recordings[] -> releasegroups[] -> releases[] ->
mediums[] -> tracks[]``. Wer nur ``releases`` schickt (kein ``recordings``),
bekommt ``releases[]`` unmittelbar unter dem Treffer — dann aber ohne
MBID der Aufnahme.

**Was NICHT aus MusicBrainz kommt.** ``sources`` ist
``track_mbid.submission_count`` und ``usermeta`` sind die eingereichten
Textmetadaten (``track_meta``/``meta``) — beides eigener Bestand
(:mod:`acoustid_api.store`). ``tracks`` und ``compress`` sind reine
Strukturmodifikatoren auf bereits geladenen Daten.

**Degradierter Betrieb (Invariante §8.7).** Ist der MusicBrainz-Spiegel
nicht erreichbar oder passt sein Schema nicht, bleibt die Metadatenliste
leer — die Antwort geht mit HTTP 200 heraus und traegt weiterhin die
AcoustID-UUIDs sowie (in den Zweigen ``m2``/``recordings``/
``recordingids``) die MBIDs samt ``sources``. Ein
:class:`~shared.mb.MbQueryError` degradiert dagegen **nicht**: er wird zu
Fehler 5 / HTTP 500, sonst verschwindet ein Programmfehler hinter leeren
Metadaten.

Der Antwortaufbau ist eine moeglichst wortgetreue Uebersetzung von
``LookupHandler`` aus ``acoustid/api/v2/__init__.py`` (v26.3.1) — bis hin
zu Eigenheiten, die man „aufraeumen" wollen wuerde. Wo bewusst abgewichen
wird, steht der Grund an Ort und Stelle; die Liste fuer Leser steht in
docs/api-lookup.md.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

import psycopg

from acoustid_api.errors import InternalError
from acoustid_api.store import MetaRow, lookup_mbids, lookup_meta, lookup_meta_ids
from shared.mb import MbClient, MbQueryError, MbSchemaMismatch, MbUnavailable, MetadataResult

__all__ = [
    "KNOWN_META",
    "RECORDINGS_KEY",
    "MetaBranch",
    "MetaPlan",
    "inject_metadata",
]

_LOG = logging.getLogger(__name__)

#: Schluessel der Aufnahmenliste unter einem Treffer (Original:
#: ``LookupHandler.recordings_name``).
RECORDINGS_KEY: Final = "recordings"

#: Alle Werte, die ``meta`` kennt. Unbekannte werden stillschweigend
#: ignoriert — genau wie im Original, das schlicht auf Mitgliedschaft prueft.
KNOWN_META: Final = frozenset(
    {
        "recordings",
        "recordingids",
        "releases",
        "releaseids",
        "releasegroups",
        "releasegroupids",
        "tracks",
        "compress",
        "usermeta",
        "sources",
        "m2",
    }
)


class MetaBranch(Enum):
    """Welcher Schluessel direkt unter einem Treffer entsteht."""

    NONE = "none"
    M2 = "m2"
    RECORDINGS = "recordings"
    RELEASE_GROUPS = "releasegroups"
    RELEASES = "releases"


@dataclass(frozen=True, slots=True)
class MetaPlan:
    """Der ausgewertete ``meta``-Parameter.

    Attributes:
        branch: Der eine gewaehlte Zweig (Praezedenz, s. Modul-Docstring).
        recording_ids_only: ``recordingids`` — nur die MBID, kein Titel.
        release_ids_only: ``releaseids`` — nur die Release-MBID.
        release_group_ids_only: ``releasegroupids``.
        releases: Veroeffentlichungen werden gebraucht.
        release_groups: Release-Gruppen werden gebraucht.
        tracks: Medien- und Trackliste je Veroeffentlichung.
        compress: Redundante Felder aus der Antwort loeschen.
        usermeta: Rueckfall auf eingereichte Textmetadaten.
        sources: ``submission_count`` je MBID mitgeben.
    """

    branch: MetaBranch
    recording_ids_only: bool = False
    release_ids_only: bool = False
    release_group_ids_only: bool = False
    releases: bool = False
    release_groups: bool = False
    tracks: bool = False
    compress: bool = False
    usermeta: bool = False
    sources: bool = False

    @classmethod
    def parse(cls, meta: Iterable[str]) -> MetaPlan:
        """Wertet die ``meta``-Liste aus (Praezedenz inklusive)."""
        values = set(meta)
        recordings = "recordings" in values or "recordingids" in values
        release_groups = "releasegroups" in values or "releasegroupids" in values
        releases = "releases" in values or "releaseids" in values

        if "m2" in values:
            branch = MetaBranch.M2
        elif recordings:
            branch = MetaBranch.RECORDINGS
        elif release_groups:
            branch = MetaBranch.RELEASE_GROUPS
        elif releases:
            branch = MetaBranch.RELEASES
        else:
            branch = MetaBranch.NONE

        return cls(
            branch=branch,
            recording_ids_only="recordingids" in values,
            release_ids_only="releaseids" in values,
            release_group_ids_only="releasegroupids" in values,
            releases=releases,
            release_groups=release_groups,
            tracks="tracks" in values,
            compress="compress" in values,
            usermeta="usermeta" in values,
            sources="sources" in values,
        )

    @property
    def needs_musicbrainz(self) -> bool:
        """Braucht dieser Plan ueberhaupt eine MB-Abfrage?

        ``meta=sources`` allein zum Beispiel nicht — dann fehlt der Zweig,
        und die MBIDs kommen ohnehin aus der eigenen Datenbank.
        """
        return self.branch is not MetaBranch.NONE

    @property
    def load_releases(self) -> bool:
        """``m2`` laedt Veroeffentlichungen immer mit (Original-Verhalten)."""
        return self.releases or self.release_groups or self.branch is MetaBranch.M2

    @property
    def load_release_groups(self) -> bool:
        return self.release_groups


def inject_metadata(
    connection: psycopg.Connection,
    mb: MbClient | None,
    plan: MetaPlan,
    result_map: Mapping[int, list[dict[str, Any]]],
    *,
    keep_submitted_mbid: bool = False,
) -> bool:
    """Haengt die Metadaten an die bereits gebauten Trefferobjekte.

    Args:
        connection: Verbindung zur **eigenen** Postgres (MBIDs, usermeta).
        mb: MusicBrainz-Client; ``None`` bedeutet „nicht konfiguriert" und
            fuehrt zum selben degradierten Ergebnis wie ein Ausfall.
        plan: Ausgewerteter ``meta``-Parameter.
        result_map: Track-ID -> die Trefferobjekte dieser AcoustID. Ein
            Treffer kann in mehreren Teilanfragen vorkommen; alle bekommen
            dieselben Metadaten (Original: ein gemeinsames ``result_map``
            ueber die ganze Anfrage).
        keep_submitted_mbid: ``mb.keep_submitted_mbid`` — bei aufgeloesten
            Redirects die eingereichte statt der kanonischen MBID liefern.

    Returns:
        ``True``, wenn degradiert geantwortet wurde (MusicBrainz nicht
        erreichbar, Schema-Mismatch oder gar nicht konfiguriert).

    Raises:
        InternalError: Die MB-Abfrage ist gescheitert, obwohl Verbindung
            und Schema standen (Fehler 5 / HTTP 500).
    """
    if plan.branch is MetaBranch.NONE:
        return False
    injector = _Injector(connection, mb, plan, result_map, keep_submitted_mbid=keep_submitted_mbid)
    injector.run()
    return injector.degraded


class _Injector:
    """Der Antwortaufbau — Uebersetzung von ``LookupHandler``."""

    def __init__(
        self,
        connection: psycopg.Connection,
        mb: MbClient | None,
        plan: MetaPlan,
        result_map: Mapping[int, list[dict[str, Any]]],
        *,
        keep_submitted_mbid: bool,
    ) -> None:
        self.connection = connection
        self.mb = mb
        self.plan = plan
        self.result_map = result_map
        self.keep_submitted_mbid = keep_submitted_mbid
        self.degraded = mb is None

    # --- Einstieg ----------------------------------------------------------

    def run(self) -> None:
        match self.plan.branch:
            case MetaBranch.M2:
                self._inject_m2()
            case MetaBranch.RECORDINGS:
                self._inject_recordings()
            case MetaBranch.RELEASE_GROUPS:
                self._inject_grouped(release_groups=True)
            case MetaBranch.RELEASES:
                self._inject_grouped(release_groups=False)
            case MetaBranch.NONE:  # pragma: no cover - von inject_metadata abgefangen
                pass

    # --- Eigener Bestand ---------------------------------------------------

    def _recording_ids(
        self, *, add: bool, add_sources: bool = False
    ) -> tuple[dict[Any, list[dict[str, Any]]], dict[int, list[str]]]:
        """Die MBIDs unserer Treffer — ohne jede MusicBrainz-Abfrage.

        Args:
            add: ``True`` haengt je MBID ein ``recordings``-Objekt an den
                Treffer (Zweige ``m2``/``recordings``); ``False`` merkt sich
                nur, welche Treffer zu welcher MBID gehoeren (Zweige
                ``releases``/``releasegroups``, wo es kein
                ``recordings``-Objekt gibt).
            add_sources: ``sources`` (Einreichungszahl) mitgeben.

        Returns:
            ``(elemente_je_mbid, mbids_je_track)``.
        """
        elements: dict[Any, list[dict[str, Any]]] = {}
        by_track: dict[int, list[str]] = {}
        for track_id, mbids in lookup_mbids(self.connection, self.result_map).items():
            by_track[track_id] = []
            for mbid, sources in mbids:
                by_track[track_id].append(mbid)
                if not add:
                    elements.setdefault(mbid, []).extend(self.result_map[track_id])
                    continue
                for result_el in self.result_map[track_id]:
                    recording: dict[str, Any] = {"id": mbid}
                    if add_sources:
                        recording["sources"] = sources
                    result_el.setdefault(RECORDINGS_KEY, []).append(recording)
                    elements.setdefault(mbid, []).append(recording)
        return elements, by_track

    def _user_meta_ids(self) -> tuple[dict[Any, list[dict[str, Any]]], list[MetaRow]]:
        """``usermeta``: Platzhalter-Objekte je eingereichtem Metadatensatz."""
        elements: dict[Any, list[dict[str, Any]]] = {}
        meta_ids: list[int] = []
        for track_id, ids in lookup_meta_ids(self.connection, self.result_map).items():
            for meta_id in ids:
                meta_ids.append(meta_id)
                for result_el in self.result_map[track_id]:
                    recording: dict[str, Any] = {}
                    result_el.setdefault(RECORDINGS_KEY, []).append(recording)
                    elements.setdefault(meta_id, []).append(recording)
        return elements, lookup_meta(self.connection, meta_ids)

    # --- MusicBrainz -------------------------------------------------------

    def _load(self, mbids: Sequence[str], *, only_ids: bool = False) -> MetadataResult:
        """MB-Abfrage mit den beiden Ausgaengen des degradierten Betriebs."""
        if self.mb is None or not mbids:
            return MetadataResult()
        try:
            return self.mb.lookup_metadata(
                mbids,
                load_releases=self.plan.load_releases,
                load_release_groups=self.plan.load_release_groups,
                only_ids=only_ids,
            )
        except (MbUnavailable, MbSchemaMismatch) as exc:
            self.degraded = True
            _LOG.warning(
                "Lookup antwortet ohne MusicBrainz-Metadaten",
                extra={"mb_error": str(exc), "mb_error_type": type(exc).__name__},
            )
            return MetadataResult()
        except MbQueryError as exc:
            _LOG.error("MusicBrainz-Abfrage gescheitert", extra={"mb_error": str(exc)})
            raise InternalError() from exc

    @staticmethod
    def _apply_redirects(
        elements: dict[Any, list[dict[str, Any]]], redirects: Mapping[str, str]
    ) -> None:
        """Aufgeloeste MBIDs zusaetzlich unter ihrer kanonischen Form fuehren.

        Die Metadaten kommen unter der kanonischen MBID zurueck, die
        Antwortobjekte haengen aber an der eingereichten.
        """
        for submitted, canonical in redirects.items():
            if canonical == submitted:
                continue
            found = elements.get(submitted)
            if found:
                elements.setdefault(canonical, []).extend(found)

    def _log_missing(self, expected: Iterable[Any], rows: Sequence[dict[str, Any]]) -> None:
        """Aufnahmen, die der Spiegel nicht kennt (Original: Merge-Task).

        Das Original stellt daraus einen ``merge_missing_mbid``-Auftrag in
        eine Warteschlange und deaktiviert die MBID nach sieben Tagen. Wir
        haben keine solche Pflege (und keinen Schreibzugriff auf den
        Spiegel) — hier bleibt es beim Log; die Redirect-Aufloesung deckt
        den haeufigsten Grund bereits ab.
        """
        if self.degraded:
            return
        missing = set(expected) - {row["recording_id"] for row in rows}
        if missing:
            _LOG.info(
                "MusicBrainz kennt einzelne MBIDs nicht",
                extra={"mb_missing": sorted(str(item) for item in missing)[:10]},
            )

    # --- Zweig: recordings / recordingids ---------------------------------

    def _inject_recordings(self) -> None:
        elements, _ = self._recording_ids(add=True, add_sources=self.plan.sources)
        result = self._load(sorted(elements), only_ids=self._recordings_only_ids())
        self._apply_redirects(elements, result.redirects)
        rows = result.rows
        self._log_missing(elements, rows)

        if self.plan.usermeta and not rows:
            # Nur wenn MusicBrainz zu KEINER MBID etwas liefert — so steht
            # es im Original, und so bleibt es auch bei uns (der Rueckfall
            # greift damit auch im degradierten Betrieb).
            user_elements, meta_rows = self._user_meta_ids()
            elements.update(user_elements)
            rows = [_user_meta_row(row) for row in meta_rows]

        for recording, recording_rows in _group(rows, "recording_id", self._extract_recording):
            if self.plan.release_groups:
                self._fill_release_groups(recording, recording_rows)
                if self.plan.compress:
                    _compress_release_groups(recording)
            elif self.plan.releases:
                self._fill_releases(recording, recording_rows)
                if self.plan.compress:
                    _compress_releases(recording)
            for element in elements.get(recording["id"], ()):
                submitted = element.get("id")
                if self.plan.usermeta:
                    _strip_user_meta_ids(recording)
                element.update(recording)
                if self.keep_submitted_mbid and isinstance(submitted, str):
                    element["id"] = submitted

    def _recordings_only_ids(self) -> bool:
        """``recordingids`` ohne Release-Bedarf braucht nur die Existenzpruefung."""
        return self.plan.recording_ids_only and not self.plan.load_releases

    # --- Zweige: releases / releasegroups ----------------------------------

    def _inject_grouped(self, *, release_groups: bool) -> None:
        elements, by_track = self._recording_ids(add=False)
        result = self._load(sorted(elements))
        self._apply_redirects(elements, result.redirects)
        self._log_missing(elements, result.rows)

        # Die Metadaten tragen die kanonische MBID; unsere Zuordnung noch
        # die eingereichte.
        canonical = {
            track_id: {result.redirects.get(mbid, mbid) for mbid in mbids}
            for track_id, mbids in by_track.items()
        }
        for track_id, mbids in canonical.items():
            rows = [row for row in result.rows if row["recording_id"] in mbids]
            part: dict[str, Any] = {}
            if release_groups:
                self._fill_release_groups(part, rows)
            else:
                self._fill_releases(part, rows)
            for result_el in self.result_map[track_id]:
                result_el.update(part)

    # --- Bausteine ---------------------------------------------------------

    def _fill_release_groups(self, parent: dict[str, Any], rows: Sequence[dict[str, Any]]) -> None:
        parent["releasegroups"] = []
        for group, group_rows in _group(rows, "release_group_id", self._extract_release_group):
            parent["releasegroups"].append(group)
            if not self.plan.releases:
                continue
            self._fill_releases(group, group_rows)
            if self.plan.compress:
                for release in group["releases"]:
                    if release.get("artists") == group.get("artists") and "artists" in release:
                        del release["artists"]
                    if release.get("title") == group.get("title") and "title" in release:
                        del release["title"]

    def _fill_releases(self, parent: dict[str, Any], rows: Sequence[dict[str, Any]]) -> None:
        parent["releases"] = []
        for release, release_rows in _group(rows, "release_id", self._extract_release):
            parent["releases"].append(release)
            if not self.plan.tracks:
                continue
            release["mediums"] = _group_tracks(release_rows)
            if self.plan.compress:
                for medium in release["mediums"]:
                    for track in medium["tracks"]:
                        if "artists" in track and track["artists"] == release.get("artists"):
                            del track["artists"]

    def _extract_recording(self, row: dict[str, Any]) -> dict[str, Any]:
        recording: dict[str, Any] = {"id": row["recording_id"]}
        if self.plan.recording_ids_only:
            return recording
        recording["title"] = row["recording_title"] or ""
        if row["recording_duration"]:
            # Das Original serialisiert die (ganzzahlig abgeschnittenen)
            # Sekunden als float — `639.0`, nicht `639`.
            recording["duration"] = float(row["recording_duration"])
        if row["recording_artists"]:
            recording["artists"] = row["recording_artists"]
        return recording

    def _extract_release(self, row: dict[str, Any]) -> dict[str, Any]:
        release: dict[str, Any] = {"id": row["release_id"]}
        if self.plan.release_ids_only:
            return release
        release["title"] = row["release_title"] or ""
        if row["release_medium_count"]:
            release["medium_count"] = row["release_medium_count"]
        if row["release_track_count"]:
            release["track_count"] = row["release_track_count"]
        if row["release_artists"]:
            release["artists"] = row["release_artists"]
        events = [_release_event(event) for event in row["release_events"] or ()]
        if events:
            release["releaseevents"] = events
            # Das erste Ereignis wird zusaetzlich flach ins Release kopiert
            # (`country` und `date`) — Original-Verhalten, auf das Clients
            # sich stuetzen.
            release.update(events[0])
        return release

    def _extract_release_group(self, row: dict[str, Any]) -> dict[str, Any]:
        group: dict[str, Any] = {"id": row["release_group_id"]}
        if self.plan.release_group_ids_only:
            return group
        group["title"] = row["release_group_title"] or ""
        if row["release_group_primary_type"]:
            group["type"] = row["release_group_primary_type"]
        if row["release_group_secondary_types"]:
            group["secondarytypes"] = row["release_group_secondary_types"]
        if row["release_group_artists"]:
            group["artists"] = row["release_group_artists"]
        return group

    # --- Zweig: m2 ---------------------------------------------------------

    def _inject_m2(self) -> None:
        """``meta=2`` — Aufnahme mit einer flachen Trackliste.

        Eine eigene, aeltere Antwortform: statt ``releases`` haengt an der
        Aufnahme eine ``tracks``-Liste, in der jeder Track sein Medium und
        dessen Release mitbringt. Kein bekannter Client benutzt sie; der
        Vertrag verlangt sie trotzdem.
        """
        elements, _ = self._recording_ids(add=True)
        result = self._load(sorted(elements))
        self._apply_redirects(elements, result.redirects)
        self._log_missing(elements, result.rows)

        started: set[Any] = set()
        for row in result.rows:
            recording_id = row["recording_id"]
            for element in elements.get(recording_id, ()):
                if recording_id not in started:
                    if row["recording_duration"]:
                        element["duration"] = float(row["recording_duration"])
                    # Jedes Element bekommt seine EIGENE Liste; das Original
                    # teilt hier versehentlich eine (Aliasing) und haengt
                    # Tracks dann mehrfach an.
                    element["tracks"] = []
                if row.get("release_id") is None:
                    # Aufnahme ohne Veroeffentlichung — im Original gibt es
                    # diese Zeile nicht (INNER JOIN), bei uns schon.
                    continue
                element["tracks"].append(_m2_track(row))
            started.add(recording_id)


# --- Freie Helfer ----------------------------------------------------------


def _group(
    rows: Sequence[dict[str, Any]],
    key: str,
    extract: Any,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Zeilen nach einem Schluessel gruppieren, Reihenfolge des ersten Auftretens.

    Zeilen ohne diesen Schluessel (``None``) fallen heraus: das sind die
    Basiszeilen von Aufnahmen ohne Veroeffentlichung, die es im Original
    gar nicht erst gibt.
    """
    grouped: dict[Any, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for row in rows:
        identifier = row.get(key)
        if identifier is None:
            continue
        entry = grouped.get(identifier)
        if entry is None:
            entry = (extract(row), [])
            grouped[identifier] = entry
        entry[1].append(row)
    return list(grouped.values())


def _group_tracks(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Die Tracks eines Release nach Medien buendeln."""
    mediums: dict[Any, dict[str, Any]] = {}
    for row in rows:
        position = row["medium_position"]
        medium = mediums.get(position)
        if medium is None:
            medium = {
                "position": position,
                "tracks": [],
                # Enthaelt Data-Tracks mit — wie im Original, damit die Zahl
                # dieselbe bleibt (Fallstrick 4 des Phase-1-Berichts).
                "track_count": row["medium_track_count"],
            }
            if row["medium_format"]:
                medium["format"] = row["medium_format"]
            if row["medium_title"]:
                medium["title"] = row["medium_title"]
            mediums[position] = medium
        medium["tracks"].append(
            {
                "id": row["track_id"],
                "position": row["track_position"],
                "title": row["track_title"],
                "artists": row["track_artists"],
            }
        )
    return list(mediums.values())


def _release_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Ein Veroeffentlichungsereignis; leere Felder fallen weg."""
    result: dict[str, Any] = {}
    if event.get("release_country"):
        result["country"] = event["release_country"]
    date = {
        name: event[f"release_date_{name}"]
        for name in ("year", "month", "day")
        if event.get(f"release_date_{name}")
    }
    if date:
        result["date"] = date
    return result


def _m2_track(row: Mapping[str, Any]) -> dict[str, Any]:
    """Ein Track der ``m2``-Antwort samt Medium und Release."""
    medium: dict[str, Any] = {
        "track_count": row["medium_track_count"],
        "position": row["medium_position"],
        "release": {"id": row["release_id"], "title": row["release_title"]},
    }
    if row["medium_format"]:
        medium["format"] = row["medium_format"]
    track: dict[str, Any] = {
        "title": row["track_title"],
        "artists": row["track_artists"],
        "position": row["track_position"],
        "medium": medium,
    }
    if row["track_duration"] is not None:
        # Das Original ruft hier ungeprueft `float()` auf und stirbt an
        # einem Track ohne Laenge (in MusicBrainz nicht selten).
        track["duration"] = float(row["track_duration"])
    return track


def _compress_release_groups(recording: Mapping[str, Any]) -> None:
    """``compress`` im Zweig ``releasegroups``.

    Zwei Dinge passieren: aus jedem Track faellt der Titel, wenn er dem der
    Aufnahme entspricht — und aus der Release-Gruppe fallen die Kuenstler,
    wenn sie denen der Aufnahme entsprechen. Letzteres macht das Original
    (durch eine Einrueckung) **nur fuer die letzte** Release-Gruppe; das ist
    hier bewusst nachgebildet, damit die Antwort zeichengleich bleibt.
    """
    groups = recording.get("releasegroups", [])
    for group in groups:
        for release in group.get("releases", []):
            for medium in release.get("mediums", []):
                for track in medium["tracks"]:
                    if "title" in track and track["title"] == recording.get("title"):
                        del track["title"]
    if groups:
        last = groups[-1]
        if "artists" in last and last["artists"] == recording.get("artists"):
            del last["artists"]


def _compress_releases(recording: Mapping[str, Any]) -> None:
    """``compress`` im Zweig ``recordings`` + ``releases`` (ohne Gruppen)."""
    for release in recording.get("releases", []):
        for medium in release.get("mediums", []):
            for track in medium["tracks"]:
                if "title" in track and track["title"] == recording.get("title"):
                    del track["title"]
        if "artists" in release and release["artists"] == recording.get("artists"):
            del release["artists"]


def _strip_user_meta_ids(recording: dict[str, Any]) -> None:
    """``usermeta``: die kuenstlichen IDs der eigenen ``meta``-Zeilen entfernen.

    Die Rueckfall-Zeilen tragen als ``id`` die Ganzzahl aus unserer
    ``meta``-Tabelle — keine MBID. Sie darf nicht in die Antwort, sonst
    haelt ein Client sie fuer eine MusicBrainz-ID.
    """
    if not isinstance(recording.get("id"), int):
        return
    del recording["id"]
    if not recording.get("title"):
        recording.pop("title", None)
    for container in ("releasegroups", "releases"):
        entries = recording.get(container)
        if entries is None:
            continue
        kept = []
        for entry in entries:
            entry.pop("id", None)
            if not entry.get("title"):
                entry.pop("title", None)
            if container == "releasegroups":
                nested = [item for item in entry.get("releases", ()) if _strip_release(item)]
                if nested:
                    entry["releases"] = nested
                else:
                    entry.pop("releases", None)
            if entry:
                kept.append(entry)
        if kept:
            recording[container] = kept
        else:
            del recording[container]


def _strip_release(release: dict[str, Any]) -> bool:
    """ID und leeren Titel entfernen; ``False``, wenn nichts uebrig bleibt."""
    release.pop("id", None)
    if not release.get("title"):
        release.pop("title", None)
    return bool(release)


def _user_meta_row(row: MetaRow) -> dict[str, Any]:
    """Eine ``meta``-Zeile in das Zeilenformat der MB-Schicht uebersetzen.

    Die Struktur stammt aus ``acoustid/data/meta.py``: dieselben Schluessel
    wie eine MusicBrainz-Zeile, damit der Antwortaufbau nichts davon
    mitbekommt. Die ``id``-Felder tragen die Ganzzahl aus unserer
    ``meta``-Tabelle und werden spaeter entfernt.

    **Kuenstler stehen hier als nackte Zeichenketten** und nicht als
    ``{"id": …, "name": …}`` — das Original haengt an dieser Stelle den
    Textwert direkt in die Liste. Eine „aufgeraeumte" Struktur waere
    schoener und inkompatibel.
    """
    artists = [row.artist] if row.artist else []
    album_artists = [row.album_artist] if row.album_artist else []
    return {
        "recording_id": row.meta_id,
        "recording_title": row.track,
        "recording_artists": artists,
        "recording_duration": None,
        "track_id": row.meta_id,
        "track_position": row.track_no,
        "track_title": row.track,
        "track_artists": artists,
        "track_duration": None,
        "medium_position": row.disc_no,
        "medium_format": None,
        "medium_title": None,
        "medium_track_count": None,
        "release_id": row.meta_id,
        "release_title": row.album,
        "release_artists": album_artists,
        "release_medium_count": None,
        "release_track_count": None,
        "release_events": [
            {
                "release_country": "",
                "release_date_year": row.year,
                "release_date_month": None,
                "release_date_day": None,
            }
        ],
        "release_group_id": row.meta_id,
        "release_group_title": row.album,
        "release_group_artists": album_artists,
        "release_group_primary_type": None,
        "release_group_secondary_types": [],
    }
