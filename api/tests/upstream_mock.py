"""api.acoustid.org als Attrappe — fuer alle Tests der Phase 12.

**Nie echtes Netz.** An der Stelle des Original-Dienstes steht ein
``httpx.MockTransport``; der Weiterleiter selbst ist der **echte**
:class:`~acoustid_api.upstream.UpstreamForwarder`, damit das Wire-Format
mitgeprueft wird und nicht bloss eine Absichtserklaerung.

Gewartet wird nie wirklich: :class:`Clock` ist eine Uhr, die ausschliesslich
durch das eigene ``sleep`` weiterlaeuft — Drossel und Backoff werden damit
zur pruefbaren Zahlenreihe (wie beim Downloader in Phase 6).

Bewusst ein eigenes Modul und keine ``conftest.py``: pytest laedt alle
`conftest`-Module unter demselben Namen (siehe `stubs.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import parse_qsl

import httpx

from acoustid_api.upstream import UpstreamForwarder

__all__ = [
    "APP_KEY",
    "USER_KEY",
    "Clock",
    "Handler",
    "MockUpstream",
    "error_response",
    "make_forwarder",
    "ok_response",
]

#: Unser eigener Application-Key — im Betrieb ein Secret, in den Tests der
#: Beweis, dass er weder im Log noch in ``forward_error`` landet.
APP_KEY = "GeheimerKey"

#: Der Account-Key des einreichenden Clients. Er wird unveraendert
#: durchgereicht (Zweckbindung, Phase-1-Bericht) und nie geloggt.
USER_KEY = "fremderUserKey"

Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    """Uhr, die ausschliesslich durch das eigene ``sleep`` weiterlaeuft."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        """Zeit vergehen lassen, ohne dass jemand gewartet hat."""
        self.now += seconds


def ok_response(ids: Sequence[int] = (4711,)) -> httpx.Response:
    """Die Erfolgsantwort des Originals (Phase-1-Bericht)."""
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "submissions": [{"id": value, "index": "0", "status": "pending"} for value in ids],
        },
    )


def error_response(code: int, message: str, status: int = 400) -> httpx.Response:
    """Eine Fehlerantwort im AcoustID-Format."""
    return httpx.Response(
        status, json={"status": "error", "error": {"code": code, "message": message}}
    )


class MockUpstream:
    """``httpx.MockTransport``-Handler mit Anfrageprotokoll.

    ``responses`` wird der Reihe nach abgearbeitet (ein Eintrag darf auch
    eine Exception sein — abgerissene Verbindung); danach antwortet
    ``default``, per Vorgabe mit ``ok``.
    """

    def __init__(
        self,
        responses: Sequence[httpx.Response | Exception] | None = None,
        default: Callable[[], httpx.Response] = ok_response,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = list(responses or [])
        self.default = default

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if not self.responses:
            return self.default()
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def count(self) -> int:
        return len(self.requests)

    def fields(self, position: int = 0) -> list[tuple[str, str]]:
        """Der Rumpf einer Anfrage als Paarliste (Mehrfachnamen bleiben)."""
        return parse_qsl(self.requests[position].content.decode(), keep_blank_values=True)

    def field(self, name: str, position: int = 0) -> str | None:
        for key, value in self.fields(position):
            if key == name:
                return value
        return None

    def values(self, name: str, position: int = 0) -> list[str]:
        return [value for key, value in self.fields(position) if key == name]


def make_forwarder(
    handler: Handler, clock: Clock | None = None, **kwargs: Any
) -> UpstreamForwarder:
    """Der echte Weiterleiter auf einem Mock-Transport, mit Testuhr."""
    tick = clock or Clock()
    return UpstreamForwarder(
        APP_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=tick.sleep,
        monotonic=tick.monotonic,
        **kwargs,
    )
