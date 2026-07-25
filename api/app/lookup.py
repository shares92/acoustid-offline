"""Antwortaufbau von ``/v2/lookup`` (ARCHITECTURE §7).

Die eigentliche Sucharbeit macht :class:`acoustid_api.matching.Matcher`;
hier entsteht daraus der Antwortbaum, und zwar in genau den beiden Formen,
die das Original kennt::

    {"status": "ok", "results": [{"id": "<track-gid>", "score": 1.0}]}

    {"status": "ok", "fingerprints": [{"index": 0, "results": [...]}]}

Die zweite Form gilt, sobald ``batch`` gesetzt ist — dann wird **jede**
Teilanfrage beantwortet, sonst nur die erste. ``index`` traegt die Nummer
aus dem Parameter-Suffix (``fingerprint.3`` -> ``3``) und ist ``null``, wenn
die Teilanfrage ohne Suffix kam.

``meta`` haengt seit Phase 10 Metadaten an die fertigen Trefferobjekte
(:mod:`acoustid_api.meta`). Wichtig dabei: das geschieht **einmal fuer die
ganze Anfrage**, nicht je Teilanfrage. Taucht dieselbe AcoustID in mehreren
Teilanfragen auf, teilen sich ihre Trefferobjekte einen Eintrag in der
Zuordnung ``track_id -> Objekte`` und bekommen dieselben Metadaten — das
spart Roundtrips zur MusicBrainz-Datenbank und ist zugleich das Verhalten
des Originals.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import psycopg

from acoustid_api.matching import Match, Matcher
from acoustid_api.meta import MetaPlan, inject_metadata
from acoustid_api.params import FingerprintQuery, LookupParams, TrackQuery
from acoustid_api.store import resolve_track_gid

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from acoustid_api.service import ApiService

__all__ = ["TRACK_QUERY_SCORE", "handle_lookup"]

_LOG = logging.getLogger(__name__)

#: Ein direkt nachgeschlagener ``trackid`` ist definitionsgemaess ein
#: Volltreffer — im Original steht dort dieselbe feste 1.0.
TRACK_QUERY_SCORE = 1.0


def handle_lookup(
    connection: psycopg.Connection, service: ApiService, params: LookupParams
) -> dict[str, Any]:
    """Beantwortet einen Lookup (ohne den ``status``-Schluessel).

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        service: Laufzeitumgebung (Pipeline, MusicBrainz-Client, Config).
        params: Geprueftes Ergebnis von :func:`acoustid_api.params.parse_lookup`.

    Returns:
        ``{"results": [...]}`` bzw. ``{"fingerprints": [...]}`` — die
        Huelle mit ``status`` setzt die HTTP-Schicht.

    Raises:
        ServiceUnavailableError: Der Suchindex war nicht ansprechbar.
        InternalError: Die MusicBrainz-Abfrage ist gescheitert, obwohl der
            Spiegel erreichbar war.
    """
    queries = params.selected()
    all_matches = [_run(connection, service.matcher, query, params) for query in queries]

    # Track-ID -> alle Trefferobjekte dieser AcoustID in dieser Anfrage.
    # Bewusst ueber alle Teilanfragen hinweg (Original-Verhalten): derselbe
    # Treffer in zwei Teilanfragen kostet nur einen MB-Roundtrip.
    result_map: dict[int, list[dict[str, Any]]] = {}
    all_results = [_results(matches, result_map) for matches in all_matches]

    if params.meta and result_map:
        inject_metadata(
            connection,
            service.mb,
            MetaPlan.parse(params.meta),
            result_map,
            keep_submitted_mbid=service.config.mb.keep_submitted_mbid,
        )

    if params.batch:
        return {
            "fingerprints": [
                {"index": query.index, "results": results}
                for query, results in zip(queries, all_results, strict=True)
            ]
        }
    return {"results": all_results[0] if all_results else []}


def _run(
    connection: psycopg.Connection,
    matcher: Matcher,
    query: FingerprintQuery | TrackQuery,
    params: LookupParams,
) -> list[Match]:
    """Eine einzelne Teilanfrage beantworten."""
    if isinstance(query, TrackQuery):
        target = resolve_track_gid(connection, query.track_gid)
        if target is None:
            return []
        track_id, track_gid = target
        return [Match(track_id=track_id, track_gid=track_gid, score=TRACK_QUERY_SCORE)]
    return matcher.search(
        connection,
        query.hashes,
        query.duration,
        max_duration_diff=params.max_duration_diff,
    )


def _results(
    matches: list[Match], result_map: dict[int, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Treffer in die Antwortstruktur uebersetzen und fuer ``meta`` merken.

    Die erzeugten Objekte landen zusaetzlich in ``result_map``; der
    Metadaten-Aufbau ergaenzt sie spaeter **an Ort und Stelle**.
    """
    results: list[dict[str, Any]] = []
    for match in matches:
        result = {"id": str(match.track_gid), "score": match.score}
        result_map.setdefault(match.track_id, []).append(result)
        results.append(result)
    return results
