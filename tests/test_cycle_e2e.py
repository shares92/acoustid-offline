"""E2E: der zeitgesteuerte Zyklus am echten Container (M2.5).

Der Nachweis, den die Definition of Done verlangt — *„Zyklus-Test gruen;
Historie korrekt; Prozesse schlafen nach dem Lauf"* —, und zwar gegen die
ganze Kette: Zeitplan → Wecken → Job als **Subprozess** des Waechters →
Report → ``update_run`` → Schlafen legen.

**Was hier absichtlich anders ist als in** ``test_wake_e2e.py``: dort weckt
eine Anfrage, hier ein Termin. Der Unterschied ist der ganze Punkt von
M2.5 — bis dahin konnte die Instanz nur reagieren.

**Die Quelle wird abgeklemmt — der Container hat naemlich Netz.** Compose
haengt ihn an sein Default-Bridge-Netz, und das ist nach draussen genattet:
„Compose-Test" heisst gerade **nicht** „ohne Netz" (der Marker ``network``
waehlt Tests ab, er nimmt keinem Container die Route). Der Delta-Import
zoege hier also echte Tagesdateien von data.acoustid.org — Fair-Use
gegenueber AcoustID OUe (§12 Punkt 9) und ein Lauf, der nie endet. Deshalb
zeigt der Aufbau den Namen im Container auf ``127.0.0.1``
(:func:`_cut_the_source`).

Der **erfolgreiche** Zyklus laeuft trotzdem am Backup-Job — er ist derselbe
Ablauf mit demselben Wecken, Report und Schlafen, nur ohne Download. Die
beiden **Fehlerpfade** laufen am Delta-Import: einmal bricht der
Plattenplatz-Guard ab, einmal die Kaltstart-Sperre des Importers (leere
``import_state``, kein Bootstrap gelaufen). Beide sind deterministisch und
in Sekunden vorbei; auf einen Netz-Timeout wartet dieser Test nirgends.

**Zeitreisen gibt es nicht.** Statt auf 04:00 zu warten, setzt der Test die
Termine auf „gerade eben" und startet den Waechter-**Prozess** neu (nicht
den Container — das ist der Gewinn des Ein-Container-Umbaus). Ein
verpasster Termin wird am selben Tag nachgeholt (siehe
:mod:`acoustid_watchdog.scheduler`), also laeuft der Job sofort.

**Laufen lassen** (Docker noetig, das Image wird gebaut, dauert Minuten)::

    uv run pytest tests/test_cycle_e2e.py --compose

Ohne ``--compose`` bzw. ``ACOUSTID_COMPOSE_TESTS=1`` wird der Test mit
Begruendung abgewaehlt; in der CI laeuft er nie (conftest.py im
Wurzelverzeichnis).

**Apple Silicon:** Das Image ist amd64-only (E3); colima mit
``--vz-rosetta`` starten (LEARNINGS).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.compose

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Eigener Port, damit der Lauf nicht mit einem belegten 8080 kollidiert —
#: und ein anderer als in ``test_wake_e2e.py``, damit beide nebeneinander
#: laufen koennen.
WATCHDOG_PORT = 18081
WATCHDOG_URL = f"http://127.0.0.1:{WATCHDOG_PORT}"
INDEX_NAME = "cycle"

#: Prozesse, die beim Idle-Stopp stehen (der Index ist resident, E12).
SLEEPING_PROGRAMS = ("db", "api")

#: Wurzel der echten Tagesdeltas (``acoustid_importer.streams.BASE_URL``).
#: Dieser Test faehrt sie nie an — :func:`_cut_the_source` sperrt sie.
DELTA_SOURCE_HOST = "data.acoustid.org"

PROJECT = f"musicmeta-cycle-{uuid.uuid4().hex[:8]}"

_COMPOSE = ["-p", PROJECT, "-f", "docker-compose.yml"]
_SUPERVISORCTL = ["supervisorctl", "-c", "/etc/supervisor/supervisord.conf"]

#: Migrationen und Suchindex anlegen — im Container, wo die Zugaenge stehen.
_BOOTSTRAP = """
from shared.db import apply
from shared.env import EnvSettings
from shared.fpindex import FpIndexClient
import psycopg

