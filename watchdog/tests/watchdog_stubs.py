"""Attrappen der Waechter-Tests (Phase 15).

Der Waechter spricht ab Phase 15 mit zwei Gegenstellen: dem Docker-Daemon
(Unix-Socket) und dem API-Dienst (HTTP). Beide werden hier durch
``httpx.MockTransport`` ersetzt — bewusst **nicht** durch Attrappen der
Client-Klassen: so laufen Pfadbildung, Statusauswertung und
Kopfzeilen-Behandlung der echten Module mit durch den Test.

Bewusst ein eigenes Modul und nicht die conftest.py: pytest laedt alle
`conftest`-Module unter demselben Namen, ein ``from conftest import …``
wuerde je nach Sammelreihenfolge im falschen Paket landen (gleiche
Begruendung wie in ``api/tests/stubs.py``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from acoustid_watchdog.docker import DockerClient
from acoustid_watchdog.wake import ReadinessProbe
from shared.env import EnvSettings

__all__ = [
    "FakeDaemon",
    "FakeProbe",
    "RecordingProxyTransport",
    "docker_client",
    "probe",
    "streamed",
]


def streamed(
    status_code: int, body: bytes = b"", headers: dict[str, str] | None = None
) -> httpx.Response:
    """Antwort, wie sie ein **echter** Transport liefert: als Strom.

    ``httpx.Response(content=…)`` liest den Rumpf sofort ein und markiert den
    Strom als verbraucht — der Proxy koennte ihn dann nicht mehr
    weiterstreamen. Die HTTP-Transporte von httpx geben immer einen
    ungelesenen Strom zurueck; diese Attrappe tut dasselbe, damit der
    Streaming-Pfad des Proxys wirklich durchlaufen wird.
    """
    return httpx.Response(
        status_code,
        stream=httpx.ByteStream(body),
        headers={"content-length": str(len(body)), **(headers or {})},
    )


class FakeDaemon:
    """Ein Docker-Daemon aus einem Wörterbuch von Containerzustaenden.

    Versteht genau die drei Routen, die :mod:`acoustid_watchdog.docker`
    benutzt, und merkt sich jeden Aufruf — daran laesst sich pruefen, dass
    ein Weckvorgang wirklich nur einmal startet.
    """

    def __init__(self, containers: dict[str, bool] | None = None) -> None:
        #: Containername -> laeuft gerade. Fehlt ein Name, antwortet der
        #: Daemon mit 404 (wie fuer einen nie angelegten Container).
        self.containers = dict(containers or {})
        #: ``(methode, name)`` in Aufrufreihenfolge.
        self.calls: list[tuple[str, str]] = []
        #: Aufrufe, die mit HTTP 500 beantwortet werden sollen.
        self.fail_on: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "containers":
            return httpx.Response(404, json={"message": f"unbekannt: {request.url.path}"})
        _, name, action = parts
        self.calls.append((action, name))

        if name in self.fail_on:
            return httpx.Response(500, json={"message": f"{name}: Daemon-Fehler"})
        if name not in self.containers:
            return httpx.Response(404, json={"message": f"No such container: {name}"})

        if action == "json":
            running = self.containers[name]
            return httpx.Response(
                200,
                json={
                    "Name": f"/{name}",
                    "State": {
                        "Status": "running" if running else "exited",
                        "Running": running,
                        "Health": {"Status": "healthy" if running else "unhealthy"},
                    },
                },
            )
        if action in ("start", "stop"):
            wanted = action == "start"
            if self.containers[name] == wanted:
                return httpx.Response(304)
            self.containers[name] = wanted
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unbekannte Aktion: {action}"})

    # --- Auswertung ---------------------------------------------------------

    def count(self, action: str, name: str | None = None) -> int:
        """Wie oft wurde ``action`` (optional fuer ``name``) aufgerufen?"""
        return sum(
            1
            for call_action, call_name in self.calls
            if call_action == action and (name is None or call_name == name)
        )

    @property
    def all_running(self) -> bool:
        return all(self.containers.values())


def docker_client(daemon: FakeDaemon) -> DockerClient:
    """Echter :class:`DockerClient` auf einem Attrappen-Daemon."""
    return DockerClient(client=httpx.Client(transport=httpx.MockTransport(daemon)))


class FakeProbe:
    """Bereitschaftsfrage mit steuerbarer Antwort.

    ``ready_after`` sagt, nach wie vielen Fragen der Stack als bereit gilt —
    so laesst sich ein Start simulieren, der Zeit braucht, ohne echte
    Wartezeit im Test.
    """

    def __init__(self, *, ready_after: int = 0) -> None:
        self.ready_after = ready_after
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        return self.calls > self.ready_after

    def close(self) -> None:
        pass


def probe(handler: Callable[[httpx.Request], httpx.Response]) -> ReadinessProbe:
    """Echte :class:`ReadinessProbe` auf einem Attrappen-Transport.

    Die Adresse ist der Bootstrap-Vorgabewert (``AOFF_API_HEALTH_URL``) —
    der Transport ist ohnehin eine Attrappe, aber so steht im Test dieselbe
    URL wie im Betrieb.
    """
    return ReadinessProbe(
        EnvSettings().api_health_url,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class RecordingProxyTransport:
    """API-Dienst-Attrappe fuer den Proxy: merkt sich, was ankam.

    Liefert per Vorgabe eine Lookup-Antwort; ``responder`` ersetzt das durch
    eine eigene Antwort (z. B. das nackte HTTP 405 des Batch-Endpunkts).
    """

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.responder = responder
        #: Alle empfangenen Anfragen, in Reihenfolge.
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # Der Rumpf muss hier gelesen werden, solange die Anfrage lebt.
        request.read()
        self.requests.append(request)
        if self.responder is not None:
            return self.responder(request)
        return streamed(
            200,
            json.dumps({"status": "ok", "results": []}).encode(),
            {"content-type": "application/json", "access-control-allow-origin": "*"},
        )

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def payload(self, index: int = -1) -> Any:
        """Der Rumpf einer empfangenen Anfrage als Text."""
        return self.requests[index].content.decode()
