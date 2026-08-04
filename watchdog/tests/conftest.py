"""Gemeinsame Fixtures der Waechter-Tests (Phasen 14 und 15).

Der Waechter braucht **keinen** Dienst aus dem Compose-Stack: sein ganzer
Zustand liegt in einer SQLite-Datei und einer ``config.yaml`` auf dem
Cache-Pool. Genau das ist die Invariante §8.2 („kein UI-Aufruf weckt das
Array") — und deshalb laeuft diese Testsuite vollstaendig ohne Marker,
ohne Postgres und ohne Index.

Ab Phase 15 spricht er zusaetzlich mit der Prozess-Steuerung und dem
API-Dienst. Beide Gegenstellen sind hier Attrappen (`watchdog_stubs.py`) —
**nie** der echte Socket des Entwicklerrechners: ein Unit-Test darf nicht
davon abhaengen, ob gerade ein supervisord laeuft, und schon gar nicht
fremde Prozesse anfassen. (Der Kontrakt-Test gegen ein **echtes**
supervisord steht bewusst getrennt in ``test_watchdog_supervisor.py`` und
startet sich seine eigene Gegenstelle.)

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
from watchdog_stubs import (
    FakeSupervisor,
    RecordingProxyTransport,
    controller,
    probe,
    sleeping_stack,
)

from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.main import create_app
from acoustid_watchdog.proxy import ReverseProxy
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.store import Database
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
def supervisor() -> FakeSupervisor:
    """supervisord-Attrappe mit einem schlafenden Stack."""
    return sleeping_stack()


@pytest.fixture
def upstream() -> RecordingProxyTransport:
    """API-Dienst-Attrappe hinter dem Proxy."""
    return RecordingProxyTransport()


@pytest.fixture
def service(
    env_settings: EnvSettings,
    supervisor: FakeSupervisor,
    upstream: RecordingProxyTransport,
) -> Iterator[WatchdogService]:
    """Vollstaendig gestarteter Waechter-Dienst (inkl. Erststart-Pfad).

    Steuerung und API sind Attrappen; die Bereitschaftsfrage antwortet,
    sobald alle Stack-Prozesse laufen — genau wie im Betrieb.

    Die Prozessgruppen-Steuerung ist die **echte** (nur auf einer
    Attrappen-Gegenstelle) und laeuft bewusst **ohne** Bereitschafts-Gates:
    die des Betriebs sprechen mit Postgres und HTTP-Diensten, die es hier
    nicht gibt. Was die Gates tun, prueft ``test_watchdog_stack.py``.
    """

    def health(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if supervisor.all_running else 503)

    running = WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        stack=controller(supervisor),
        probe=probe(health),
        proxy=ReverseProxy(
            env_settings.api_base_url,
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
