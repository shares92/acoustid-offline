"""Zweistufige Matching-Pipeline des Lookups (ARCHITECTURE §5.3).

Der Suchindex allein kann die Frage „welche AcoustID ist das?" nicht
beantworten: er kennt je Fingerprint nur den Query-Extrakt (120 Hashes) und
liefert als Score die reine Trefferzahl — gut genug, um aus 100 Millionen
Fingerprints ein paar Dutzend Verdaechtige zu ziehen, zu grob fuer eine
Antwort. Deshalb zwei Stufen:

**Stufe 1 — Kandidaten.** :func:`shared.fpindex.extract_query` macht aus dem
angefragten Vollvektor denselben Extrakt, den der Importer beim Indexieren
gebildet hat; ``POST /:index/_search`` liefert dazu bis zu
:data:`CANDIDATE_LIMIT` Dokument-IDs. Der Index filtert dabei selbst
(absoluter Mindestscore und 10 % des besten Treffers) — eine leere Antwort
ist der Normalfall fuer einen unbekannten Fingerprint.

**Stufe 2 — Rescoring.** Zu den Kandidaten holt :mod:`acoustid_api.store`
die Vollvektoren aus Postgres (mit Laengenfilter ``length`` ±
``maxdurationdiff``), und :func:`shared.fingerprint.compare2` rechnet den
echten AcoustID-Score. Was ueber :data:`MIN_SCORE` liegt, wird nach Score
absteigend und bei Gleichstand nach ``fingerprint.id`` aufsteigend
sortiert, auf :data:`MAX_RESULTS` gekappt und erst dann nach Track
dedupliziert.

Die Reihenfolge „erst kappen, dann deduplizieren" wirkt falsch herum, ist
aber genau das Verhalten des Originals (``LIMIT 10`` auf die
Fingerprint-Zeilen, Dedup danach im Antwortaufbau). Ein Track mit vielen
gut passenden Fingerprints kann die zehn Plaetze also fuer sich belegen;
Clients kennen das so.

**Wenn der Index nicht antwortet**, wird der Lookup zum Fehler 13/503 und
nicht etwa zu „nichts gefunden" — ein leeres Ergebnis waere von einem echten
Nicht-Treffer nicht zu unterscheiden, und der davorliegende Waechter
beantwortet 503 ohnehin schon als „gleich nochmal versuchen". Das Original
verschluckt Indexfehler an dieser Stelle still (bewusste Abweichung).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import psycopg

from acoustid_api.errors import ServiceUnavailableError
from acoustid_api.store import Candidate, load_candidates, resolve_tracks
from shared.fingerprint import MAX_OFFSET, compare2
from shared.fpindex import FpIndexClient, FpIndexError, extract_query

__all__ = [
    "CANDIDATE_LIMIT",
    "MAX_RESULTS",
    "MIN_SCORE",
    "SEARCH_TIMEOUT_MS",
    "Match",
    "Matcher",
]

_LOG = logging.getLogger(__name__)

#: Kandidaten je Suche (ARCHITECTURE §5.3: 20 bis 40). Mehr Kandidaten
#: heisst mehr Rescoring-Rechenzeit, nicht mehr Treffer — der Index sortiert
#: bereits sinnvoll vor.
CANDIDATE_LIMIT: Final = 40

#: Serverseitige Suchfrist des Index in Millisekunden (ARCHITECTURE §5.3).
SEARCH_TIMEOUT_MS: Final = 2000

#: ``TRACK_GROUP_MERGE_THRESHOLD`` — nur Treffer **oberhalb** dieses Scores
#: gelten (echtes Groesser, nicht Groesser-Gleich).
MIN_SCORE: Final = 0.4

#: ``MAX_RESULTS_PER_FINGERPRINT_QUERY``.
MAX_RESULTS: Final = 10


@dataclass(frozen=True, slots=True)
class Match:
    """Ein Treffer nach dem Rescoring.

    Attributes:
        track_id: Aufgeloeste Track-ID (Merges eingerechnet).
        track_gid: Oeffentliche AcoustID — das ``id``-Feld der Antwort.
        score: AcoustID-Score aus ``compare2``.
    """

    track_id: int
    track_gid: UUID
    score: float


class Matcher:
    """Fuehrt beide Stufen aus; haelt Index-Client und Query-Groesse.

    Args:
        index: Client des acoustid-index (Stufe 1).
        query_hashes: ``index.query_hashes`` aus der Laufzeit-Konfiguration.
            **Muss** derselbe Wert sein, mit dem der Importer indexiert hat —
            sonst passt der Extrakt der Anfrage nicht zu den Dokumenten.
        candidate_limit: Kandidatenzahl je Suche.
        search_timeout_ms: Serverseitige Frist der Indexsuche.
    """

    def __init__(
        self,
        index: FpIndexClient,
        *,
        query_hashes: int,
        candidate_limit: int = CANDIDATE_LIMIT,
        search_timeout_ms: int = SEARCH_TIMEOUT_MS,
    ) -> None:
        self.index = index
        self.query_hashes = query_hashes
        self.candidate_limit = candidate_limit
        self.search_timeout_ms = search_timeout_ms

    def search(
        self,
        connection: psycopg.Connection,
        hashes: tuple[int, ...],
        duration: int,
        *,
        max_duration_diff: int,
    ) -> list[Match]:
        """Sucht die AcoustIDs zu einem Fingerprint.

        Args:
            connection: Verbindung zur AcoustID-Postgres.
            hashes: Vollvektor der Anfrage (u32).
            duration: Vom Client gemeldete Laenge in Sekunden.
            max_duration_diff: Zugelassene Laengenabweichung in Sekunden.

        Returns:
            Bis zu :data:`MAX_RESULTS` Treffer, absteigend nach Score und je
            Track nur einmal. Leer, wenn nichts ueber :data:`MIN_SCORE` liegt.

        Raises:
            ServiceUnavailableError: Der Suchindex war nicht ansprechbar.
        """
        query = extract_query(hashes, max_hashes=self.query_hashes)
        if not query:
            # Nur Stille — daraus laesst sich nichts nachschlagen.
            _LOG.info("Fingerprint ohne indexierbare Hashes", extra={"length": len(hashes)})
            return []

        try:
            hits = self.index.search(
                query, limit=self.candidate_limit, timeout_ms=self.search_timeout_ms
            )
        except FpIndexError as exc:
            _LOG.warning("Indexsuche fehlgeschlagen", extra={"error": str(exc)})
            raise ServiceUnavailableError() from exc
        if not hits:
            return []

        candidates = load_candidates(
            connection,
            [hit.doc_id for hit in hits],
            duration=duration,
            max_duration_diff=max_duration_diff,
        )
        scored = self._rescore(candidates, hashes)
        _LOG.debug(
            "Lookup gerechnet",
            extra={
                "query_hashes": len(query),
                "candidates": len(hits),
                "in_length_window": len(candidates),
                "above_cutoff": len(scored),
            },
        )
        return self._resolve(connection, scored)

    def _rescore(
        self, candidates: list[Candidate], hashes: tuple[int, ...]
    ) -> list[tuple[Candidate, float]]:
        """Score je Kandidat, sortiert und auf :data:`MAX_RESULTS` gekappt."""
        scored = [
            (candidate, score)
            for candidate in candidates
            # Argumentreihenfolge wie im Original-SQL:
            # acoustid_compare2(fingerprint, query, max_offset).
            if (score := compare2(candidate.hashes, hashes, MAX_OFFSET)) > MIN_SCORE
        ]
        scored.sort(key=lambda item: (-item[1], item[0].fingerprint_id))
        return scored[:MAX_RESULTS]

    @staticmethod
    def _resolve(
        connection: psycopg.Connection, scored: list[tuple[Candidate, float]]
    ) -> list[Match]:
        """Track-IDs aufloesen und je Track nur den besten Treffer behalten."""
        if not scored:
            return []
        resolved = resolve_tracks(connection, [candidate.track_id for candidate, _ in scored])
        matches: list[Match] = []
        seen: set[int] = set()
        for candidate, score in scored:
            target = resolved.get(candidate.track_id)
            if target is None:
                continue
            track_id, track_gid = target
            if track_id in seen:
                continue
            seen.add(track_id)
            matches.append(Match(track_id=track_id, track_gid=track_gid, score=score))
        return matches
