"""Gemeinsame Fixtures der Waechter-Tests (Phasen 14 und 15).

Der Waechter braucht **keinen** Dienst aus dem Compose-Stack: sein ganzer
Zustand liegt in einer SQLite-Datei und einer ``config.yaml`` auf dem
Cache-Pool. Genau das ist die Invariante §8.2 („kein UI-Aufruf weckt das
Array") — und deshalb laeuft diese Testsuite vollstaendig ohne Marker,
ohne Postgres und ohne Index.

Ab Phase 15 spricht er zusaetzlich mit dem Docker-Daemon und dem
API-Dienst. Beide Gegenstellen sind hier Attrappen auf einem
``httpx.MockTransport`` (`stubs.py`) — **nie** der echte Socket des
Entwicklerrechners: ein Unit-Test darf nicht davon abhaengen, ob gerade
Docker laeuft, und schon gar nicht Container anfassen.

Jeder Test bekommt ein frisches Datenverzeichnis unter ``tmp_path``; die
``AOFF_``-Umgebung wird nie gelesen, sondern als
:class:`~shared.env.EnvSettings` direkt gebaut — so haengt kein Ergebnis an
der Umgebung des Entwicklerrechners.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from watchdog_stubs import FakeDaemon, RecordingProxyTransport, docker_client, probe

from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.main import create_app
from acoustid_watchdog.proxy import ReverseProxy
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.store import Database
from acoustid_watchdog.wake import API_BASE_URL, STACK_CONTAINERS
from shared.env import EnvSettings


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Datenverzeichnis des Waechters (im Betrieb: der Cache-Pool)."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def env_settings(data_dir: Path) -> EnvSettings:
    """Bootstrap-Werte wie im Betrieb — nur auf ein Wegwerf-Verzeichnis."""
    return EnvSettings(data_dir=data_dir)


@pytest.fixture
def db(data_dir: Path) -> Iterator[Database]:
    """Frisch angelegte, migrierte Zustandsdatenbank."""
    with Database.for_data_dir(data_dir) as database:
        yield database


@pytest.fixture
def config_store(env_settings: EnvSettings) -> ConfigStore:
    """Config-Store auf dem Pfad aus den Bootstrap-Werten."""
    return ConfigStore.from_path(env_settings.config_path)


@pytest.fixture
def daemon() -> FakeDaemon:
    """Docker-Daemon-Attrappe mit einem schlafenden Stack."""
    return FakeDaemon(dict.fromkeys(STACK_CONTAINERS, False))


@pytest.fixture
def upstream() -> RecordingProxyTransport:
    """API-Dienst-Attrappe hinter dem Proxy."""
    return RecordingProxyTransport()


@pytest.fixture
def service(
    env_settings: EnvSettings,
    daemon: FakeDaemon,
    upstream: RecordingProxyTransport,
) -> Iterator[WatchdogService]:
    """Vollstaendig gestarteter Waechter-Dienst (inkl. Erststart-Pfad).

    Docker und API sind Attrappen; die Bereitschaftsfrage antwortet, sobald
    alle Stack-Container laufen — genau wie im Betrieb.
    """

    def health(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if daemon.all_running else 503)

    running = WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        docker=docker_client(daemon),
        probe=probe(health),
        proxy=ReverseProxy(
            API_BASE_URL,
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        ),
    )
    with running:
        yield running


@pytest.fixture
def client(service: WatchdogService) -> Iterator[TestClient]:
    """Testclient auf die echte App mit dem gestarteten Dienst."""
    with TestClient(create_app(service)) as test_client:
        yield test_client
