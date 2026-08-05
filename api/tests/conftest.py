"""Gemeinsame Fixtures der API-Tests (Phase 9).

Zwei Welten, die sich nicht vermischen sollen:

* **Ohne Dienste.** Die HTTP-Schicht (Rumpf-Grenzen, gzip, Formate,
  Fehlerabbildung) laesst sich vollstaendig ohne Datenbank und ohne Index
  pruefen; die Attrappen dazu stehen in `stubs.py`.
* **Mit Diensten.** Der Nachweis, dass ein eingespielter Fingerprint auch
  wiedergefunden wird, braucht beide echten Dienste; diese Tests tragen die
  Marker ``integration`` + ``db`` + ``index`` und stehen in
  `test_lookup_integration.py`. Sie bekommen eine **frisch angelegte,
  leere** Datenbank mit vollstaendigem Schema — die konfigurierte Datenbank
  aus den `MMO_DB_*`-Variablen dient nur zum Anlegen und Loeschen.

Steuerung ueber `--integration` bzw. `ACOUSTID_INTEGRATION_TESTS`, siehe
conftest.py im Repo-Wurzelverzeichnis.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from stubs import StubMatcher, StubService

from acoustid_api.main import create_app
from shared.db import apply
from shared.env import EnvSettings

# --- Ohne Dienste -----------------------------------------------------------


@pytest.fixture
def matcher() -> StubMatcher:
    return StubMatcher()


@pytest.fixture
def service(matcher: StubMatcher) -> StubService:
    return StubService(matcher)


@pytest.fixture
def client(service: StubService) -> Iterator[TestClient]:
    """Testclient auf die echte App mit dem Attrappen-Dienst."""
    with TestClient(create_app(service)) as test_client:  # type: ignore[arg-type]
        yield test_client


# --- Mit Diensten -----------------------------------------------------------


@pytest.fixture(scope="session")
def env_settings() -> EnvSettings:
    """Zugang aus den `MMO_`-Variablen — derselbe Weg wie im Betrieb."""
    return EnvSettings.from_env()


@pytest.fixture
def scratch_env(env_settings: EnvSettings) -> Iterator[EnvSettings]:
    """Frisch angelegte, migrierte Datenbank — als vollstaendige Umgebung.

    Der API-Dienst baut seinen Pool aus genau diesen Werten; deshalb wird
    hier die ganze :class:`EnvSettings` durchgereicht und nicht nur eine
    Verbindung.
    """
    name = f"aoff_api_{uuid4().hex[:12]}"
    admin_dsn = env_settings.db_dsn().get_secret_value()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    scratch = env_settings.model_copy(update={"db_name": name})
    try:
        with psycopg.connect(scratch.db_dsn().get_secret_value(), autocommit=True) as setup:
            apply(setup)
        yield scratch
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def db(scratch_env: EnvSettings) -> Iterator[psycopg.Connection]:
    """Verbindung in die Wegwerf-Datenbank, mit autocommit."""
    with psycopg.connect(scratch_env.db_dsn().get_secret_value(), autocommit=True) as connection:
        yield connection
