"""Original-Antworten von api.acoustid.org als Pruefvorlage.

Alles hier ist **woertlich** aus docs/research/phase1-api-formate.md
uebernommen (Phase-1-Recherche, Primaerquellen acoustid.org/webservice und
``github.com/acoustid/acoustid-server``). Es ist bewusst kein Code, sondern
eine Abschrift: die Tests vergleichen unsere Antworten gegen genau diese
Strukturen, und wenn sich der Vertrag je aendert, aendert sich zuerst diese
Datei.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["ERROR_EXAMPLE", "ERROR_TABLE", "LOOKUP_BATCH_EXAMPLE", "LOOKUP_OK_EXAMPLE"]

#: „Antwort (Erfolg)" aus dem Forschungsbericht.
LOOKUP_OK_EXAMPLE: Final[dict[str, Any]] = {
    "status": "ok",
    "results": [{"id": "<track-gid>", "score": 1.0}],
}

#: „Batch: {"status":"ok","fingerprints":[{"index":0,"results":[…]}]}"
#: (``index`` ist int oder null).
LOOKUP_BATCH_EXAMPLE: Final[dict[str, Any]] = {
    "status": "ok",
    "fingerprints": [{"index": 0, "results": []}],
}

#: Abschnitt „Fehlerformat" des Forschungsberichts.
ERROR_EXAMPLE: Final[dict[str, Any]] = {
    "status": "error",
    "error": {"code": 4, "message": "invalid API key"},
}

#: Die vollstaendige Tabelle „Code | HTTP | Message". Die Platzhalter stehen
#: so im Original (``<name>`` ist der Parametername, ``%f`` die Rate).
ERROR_TABLE: Final[tuple[tuple[int, int, str], ...]] = (
    (1, 400, 'unknown format "<name>"'),
    (2, 400, 'missing required parameter "<name>"'),
    (3, 400, "invalid fingerprint"),
    (4, 400, "invalid API key"),
    (5, 500, "internal error"),
    (6, 400, 'invalid user API key ("User with the API key does not exist")'),
    (7, 400, 'parameter "<name>" is not a valid UUID'),
    (8, 400, 'parameter "<name>" must be a positive integer'),
    (9, 400, 'parameter "<name>" must be a positive integer'),
    (
        10,
        400,
        'parameter "<name>" is not a valid foreign ID, it must be in the format vendor:id',
    ),
    (11, 400, 'parameter "<name>" must be between 1 and 30'),
    (12, 400, "not allowed"),
    (13, 503, "service currently unavailable, try again later"),
    (14, 429, "rate limit (%f requests per second) exceeded, try again later"),
    (15, 400, "invalid MusicBrainz access token"),
    (16, 400, "only requests over HTTPS are allowed here"),
    (17, 400, "unknown application"),
    (18, 404, "fingerprint not found"),
    (19, 413, "request too large"),
)