settings = EnvSettings.from_env()
with psycopg.connect(settings.db_dsn().get_secret_value(), autocommit=True) as connection:
    apply(connection)
FpIndexClient.from_env(settings).ensure_index()
print("bootstrap ok")
"""

#: Setzt Termine und Schalter der ``config.yaml``. Der Waechter haelt seine
#: Konfiguration im Speicher (bewusst kein Datei-Watcher, siehe
#: ``config_store``) — deshalb muss er danach neu starten.
#: **Die Uhrzeit muss die des Containers sein**, nicht die des Hosts: der
#: Zeitplan bildet seine Termine in der lokalen Zeit des Waechter-Prozesses
#: (`acoustid_watchdog.scheduler`), und die Container-Zeitzone ist per
#: Default UTC. Deshalb rechnet dieses Snippet **drinnen**.
#:
#: ``update_after`` ist der Weg fuer einen **zweiten** Termin am selben Tag:
#: er wartet, bis die laufende Minute nach dem uebergebenen Zeitpunkt liegt,
#: und setzt den Termin dann auf genau diese Minute. Das ist noetig, weil
#: „faellig" heisst *„seit diesem Termin lief noch keiner"* — ein Termin,
#: der vor dem letzten Lauf liegt, ist derselbe verbrauchte Termin und
#: feuert nie wieder (der Fehler, an dem dieser Test zuerst scheiterte).
_CONFIGURE = """
import datetime, json, sys, time
from shared.config import Config, load_config, save_config
from shared.env import EnvSettings

changes = json.loads(sys.argv[1])
settings = EnvSettings.from_env()
config = load_config(settings.config_path, create_if_missing=True)

now = datetime.datetime.now()
soon = (now - datetime.timedelta(minutes=1)).strftime("%H:%M")

data = config.model_dump(by_alias=True, mode="json")
if changes.get("update_now"):
    data["acoustid"]["update"]["time"] = soon
if changes.get("update_after"):
    boundary = datetime.datetime.fromisoformat(changes["update_after"])
    while True:
        moment = datetime.datetime.now(datetime.UTC)
        if moment.replace(second=0, microsecond=0) > boundary:
            break
        time.sleep(2)
    data["acoustid"]["update"]["time"] = datetime.datetime.now().strftime("%H:%M")
if changes.get("update_time"):
    data["acoustid"]["update"]["time"] = changes["update_time"]
if changes.get("backup_now"):
    data["backup"]["time"] = soon
if "backup_dir" in changes:
    data["backup"]["dir"] = changes["backup_dir"]
if "min_free_gb" in changes:
    data["disk"]["min_free_gb"] = changes["min_free_gb"]
if "metrics" in changes:
    data["metrics"]["enabled"] = changes["metrics"]

