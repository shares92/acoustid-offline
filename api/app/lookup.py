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

``meta`` bleibt in dieser Phase ohne Wirkung: Metadaten aus der
MusicBrainz-Spiegel-Datenbank kommen in Phase 10. Der Parameter wird
angenommen (Picard und beets schicken ihn immer mit) und protokolliert.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from acoustid_api.matching import Match, Matcher
from acoustid_api.params import FingerprintQuery, LookupParams, TrackQuery
from acoustid_api.store import resolve_track_gid

__all__ = ["TRACK_QUERY_SCORE", "handle_lookup"]

_LOG = logging.getLogger(__name__)

#: Ein direkt nachgeschlagener ``trackid`` ist definitionsgemaess ein
#: Volltreffer — im Original steht dort dieselbe feste 1.0.
TRACK_QUERY_SCORE = 1.0


def handle_lookup(
    connection: psycopg.Connection, matcher: Matcher, params: LookupParams
) -> dict[str, Any]:
    """Beantwortet einen Lookup (ohne den ``status``-Schluessel).

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        matcher: Die zweistufige Pipeline.
        params: Geprueftes Ergebnis von :func:`acoustid_api.params.parse_lookup`.

    Returns:
        ``{"results": [...]}`` bzw. ``{"fingerprints": [...]}`` — die
        Huelle mit ``status`` setzt die HTTP-Schicht.

    Raises:
        ServiceUnavailableError: Der Suchindex war nicht ansprechbar.
    """
    if params.meta:
        _LOG.debug(
            "meta wird in dieser Phase nicht ausgewertet (Phase 10)",
            extra={"meta": list(params.meta)},
        )

    queries = params.selected()
    all_matches = [_run(connection, matcher, query, params) for query in queries]

    if params.batch:
        return {
            "fingerprints": [
                {"index": query.index, "results": _results(matches)}
                for query, matches in zip(queries, all_matches, strict=True)
            ]
        }
    return {"results": _results(all_matches[0]) if all_matches else []}


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


def _results(matches: list[Match]) -> list[dict[str, Any]]:
    """Treffer in die Antwortstruktur uebersetzen."""
    return [{"id": str(match.track_gid), "score": match.score} for match in matches]
