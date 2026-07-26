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

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

from acoustid_api.matching import Match
from shared.config import Config
from shared.fpindex import Change

__all__ = [
    "FakeDb",
    "StubConnection",
    "StubCursor",
    "StubIndex",
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

    Wer doch einzelne Anweisungen unterscheiden muss — der Submit schreibt und
    braucht IDs zurueck —, gibt einen ``handler`` mit: er bekommt Anweisung und
    Parameter und liefert Zeilen (oder ``None``, dann gelten die
    Vorgabezeilen).
    """

    def __init__(
        self,
        rows: Sequence[tuple[Any, ...]] = (),
        handler: Callable[[str, Any], Sequence[tuple[Any, ...]] | None] | None = None,
    ) -> None:
        self.rows = list(rows)
        self.queries: list[str] = []
        self.handler = handler

    def execute(self, query: Any, params: Any = None) -> StubCursor:
        self.queries.append(str(query))
        if self.handler is not None:
            handled = self.handler(str(query), params)
            if handled is not None:
                return StubCursor(handled)
        return StubCursor(self.rows)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Der Submit schreibt in einer Transaktion — hier ohne Wirkung."""
        yield


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


class StubIndex:
    """Index-Attrappe: nimmt Batches entgegen oder wirft.

    ``batches`` haelt fest, was geschickt wurde — so laesst sich pruefen, dass
    die Dokument-ID aus dem reservierten Bereich stammt und die Hashes der
    Query-Extraktion entsprechen.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.batches: list[list[Change]] = []
        self.error = error

    def update(
        self,
        changes: Any,
        *,
        metadata: Mapping[str, str] | None = None,
        expected_version: int | None = None,
    ) -> int:
        if self.error is not None:
            raise self.error
        self.batches.append(list(changes))
        return len(self.batches)

    @property
    def doc_ids(self) -> list[int]:
        """Alle je geschickten Dokument-IDs, in Reihenfolge."""
        return [change.doc_id for batch in self.batches for change in batch]

    def close(self) -> None:
        pass


class StubService:
    """Steht an der Stelle von :class:`acoustid_api.service.ApiService`."""

    def __init__(
        self,
        matcher: StubMatcher | None = None,
        connection: StubConnection | None = None,
        mb: Any = None,
        config: Config | None = None,
        index: StubIndex | None = None,
        upstream: Any = None,
    ) -> None:
        self.matcher = matcher or StubMatcher()
        self.connection = connection or StubConnection()
        self.pool = StubPool(self.connection)
        self.config = config or Config()
        #: Suchindex; der Submit traegt seine Einreichungen dort nach.
        self.index = index or StubIndex()
        #: MusicBrainz-Client; ``None`` = nicht konfiguriert, also der
        #: degradierte Betrieb aus Invariante §8.7.
        self.mb = mb
        #: Upstream-Weiterleiter (Phase 12). Bewusst **ohne** Vorgabe aus der
        #: Config: die Tests bringen einen echten
        #: :class:`acoustid_api.upstream.UpstreamForwarder` auf einem
        #: ``httpx.MockTransport`` mit, damit das Wire-Format mitgeprueft
        #: wird — nie eine Attrappe des HTTP-Verkehrs.
        self.upstream = upstream

    def close(self) -> None:
        pass


def make_match(score: float = 1.0, track_id: int = 1, gid: UUID | None = None) -> Match:
    """Ein Treffer, wie ihn die Pipeline liefern wuerde."""
    return Match(track_id=track_id, track_gid=gid or uuid4(), score=score)


class FakeDb:
    """``local_submission`` als Liste von Zeilen im Arbeitsspeicher.

    Versteht genau die Anweisungen, die :mod:`acoustid_api.store` fuer den
    Submit-Schreibpfad (Phase 11) und die Upstream-Warteschlange (Phase 12)
    absetzt — unterschieden am Wortlaut, so wie ihn das Modul formuliert.
    Damit laufen Statusmaschine, Arbeitsvorrat und die 7-Fehler-Grenze ohne
    einen einzigen Dienst; dass die **echten** Anweisungen dasselbe tun,
    zeigen die Integrationstests.

    Als ``handler`` einer :class:`StubConnection` einsetzen::

        db = FakeDb()
        service = StubService(connection=StubConnection(handler=db))
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._track_ids = 0
        self._row_ids = 0

    # --- Anweisungen -------------------------------------------------------

    def __call__(self, query: str, params: Any) -> Sequence[tuple[Any, ...]] | None:
        statement = query.strip()
        if "nextval" in statement:
            self._track_ids += 1
            return [(self._track_ids,)]
        if statement.startswith("INSERT INTO local_submission"):
            return self._insert(params)
        if statement.startswith("UPDATE local_submission"):
            return self._update(statement, params)
        if statement.startswith("SELECT DISTINCT ON (local_track_id)"):
            if "'new'" in statement:
                return self._pending(params)
            return self._forward_queue(params)
        if statement.startswith("SELECT local_track_id, mbid::text"):
            return self._forward_mbids(params)
        if statement.startswith("SELECT id, status, local_track_gid"):
            return self._states(params)
        return None

    def _insert(self, params: Any) -> Sequence[tuple[Any, ...]]:
        self._row_ids += 1
        self.rows.append(
            {
                **params,
                "id": self._row_ids,
                "status": "new",
                "submitted_by": params.get("user"),
                "forward_attempts": 0,
                "forward_error": None,
                "forwarded_at": None,
            }
        )
        return [(self._row_ids,)]

    def _update(self, statement: str, params: Any) -> Sequence[tuple[Any, ...]]:
        # Unterschieden wird an der SET-Klausel: `status = 'forward_failed'`
        # steht auch in der WHERE-Bedingung des Zuruecksetzens.
        if "SET status = 'indexed'" in statement:
            for row in self.rows:
                if row["status"] == "new" and row["local_track_id"] in params["ids"]:
                    row["status"] = "indexed"
            return []
        if "SET status = 'forwarded'" in statement:
            return [
                (row["id"],)
                for row in self._group(params["id"])
                if _switch(row, "forwarded", forward_error=None, forwarded_at="jetzt")
            ]
        if "SET status = 'forward_failed'" in statement:
            changed: list[tuple[Any, ...]] = []
            for row in self._group(params["id"]):
                if row["status"] not in ("indexed", "forward_failed"):
                    continue
                row["status"] = "forward_failed"
                row["forward_attempts"] += 1
                row["forward_error"] = params["error"]
                changed.append((row["forward_attempts"],))
            return changed
        if "SET forward_attempts = 0" in statement:
            only = params["only"]
            reset: list[tuple[Any, ...]] = []
            for row in self.rows:
                if row["status"] != "forward_failed":
                    continue
                if row["forward_attempts"] < params["min_attempts"]:
                    continue
                if only is not None and row["local_track_id"] not in only:
                    continue
                row["forward_attempts"] = 0
                row["forward_error"] = None
                reset.append((row["local_track_id"],))
            return reset
        raise AssertionError(f"unbekannte UPDATE-Anweisung: {statement}")

    def _pending(self, params: Any) -> Sequence[tuple[Any, ...]]:
        found = [
            (row["local_track_id"], row["fingerprint"])
            for row in self._heads()
            if row["status"] == "new"
        ]
        return found[: params["limit"]]

    def _forward_queue(self, params: Any) -> Sequence[tuple[Any, ...]]:
        only = params["only"]
        found: list[tuple[Any, ...]] = []
        for row in self._heads():
            waiting = row["status"] == "indexed" or (
                row["status"] == "forward_failed"
                and row["forward_attempts"] < params["max_attempts"]
            )
            if not waiting or (only is not None and row["local_track_id"] not in only):
                continue
            found.append(
                (
                    row["local_track_id"],
                    row["fingerprint"],
                    row["length"],
                    row.get("bitrate"),
                    row.get("fileformat"),
                    row.get("puid"),
                    row.get("foreignid"),
                    row.get("track"),
                    row.get("artist"),
                    row.get("album"),
                    row.get("album_artist"),
                    row.get("track_no"),
                    row.get("disc_no"),
                    row.get("year"),
                    row.get("submitted_by"),
                    row["forward_attempts"],
                )
            )
        return found[: params["limit"]]

    def _forward_mbids(self, params: Any) -> Sequence[tuple[Any, ...]]:
        return [
            (row["local_track_id"], row["mbid"])
            for row in sorted(self.rows, key=lambda item: (item["local_track_id"], item["id"]))
            if row["local_track_id"] in params["ids"] and row.get("mbid")
        ]

    def _states(self, params: Any) -> Sequence[tuple[Any, ...]]:
        """``/v2/submission_status`` (Phase 13): Zeile, nicht Gruppe."""
        return [
            (row["id"], row["status"], row["local_track_gid"])
            for row in self.rows
            if row["id"] in params["ids"]
        ]

    # --- Sicht auf die Zeilen ----------------------------------------------

    def _group(self, local_track_id: int) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["local_track_id"] == local_track_id]

    def _heads(self) -> list[dict[str, Any]]:
        """Je Gruppe die aelteste Zeile (``DISTINCT ON``), nach Gruppe sortiert."""
        heads: dict[int, dict[str, Any]] = {}
        for row in sorted(self.rows, key=lambda item: item["id"]):
            heads.setdefault(row["local_track_id"], row)
        return [heads[key] for key in sorted(heads)]

    @property
    def status_by_track(self) -> dict[int, set[str]]:
        result: dict[int, set[str]] = {}
        for row in self.rows:
            result.setdefault(row["local_track_id"], set()).add(row["status"])
        return result

    @property
    def attempts_by_track(self) -> dict[int, int]:
        return {row["local_track_id"]: row["forward_attempts"] for row in self.rows}

    @property
    def errors_by_track(self) -> dict[int, str | None]:
        return {row["local_track_id"]: row["forward_error"] for row in self.rows}

    def add(self, **fields: Any) -> dict[str, Any]:
        """Eine Zeile direkt anlegen — fuer vorbereitete Ausgangslagen."""
        self._row_ids += 1
        self._track_ids = max(self._track_ids, fields.get("local_track_id", 0))
        row = {
            "id": self._row_ids,
            "local_track_id": fields.get("local_track_id", self._row_ids),
            "local_track_gid": fields.get("local_track_gid", uuid4()),
            "status": "indexed",
            "fingerprint": fields.get("fingerprint", [1, 2, 3]),
            "length": fields.get("length", 241),
            "submitted_by": fields.get("submitted_by", "usertestkey"),
            "forward_attempts": 0,
            "forward_error": None,
            "forwarded_at": None,
            **fields,
        }
        self.rows.append(row)
        return row


def _switch(row: dict[str, Any], status: str, **updates: Any) -> bool:
    """Statuswechsel nur aus einem gueltigen Vorzustand (wie im SQL)."""
    if row["status"] not in ("indexed", "forward_failed"):
        return False
    row["status"] = status
    row.update(updates)
    return True
