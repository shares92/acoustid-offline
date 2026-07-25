"""Gemeinsame Fixtures der Importer-Integrationstests (Phase 7).

Jeder Test bekommt eine **frisch angelegte, leere Datenbank** mit
vollstaendig angewendetem Schema; die konfigurierte Datenbank aus den
`AOFF_DB_*`-Variablen dient nur zum Anlegen und Loeschen. Damit sind die
Tests voneinander unabhaengig und koennen ohne Aufraeum-Logik Zeilen
zaehlen.

Die Verbindungen laufen per Vorgabe mit ``autocommit=True``: dann ist das
``with conn.transaction()`` im Importer eine echte Transaktion (und nicht
nur ein Savepoint des Aufrufers), und die Pruefabfragen zwischen zwei
Importen lassen keine Transaktion offen. Der Weg eines Service — normale
Verbindung ohne autocommit — bekommt einen eigenen Test.

Steuerung ueber `--integration` bzw. `ACOUSTID_INTEGRATION_TESTS`, siehe
conftest.py im Repo-Wurzelverzeichnis.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from shared.db import apply
from shared.env import EnvSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Die zehn Original-Tagesdateien (nicht im Repo — `fetch_fixtures.py`).
FIXTURE_DIR = REPO_ROOT / "tests/fixtures/acoustid-dumps"

#: Der vollstaendige Tag aus der Fixture-README, in Import-Reihenfolge
#: (Import-Regel 1).
FULL_DAY_FILES: tuple[str, ...] = (
    "2026-07-22-track-update.jsonl.gz",
    "2026-07-22-meta-update.jsonl.gz",
    "2026-07-22-fingerprint-update.jsonl.gz",
    "2026-07-22-track_fingerprint-update.jsonl.gz",
    "2026-07-22-track_mbid-update.jsonl.gz",
    "2026-07-22-track_meta-update.jsonl.gz",
    "2026-07-22-track_puid-update.jsonl.gz",
)


# --- Fixture-Dateien --------------------------------------------------------


@pytest.fixture(scope="session")
def dumps() -> Path:
    """Verzeichnis der Original-Fixtures; fehlen sie, wird uebersprungen."""
    missing = [name for name in FULL_DAY_FILES if not (FIXTURE_DIR / name).exists()]
    if missing:
        pytest.skip(
            f"Dump-Fixtures fehlen ({', '.join(missing[:3])} …) — "
            "'uv run python tests/fixtures/fetch_fixtures.py' holt sie nach"
        )
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def full_day(dumps: Path) -> tuple[Path, ...]:
    """Alle sieben Tagesdateien des 22.07.2026 in Import-Reihenfolge."""
    return tuple(dumps / name for name in FULL_DAY_FILES)


# --- Datenbanken ------------------------------------------------------------


@pytest.fixture(scope="session")
def env_settings() -> EnvSettings:
    """Zugang aus den `AOFF_DB_*`-Variablen — derselbe Weg wie im Betrieb."""
    return EnvSettings.from_env()


def _fresh_connection(
    settings: EnvSettings, *, autocommit: bool, migrate: bool = True
) -> Iterator[psycopg.Connection]:
    """Legt eine leere Datenbank an, migriert sie und raeumt sie danach ab.

    Mit ``migrate=False`` bleibt sie voellig leer — so beginnt der Bootstrap
    in Wirklichkeit, und nur so ist pruefbar, dass er die Gruppen ``core``
    und ``indexes`` zur richtigen Zeit anwendet (Import-Regel 6).
    """
    name = f"aoff_imp_{uuid4().hex[:12]}"
    admin_dsn = settings.db_dsn().get_secret_value()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    scratch_dsn = settings.model_copy(update={"db_name": name}).db_dsn().get_secret_value()
    try:
        if migrate:
            with psycopg.connect(scratch_dsn, autocommit=True) as setup:
                apply(setup)
        with psycopg.connect(scratch_dsn, autocommit=autocommit) as connection:
            yield connection
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def db(env_settings: EnvSettings) -> Iterator[psycopg.Connection]:
    """Leere Datenbank mit vollstaendigem Schema, Verbindung mit autocommit."""
    yield from _fresh_connection(env_settings, autocommit=True)


@pytest.fixture
def service_db(env_settings: EnvSettings) -> Iterator[psycopg.Connection]:
    """Wie :func:`db`, aber ohne autocommit — so verbindet sich ein Service."""
    yield from _fresh_connection(env_settings, autocommit=False)


@pytest.fixture
def empty_db(env_settings: EnvSettings) -> Iterator[psycopg.Connection]:
    """Leere Datenbank **ohne** Schema — der Ausgangspunkt des Bootstraps."""
    yield from _fresh_connection(env_settings, autocommit=True, migrate=False)


@pytest.fixture(scope="module")
def module_db(env_settings: EnvSettings) -> Iterator[psycopg.Connection]:
    """Eine Datenbank je Testmodul — fuer die teuren, nur lesenden Faelle.

    Der vollstaendige Tag (2.214 Fingerprint-Vektoren) wird damit einmal
    eingespielt statt je Test.
    """
    yield from _fresh_connection(env_settings, autocommit=True)


# --- Synthetische Tagesdateien ---------------------------------------------


@pytest.fixture
def write_delta(tmp_path: Path) -> Callable[..., Path]:
    """Schreibt eine gz-JSONL-Tagesdatei unter ihrem regulaeren Namen.

    Fuer die Faelle, die keine Original-Fixture belegt: eine Reaktivierung
    ueber zwei Tage, eine kaputte Zeile, ein zweiter Tag auf derselben ID.
    """

    def _write(
        name: str,
        rows: Sequence[Mapping[str, Any]] = (),
        *,
        raw_lines: Sequence[str] = (),
    ) -> Path:
        path = tmp_path / name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            for line in raw_lines:
                handle.write(line + "\n")
        return path

    return _write
