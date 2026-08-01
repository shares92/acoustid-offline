"""Docker-Steuerung ueber den Unix-Socket (Phase 15).

Der Codepfad mit der groessten Tragweite im ganzen Projekt — wer den Socket
hat, hat den Host. Diese Tests halten fest, dass er genau drei Dinge tut und
jede Antwort des Daemons richtig einordnet: „lief schon" ist kein Fehler,
„gibt es nicht" ist einer, und ein stummer Socket ist ein dritter.
"""

from __future__ import annotations

import httpx
import pytest
from watchdog_stubs import FakeDaemon, docker_client

from acoustid_watchdog.docker import (
    DOCKER_SOCKET,
    ContainerNotFoundError,
    DockerClient,
    DockerError,
    DockerUnavailableError,
)


def test_inspect_reports_running_state() -> None:
    client = docker_client(FakeDaemon({"acoustid-db": True}))

    state = client.inspect("acoustid-db")

    assert state.name == "acoustid-db"
    assert state.running is True
    assert state.status == "running"
    assert state.health == "healthy"


def test_inspect_reports_stopped_state() -> None:
    client = docker_client(FakeDaemon({"acoustid-db": False}))

    assert client.inspect("acoustid-db").running is False


def test_start_is_idempotent() -> None:
    """204 = dieser Aufruf hat gestartet, 304 = lief schon.

    Der Unterschied zaehlt: ein Weckvorgang darf mehrfach ausgeloest werden,
    ohne dass daraus ein Fehler wird.
    """
    daemon = FakeDaemon({"acoustid-api": False})
    client = docker_client(daemon)

    assert client.start("acoustid-api") is True
    assert client.start("acoustid-api") is False
    assert daemon.containers["acoustid-api"] is True


def test_stop_is_idempotent() -> None:
    daemon = FakeDaemon({"acoustid-api": True})
    client = docker_client(daemon)

    assert client.stop("acoustid-api") is True
    assert client.stop("acoustid-api") is False
    assert daemon.containers["acoustid-api"] is False


def test_stop_sends_the_grace_period() -> None:
    """Die Abschaltfrist geht als Parameter ``t`` an den Daemon.

    Dockers Vorgabe von 10 s ist der Postgres zu knapp (Hinweis aus
    Phase 8); der Wert muss deshalb wirklich auf der Leitung landen.
    """
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(204)

    client = DockerClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.stop("acoustid-db", timeout_s=45)

    assert seen[0].params["t"] == "45"


def test_unknown_container_is_a_dedicated_error() -> None:
    """404 heisst: der Stack wurde nie angelegt — der Waechter erzeugt nichts."""
    client = docker_client(FakeDaemon())

    with pytest.raises(ContainerNotFoundError, match="No such container"):
        client.start("acoustid-db")


def test_daemon_error_carries_the_message() -> None:
    daemon = FakeDaemon({"acoustid-db": False})
    daemon.fail_on.add("acoustid-db")
    client = docker_client(daemon)

    with pytest.raises(DockerError, match="Daemon-Fehler"):
        client.start("acoustid-db")


def test_silent_socket_is_reported_as_unavailable() -> None:
    """Kein Mount, kein Daemon — das ist ein Wirtsfehler, kein Containerfehler."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = DockerClient(client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(DockerUnavailableError, match="keine Antwort vom Docker-Daemon"):
        client.inspect("acoustid-db")


def test_socket_path_is_the_documented_one() -> None:
    """Der Pfad ist Vertrag mit docker-compose.watchdog.yml."""
    assert DOCKER_SOCKET == "/var/run/docker.sock"
    assert DockerClient(client=httpx.Client()).socket_path == DOCKER_SOCKET