save_config(Config.model_validate(data), settings.config_path)
print(json.dumps({"update": data["acoustid"]["update"]["time"], "backup": data["backup"]["time"]}))
"""


# --- Werkzeuge --------------------------------------------------------------


def _env(data_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "MMO_PORT": str(WATCHDOG_PORT),
        "MMO_INDEX_NAME": INDEX_NAME,
        "MMO_LOG_LEVEL": "INFO",
        "MUSICMETA_IMAGE": "musicmeta-offline:e2e",
        "MUSICMETA_CONFIG_DIR": str(data_dir / "config"),
        "MUSICMETA_INDEX_DIR": str(data_dir / "index"),
        "MUSICMETA_DB_DIR": str(data_dir / "db"),
        "MUSICMETA_IMPORT_DIR": str(data_dir / "import"),
        "MUSICMETA_BACKUP_DIR": str(data_dir / "backup"),
    }


def _compose(data_dir: Path, *args: str, check: bool = True, timeout: int = 1800) -> str:
    result = subprocess.run(
        ["docker", "compose", *_COMPOSE, *args],
        cwd=REPO_ROOT,
        env=_env(data_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker compose {' '.join(args)} scheiterte ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def _exec(data_dir: Path, *command: str, check: bool = True) -> str:
    return _compose(data_dir, "exec", "-T", "app", *command, check=check, timeout=900)


def _python(data_dir: Path, script: str, *args: str) -> str:
    return _exec(data_dir, "/app/.venv/bin/python", "-c", script, *args)


def _configure(data_dir: Path, **changes: object) -> dict[str, str]:
    """Schreibt die Konfiguration und startet den Waechter-Prozess neu."""
    output = _python(data_dir, _CONFIGURE, json.dumps(changes))
    _exec(data_dir, *_SUPERVISORCTL, "restart", "watchdog")
    _wait("Waechter wieder erreichbar", _status, timeout_s=120)
    return json.loads(output.strip().splitlines()[-1])


def _cut_the_source(data_dir: Path) -> None:
    """Macht ``data.acoustid.org`` im Container unerreichbar.

    Der Eintrag in ``/etc/hosts`` zeigt den Namen auf den Loopback; ein
    Download-Versuch scheitert dort sofort mit „Connection refused" statt in
    einem Timeout zu haengen — und vor allem geht kein Byte an die echte
    Quelle.

    **Das ist das Netz unter dem Code, nicht sein Ersatz.** Der Importer
    verweigert einen unbegrenzten Lauf auf leerer Buchfuehrung ohnehin
    (``ColdStartError``, docs/importer-job.md). Ein Test darf sich fuer eine
    Zusage wie „hier wird nichts geladen" aber nicht auf den Prueflings-Code
    verlassen: faellt die Sperre, laedt sonst wieder der Test.
    """
    _exec(
        data_dir,
        "/bin/sh",
        "-c",
        f"printf '127.0.0.1 {DELTA_SOURCE_HOST}\\n' >> /etc/hosts",
    )
    hosts = _exec(data_dir, "/bin/sh", "-c", "grep acoustid /etc/hosts", check=False)
    assert f"127.0.0.1 {DELTA_SOURCE_HOST}" in hosts, hosts


def _states(data_dir: Path) -> dict[str, str]:
    output = _exec(data_dir, *_SUPERVISORCTL, "status", check=False)
    states: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def _sleeping(data_dir: Path) -> bool:
    """Schlafen Datenbank und API? (Der Index bleibt resident, E12.)"""
    states = _states(data_dir)
    return all(states.get(name) == "STOPPED" for name in SLEEPING_PROGRAMS)


def _status() -> dict:
    response = httpx.get(f"{WATCHDOG_URL}/status", timeout=10)
    response.raise_for_status()
    return response.json()


def _last_run() -> dict | None:
    return _status()["last_update_run"]


def _wait(description: str, check, *, timeout_s: float, interval_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = check()
        except Exception as exc:
            last = exc
        else:
            if last:
                return last
        time.sleep(interval_s)
    raise AssertionError(f"{description} nicht binnen {timeout_s:g} s — zuletzt: {last!r}")


def _require_docker() -> None:
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip("docker compose ist nicht verfuegbar")


def _require_own_project_is_empty(data_dir: Path) -> None:
    """Fail-closed: unter unserem Projektnamen darf noch nichts existieren."""
    existing = _compose(data_dir, "ps", "--all", "--quiet", check=False).strip()
    if existing:
        raise AssertionError(
            f"Compose-Projekt {PROJECT!r} ist nicht leer — Abbruch, "
            f"statt fremde Container anzufassen:\n{existing}"
        )


# --- Aufbau -----------------------------------------------------------------


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("musicmeta-cycle")
    for name in ("config", "index", "db", "import", "backup"):
        (root / name).mkdir()
    return root


@pytest.fixture(scope="module")
def sleeping_stack(data_dir: Path) -> Iterator[Path]:
    """Eingerichteter Container mit gestoppter Datenbank und API.

    Die Termine stehen dabei zunaechst auf einer Uhrzeit, die **heute nicht
    mehr kommt** (23:59 waere zu knapp — 23:58 laesst dem Aufbau Luft):
    sonst liefe schon waehrend der Einrichtung ein Zyklus los.
    """
    _require_docker()
    _require_own_project_is_empty(data_dir)
    try:
        _compose(data_dir, "up", "-d", "--build", "--wait", "--wait-timeout", "600")
        _wait("Waechter erreichbar", _status, timeout_s=120)
        _cut_the_source(data_dir)

        _exec(data_dir, *_SUPERVISORCTL, "start", "db")
        _wait(
            "Postgres nimmt Verbindungen an",
            lambda: "accepting connections" in _exec(data_dir, "pg_isready", check=False),
            timeout_s=120,
        )
        assert "bootstrap ok" in _python(data_dir, _BOOTSTRAP)

        _exec(data_dir, *_SUPERVISORCTL, "stop", "db")
        assert _sleeping(data_dir)
        yield data_dir
    finally:
        _compose(data_dir, "logs", "--tail", "120", check=False)
        _compose(data_dir, "down", "--remove-orphans", check=False)


@pytest.fixture(autouse=True)
def defined_state(sleeping_stack: Path) -> Iterator[None]:
    """Kein Test erbt den Zustand eines gescheiterten Vorgaengers.

    Alle Tests dieses Moduls teilen **einen** Container (Modul-Fixture) —
    ohne dieses Aufraeumen laeuft ein Fehlschlag die Reihe hinunter. Genau
    das ist im E2E-Lauf vom 05.08. passiert: der Retry-Test lief in seinen
    Timeout, der Waechter-Neustart des naechsten Tests erschlug den noch
    laufenden Job, und danach legte niemand mehr den Stack schlafen — ausser
    dem Idle-Stopp nach 15 Minuten (§8.5). Der Kennzahlen-Test scheiterte
    also an einem Zustand, mit dem er nichts zu tun hatte.

    Aufgeraeumt wird **nach** dem Test; jeder Test, der das Schlafen selbst
    zusichert, prueft es vorher in seinem eigenen Rumpf.
    """
    yield
    if _sleeping(sleeping_stack):
        return
    _exec(sleeping_stack, *_SUPERVISORCTL, "stop", *SLEEPING_PROGRAMS, check=False)


# --- Der erfolgreiche Zyklus ------------------------------------------------


def test_a_due_job_wakes_runs_and_sleeps_again(sleeping_stack: Path) -> None:
    """Die Definition of Done: Termin -> Wecken -> Lauf -> Historie -> Schlaf.

    Gefahren am Backup-Job: derselbe Ablauf wie beim Delta-Import (wecken,
    Subprozess, Report, ``update_run``, schlafen legen), nur ohne
    Download — und dieser Test hat kein Netz.
    """
    assert _status()["stack"]["state"] == "sleeping"
    assert _sleeping(sleeping_stack)

    _configure(
        sleeping_stack,
        backup_now=True,
        backup_dir="/backup",
        min_free_gb=0,
        update_time="23:58",
    )

    # 1. Der Termin ist faellig (verpasst und deshalb nachgeholt) — der
    #    Waechter weckt von selbst, ohne dass jemand angefragt hat.
    run = _wait(
        "Sicherung erscheint in der Historie",
        lambda: _run_of_kind("backup"),
        timeout_s=240,
        interval_s=3.0,
    )
    assert run["kind"] == "backup"

    # 2. Der Lauf geht durch.
    finished = _wait(
        "Sicherung abgeschlossen",
        lambda: (lambda item: item if item and not item["running"] else None)(
            _run_of_kind("backup")
        ),
        timeout_s=600,
        interval_s=3.0,
    )
    assert finished["result"] == "success", finished
    assert finished["error"] is None

    # 3. Und danach schlafen die Prozesse wieder — von selbst.
    _wait(
        "Prozesse schlafen wieder",
        lambda: _sleeping(sleeping_stack),
        timeout_s=300,
        interval_s=5.0,
    )
    assert _status()["stack"]["state"] == "sleeping"


def test_the_backup_holds_the_three_parts(sleeping_stack: Path) -> None:
    """Was gesichert wurde — und was ausdruecklich nicht (K9)."""
    listing = _exec(sleeping_stack, "/bin/sh", "-c", "ls /backup/backup-*/").split()

    assert "manifest.json" in listing
    assert "local_submission.copy.gz" in listing
    assert "watchdog.sqlite3" in listing
    assert "config.yaml" in listing
    # Der Lookup-Cache gehoert nicht ins Backup.
    assert "lookup-cache.sqlite3" not in listing


# --- Der Fehler- und Wiederholungspfad --------------------------------------


def test_a_full_disk_aborts_the_run_before_it_starts(sleeping_stack: Path) -> None:
    """E11/§8.8: der Guard prueft **vor** dem Wecken und bricht geordnet ab.

    Eine unerfuellbare Reserve ist der einzige Fehlerpfad, der ohne Netz
    und ohne Zeitreise deterministisch ist — und er trifft genau die
    Ausweitung aus M2.5: geprueft werden alle Schreibpfade, nicht nur das
    Dump-Verzeichnis.
    """
    _wait("Prozesse schlafen", lambda: _sleeping(sleeping_stack), timeout_s=300, interval_s=5.0)

    _configure(
        sleeping_stack,
        update_now=True,
        min_free_gb=100_000_000,  # so viel hat keine Platte
        backup_dir="",  # kein zweiter Termin dazwischen
    )

    finished = _wait(
        "Delta-Lauf abgebrochen",
        lambda: (lambda item: item if item and not item["running"] else None)(
            _run_of_kind("acoustid-delta")
        ),
        timeout_s=300,
        interval_s=3.0,
    )

    assert finished["result"] == "aborted", finished
    assert "gefordert" in (finished["error"] or "")
    # Der Guard steht **vor** dem Wecken: die Prozesse blieben aus.
    assert _sleeping(sleeping_stack)
    assert _status()["stack"]["state"] == "sleeping"


def test_the_next_cycle_repeats_the_failed_run(sleeping_stack: Path) -> None:
    """Invariante §8.4: wiederholt wird beim naechsten Zyklus.

    Statt einen Tag zu warten, bekommt der Zeitplan einen **neuen** Termin
    — fachlich derselbe Fall: „seit diesem Termin lief noch keiner".
    Diesmal ohne Guard, also startet der Importer wirklich als Subprozess.
    Der Nachweis ist, dass der Zyklus ihn ueberhaupt wieder angefasst hat —
    und dass die Historie **zwei** Laeufe zeigt, nicht einen
    ueberschriebenen.

    **Woran der zweite Lauf endet.** In diesem Container wurde nie eine
    Tagesdatei importiert, ``import_state`` ist leer — ein unbegrenzter
    ``update``-Lauf muesste also die ganze Historie ab 2011-08-19 holen.
    Genau das verweigert der Importer (``ColdStartError`` ->
    ``usage_error``, docs/importer-job.md), und zwar bevor er die Quelle
    auch nur fragt. Damit ist dieser Test schnell und deterministisch: kein
    Download, kein Timeout, keine Fair-Use-Frage.

    **Der neue Termin muss nach dem ersten Lauf liegen** (``update_after``).
    „Faellig" heisst *„seit diesem Termin lief noch keiner"*; ein Termin
    davor ist derselbe verbrauchte Termin. Mit „jetzt minus eine Minute"
    lag er, wenn beide Konfigurationen in dieselbe Minutenspanne fielen,
    **vor** dem Start des ersten Laufs — und der Zyklus feuerte nie wieder.
    """
    first = _run_of_kind("acoustid-delta")
    assert first is not None and first["result"] == "aborted"

    _configure(sleeping_stack, update_after=first["started_at"], min_free_gb=0)

    second = _wait(
        "zweiter Delta-Lauf",
        lambda: (lambda item: item if item and item["id"] != first["id"] else None)(
            _run_of_kind("acoustid-delta")
        ),
        timeout_s=300,
        interval_s=3.0,
    )
    assert second["id"] > first["id"]

    finished = _wait(
        "zweiter Lauf beendet",
        lambda: (lambda item: item if item and not item["running"] else None)(
            _run_of_kind("acoustid-delta")
        ),
        timeout_s=900,
        interval_s=5.0,
    )
    # Ohne Bestand kommt keine Tagesdatei — der Lauf ist trotzdem einer.
    assert finished["id"] == second["id"]
    assert finished["result"] == "failed", finished
    assert "bootstrap" in (finished["error"] or ""), finished
    # Und es wurde nichts geladen: das Dump-Verzeichnis ist unberuehrt.
    assert list((sleeping_stack / "import").iterdir()) == []

    # Und wieder: der Zyklus raeumt hinter sich auf.
    _wait(
        "Prozesse schlafen wieder",
        lambda: _sleeping(sleeping_stack),
        timeout_s=300,
        interval_s=5.0,
    )


# --- Kennzahlen -------------------------------------------------------------


def test_metrics_answer_only_when_enabled(sleeping_stack: Path) -> None:
    """`/metrics` ist per Default aus und weckt auch eingeschaltet nichts (§6, §8.2)."""
    assert httpx.get(f"{WATCHDOG_URL}/metrics", timeout=10).status_code == 404

    _configure(sleeping_stack, metrics=True, update_time="23:58", backup_dir="")
    _wait("Prozesse schlafen", lambda: _sleeping(sleeping_stack), timeout_s=300, interval_s=5.0)

    response = httpx.get(f"{WATCHDOG_URL}/metrics", timeout=10)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'musicmeta_stack_state{state="sleeping"} 1' in body
    assert "musicmeta_runs_total{" in body
    assert "musicmeta_lookups_total" in body
    # Der Abruf hat nichts geweckt.
    assert _sleeping(sleeping_stack)


# --- Hilfe ------------------------------------------------------------------


def _run_of_kind(kind: str) -> dict | None:
    """Der letzte Lauf dieser Art aus `/status`.

    `/status` zeigt unter ``last_update_run`` den letzten
    **AcoustID-Delta**-Lauf; fuer die Sicherung fragt der Test die
    Zustandsdatenbank nicht ab, sondern liest denselben Weg ueber das
    Ereignis-Log — beides waere aufwendig. Stattdessen genuegt hier der
    Blick auf `/status`, solange die Art passt.
    """
    last = _last_run()
    if kind == "acoustid-delta":
        return last
    # Fuer die Sicherung: sie steht nicht in `last_update_run` (das ist
    # bewusst der Delta-Lauf) — der Container beantwortet die Frage direkt.
    return _backup_run()


def _backup_run() -> dict | None:
    """Der letzte Backup-Lauf, gelesen aus der Waechter-SQLite im Container."""
    output = subprocess.run(
        [
            "docker",
            "compose",
            *_COMPOSE,
            "exec",
            "-T",
            "app",
            "/app/.venv/bin/python",
            "-c",
            _LAST_BACKUP,
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "MMO_PORT": str(WATCHDOG_PORT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    ).stdout.strip()
    if not output:
        return None
    payload = json.loads(output.splitlines()[-1])
    return payload or None


_LAST_BACKUP = """
import json
from acoustid_watchdog.runs import RunKind, latest_run
from acoustid_watchdog.store import Database
from shared.env import EnvSettings

settings = EnvSettings.from_env()
with Database.for_data_dir(settings.data_dir) as db:
    run = latest_run(db, RunKind.BACKUP)
print(json.dumps(run.as_dict() if run else None))
"""
