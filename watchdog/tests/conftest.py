"""Gemeinsame Fixtures der Waechter-Tests (Phase 14).

Der Waechter braucht **keinen** Dienst aus dem Compose-Stack: sein ganzer
Zustand liegt in einer SQLite-Datei und einer ``config.yaml`` auf dem
Cache-Pool. Genau das ist die Invariante §8.2 („kein UI-Aufruf weckt das
Array") — und deshalb laeuft diese Testsuite vollstaendig ohne Marker,
ohne Postgres und ohne Index.

Jeder Test bekommt ein frisches Datenverzeichnis unter ``tmp_path``; die
``AOFF_``-Umgebung wird nie gelesen, sondern als
:class:`~shared.env.EnvSettings` direkt gebaut — so haengt kein Ergebnis an
der Umgebung des Entwicklerrechners.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.main import create_app
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
def service(env_settings: EnvSettings) -> Iterator[WatchdogService]:
    """Vollstaendig gestarteter Waechter-Dienst (inkl. Erststart-Pfad)."""
    with WatchdogService.from_env(env_settings) as running:
        yield running


@pytest.fixture
def client(service: WatchdogService) -> Iterator[TestClient]:
    """Testclient auf die echte App mit dem gestarteten Dienst."""
    with TestClient(create_app(service)) as test_client:
        yield test_client
