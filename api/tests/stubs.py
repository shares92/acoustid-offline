"""Attrappen fuer die API-Tests ohne Datenbank und ohne Suchindex.

Die HTTP-Schicht (Rumpf-Grenzen, gzip, Formate, Fehlerabbildung) laesst sich
vollstaendig ohne echte Dienste pruefen — dafuer stehen hier ein Matcher, der
vorbereitete Treffer liefert, und ein Verbindungs-Pool, dessen Verbindung auf
jede Abfrage vorbereitete Zeilen zurueckgibt.

Bewusst ein eigenes Modul und nicht die conftest.py: pytest laedt alle
`conftest`-Module unter demselben Namen, ein ``from conftest import …`` wuerde
je nach Sammelreihenfolge im falschen Paket landen.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

from acoustid_api.matching import Match
from shared.config import Config

__all__ = [
    "StubConnection",
    "StubCursor",
    "StubMatcher",
    "StubPool",
    "StubService",
    "make_match",
]


class StubCursor:
    """Ergebnis eines Attrappen-``execute``."""

    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class StubConnection:
    """Verbindung, die auf jede Abfrage dieselben Zeilen liefert.

    Reicht fuer die HTTP-Tests: dort geht es nie um SQL, sondern um das, was
    davor und danach passiert. Die echten Abfragen pruefen die
    Integrationstests.
    """

    def __init__(self, rows: Sequence[tuple[Any, ...]] = ()) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []

    def execute(self, query: Any, params: Any = None) -> StubCursor:
        self.queries.append(str(query))
        return StubCursor(self.rows)


class StubPool:
    """Pool-Attrappe mit genau einer Verbindung."""

    def __init__(self, connection: StubConnection) -> None:
        self._connection = connection

    @contextmanager
    def connection(self) -> Iterator[StubConnection]:
        yield self._connection

    def close(self) -> None:
        pass


class StubMatcher:
    """Matcher-Attrappe: liefert vorbereitete Treffer oder wirft.

    ``calls`` haelt fest, womit gesucht wurde — so laesst sich pruefen, dass
    Vektorlaenge, ``duration`` und ``maxdurationdiff`` unveraendert ankommen.
    """

    def __init__(self, matches: Sequence[Match] = (), error: Exception | None = None) -> None:
        self.matches = list(matches)
        self.error = error
        self.calls: list[tuple[int, int, int]] = []

    def search(
        self,
        connection: Any,
        hashes: tuple[int, ...],
        duration: int,
        *,
        max_duration_diff: int,
    ) -> list[Match]:
        self.calls.append((len(hashes), duration, max_duration_diff))
        if self.error is not None:
            raise self.error
        return list(self.matches)


class StubService:
    """Steht an der Stelle von :class:`acoustid_api.service.ApiService`."""

    def __init__(
        self,
        matcher: StubMatcher | None = None,
        connection: StubConnection | None = None,
        mb: Any = None,
        config: Config | None = None,
    ) -> None:
        self.matcher = matcher or StubMatcher()
        self.connection = connection or StubConnection()
        self.pool = StubPool(self.connection)
        self.config = config or Config()
        #: MusicBrainz-Client; ``None`` = nicht konfiguriert, also der
        #: degradierte Betrieb aus Invariante §8.7.
        self.mb = mb

    def close(self) -> None:
        pass


def make_match(score: float = 1.0, track_id: int = 1, gid: UUID | None = None) -> Match:
    """Ein Treffer, wie ihn die Pipeline liefern wuerde."""
    return Match(track_id=track_id, track_gid=gid or uuid4(), score=score)
