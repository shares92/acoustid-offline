"""``POST /v2/lookup/batch`` — viele Fingerprints, ein Weckvorgang (Phase 13).

Der einzige **eigene** Endpunkt des API-Dienstes (ARCHITECTURE §7 „Eigene
Endpoints"): api.acoustid.org kennt ihn nicht. Es gibt hier also kein
Vorbild, das man bug-fuer-bug nachbauen muesste — wohl aber einen Vertrag,
zu dem er passen muss, naemlich den eigenen Lookup. Deshalb gilt in jedem
Eintrag dieselbe Grammatik, dieselbe Pruefreihenfolge und dieselbe
Fehlertabelle wie in ``/v2/lookup`` (:mod:`acoustid_api.params`).

**Wozu er da ist.** Diese Instanz schlaeft im Normalfall. Wer 300 Dateien
taggen will, weckt sie mit dem Original-Batchprotokoll (max. 20
Fingerprints je Anfrage) fuenfzehnmal an; mit diesem Endpunkt dreimal. Das
ist der ganze Zweck: **eine** Anfrage, **ein** Weckvorgang, **ein** Bundel
MusicBrainz-Abfragen.

Anfrage — ein Objekt mit dem Pflichtfeld ``queries``::

    {
        "client": "…",
        "meta": "recordings sources",
        "queries": [{"fingerprint": "…", "duration": 241}, {"trackid": "…", "meta": "releases"}],
    }

Antwort — ein Array in **gleicher Reihenfolge**, je Eintrag eine vollstaendige
AcoustID-Antwort::

    {
        "status": "ok",
        "responses": [
            {"index": 0, "status": "ok", "results": [{"id": "…", "score": 0.98}]},
            {"index": 1, "status": "error", "error": {"code": 3, "message": "invalid fingerprint"}},
        ],
    }

**Ein kaputter Eintrag reisst die anderen nicht.** Alles, was ein Eintrag
selbst falsch machen kann (fehlende Laenge, unlesbarer Fingerprint, kaputte
``trackid``, ``maxdurationdiff`` ausserhalb 1…30), wird zu seiner eigenen
Fehlerantwort; die Gesamtantwort bleibt HTTP 200. Umgekehrt gilt: was der
**Anfrage** fehlt (``client``, ``queries``, Rumpfgrenze, mehr als 100
Eintraege), beendet sie ganz — im gewohnten Fehlerformat mit dem gewohnten
HTTP-Status.

**Gemeinsame Betriebsmittel gehoeren nicht einem Eintrag.** Antwortet der
Suchindex nicht, ist das kein Fehler des dritten Eintrags, sondern der
Anfrage: es kommt Fehler 13 / HTTP 503 fuer alles (dieselbe laute Absage wie
im Lookup, DECISIONS „Phase-9-Lookup-Details"). Genauso wird ein
gescheiterter MusicBrainz-Zugriff zu Fehler 5 / HTTP 500. Eine halb
beantwortete Batch-Antwort, in der die uebrigen Eintraege wie „kein Treffer"
aussehen, waere schlimmer als gar keine.

**Metadaten in einem Bundel.** Die Eintraege werden nach ihrem ausgewerteten
``meta``-Plan gruppiert; je Plan laeuft :func:`~acoustid_api.meta.inject_metadata`
**einmal** ueber die Trefferobjekte aller Eintraege dieser Gruppe. Schicken
alle hundert Eintraege dasselbe ``meta`` — der Normalfall, Clients setzen es
einmal —, dann kostet die ganze Anfrage genau ein Bundel MB-Abfragen statt
hundert. Dieselbe AcoustID in mehreren Eintraegen kostet dabei nichts extra:
ihre Trefferobjekte teilen sich einen Eintrag in der Zuordnung
``track_id -> Objekte``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import psycopg

from acoustid_api.errors import InternalError, error_payload
from acoustid_api.lookup import build_results, run_query
from acoustid_api.meta import MetaPlan, inject_metadata
from acoustid_api.params import BatchParams

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from acoustid_api.service import ApiService

__all__ = ["handle_lookup_batch"]

_LOG = logging.getLogger(__name__)


def handle_lookup_batch(
    connection: psycopg.Connection, service: ApiService, params: BatchParams
) -> dict[str, Any]:
    """Beantwortet einen Batch-Lookup (ohne den aeusseren ``status``-Schluessel).

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        service: Laufzeitumgebung (Pipeline, MusicBrainz-Client, Config).
        params: Ergebnis von :func:`acoustid_api.params.parse_lookup_batch` —
            je Eintrag entweder eine Teilanfrage oder ihr Fehler.

    Returns:
        ``{"responses": [...]}`` in Anfragereihenfolge; die Huelle mit
        ``status`` setzt die HTTP-Schicht.

    Raises:
        ServiceUnavailableError: Der Suchindex war nicht ansprechbar — das
            betrifft die ganze Anfrage, nicht einen Eintrag.
        InternalError: Die MusicBrainz-Abfrage ist gescheitert, obwohl der
            Spiegel erreichbar war.
    """
    responses: list[dict[str, Any]] = []
    # Plan -> Track-ID -> Trefferobjekte aller Eintraege mit diesem Plan.
    # Genau diese Zuordnung ist das „ein Bundel statt hundert Roundtrips".
    plans: dict[MetaPlan, dict[int, list[dict[str, Any]]]] = {}

    for entry in params.entries:
        if entry.query is None:
            # Invariante von BatchEntry: ohne Teilanfrage steht der Fehler
            # fest. Der Rueckfall ist reine Vorsicht — er haelt die
            # Reihenfolge auch dann, wenn diese Invariante je bricht.
            error = entry.error or InternalError()
            responses.append({"index": entry.index, **error_payload(error)})
            continue
        matches = run_query(
            connection,
            service.matcher,
            entry.query,
            max_duration_diff=entry.max_duration_diff,
        )
        result_map = plans.setdefault(MetaPlan.parse(entry.meta), {})
        responses.append(
            {
                "index": entry.index,
                "status": "ok",
                "results": build_results(matches, result_map),
            }
        )

    for plan, result_map in plans.items():
        if result_map:
            inject_metadata(
                connection,
                service.mb,
                plan,
                result_map,
                keep_submitted_mbid=service.config.mb.keep_submitted_mbid,
            )

    _LOG.info(
        "Batch-Lookup beantwortet",
        extra={
            "queries": len(params.entries),
            "failed": sum(1 for entry in params.entries if entry.query is None),
            "meta_plans": sum(1 for result_map in plans.values() if result_map),
            "client": params.client,
            "client_version": params.client_version,
        },
    )
    return {"responses": responses}
