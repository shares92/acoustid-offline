"""E2E: eine Anfrage weckt den schlafenden Stack (Phasen 15 und 16).

Der Nachweis, den die Definition of Done verlangt — und der einzige Test im
Repo, der das **ganze** Zusammenspiel gegen echte Container faehrt:
Waechter (Proxy + Docker-Steuerung) -> acoustid-api (interner Healthcheck)
-> Postgres + acoustid-index.

**Ablauf.** Erst wird der Stack einmal ordentlich hochgefahren (Compose legt
die Container an, die Migrationen laufen, der Suchindex entsteht), dann
wieder **gestoppt** — genau der Betriebszustand, um den es geht: Container
existieren, laufen aber nicht. Erst danach startet der Waechter; er erhebt
den Zustand aus Docker und findet einen schlafenden Stack vor. Die eine
folgende ``/v2/lookup``-Anfrage muss ihn wecken und beantwortet werden.

**Warum der Waechter zuletzt startet.** Er haelt den Stack-Zustand im
Speicher (DECISIONS 2026-08-01, Punkt 6) und erhebt ihn beim Start.

Seit Phase 16 fuehrt er ihn ausserdem nach: der vierte Test hier stoppt den
Stack **von Hand** (wie der Betreiber ueber die Unraid-Oberflaeche) und
prueft, dass der Waechter das von selbst merkt — `/status` sagt danach
``schlafend``, und die **erste** folgende Anfrage weckt, statt ins Leere zu
laufen.

Seit Phase 17 steht der Gegenbeweis daneben, und er ist der Nachweis der
Cache-Phase: derselbe Lookup ein zweites Mal weckt **nicht**. Der Test legt
den Stack von Hand schlafen und bekommt die Antwort trotzdem — bytegleich,
und danach laeuft immer noch kein Container.

Seit Phase 18 haengt der letzte Test genau daran an: er stellt den Waechter
auf ``auth.mode: apikey`` um und fragt **dieselbe, gecachte** Antwort noch
dreimal — ohne Key, mit falschem Key, mit richtigem Key. Das ist der
Nachweis, dass die Key-Pruefung vor dem Cache steht und nicht erst in der
API (§7 „Durchsetzungsort Auth & Rate-Limit"). Er kostet einen Neustart des
Waechter-Containers und drei Anfragen.

**Laufen lassen** (Docker noetig, Images werden gebaut, dauert Minuten)::

    uv run pytest tests/test_wake_e2e.py --compose

Ohne ``--compose`` bzw. ``ACOUSTID_COMPOSE_TESTS=1`` wird der Test mit
Begruendung abgewaehlt; in der CI laeuft er nie (conftest.py im
Wurzelverzeichnis).

**Achtung, er greift in die lokale Docker-Umgebung ein:** Die
Container-Namen stehen in ARCHITECTURE §6 fest, ein eigener
Compose-Projektname wuerde daran nichts aendern. Wer lokal eine echte
Instanz betreibt, sollte den Test nicht laufen lassen — er stoppt und
entfernt am Ende alles, was so heisst, inklusive der Volumes.

**Apple Silicon:** Das Index-Image gibt es nur fuer amd64 und haengt unter
qemu still; colima mit ``--vz-rosetta`` starten (LEARNINGS).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest

from shared.db import apply
from shared.fingerprint import encode_fingerprint
from shared.fpindex import FpIndexClient

pytestmark = pytest.mark.compose

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Eigene Ports, damit der Lauf nicht mit einem belegten 8080/5432 auf dem
#: Entwicklerrechner kollidiert. Der Index-Port steht im Test-Overlay fest.
WATCHDOG_PORT = 18080
DB_PORT = 15432
INDEX_URL = "http://127.0.0.1:6081"
INDEX_NAME = "e2e"

WATCHDOG_URL = f"http://127.0.0.1:{WATCHDOG_PORT}"

#: Container aus ARCHITECTURE §6, die der Waechter steuert.
STACK_CONTAINERS = ("acoustid-db", "acoustid-index", "acoustid-api")

_COMPOSE_ENV = {
    "AOFF_DB_PASSWORD": "e2e-wegwerf-passwort",
    "AOFF_DB_PORT": str(DB_PORT),
    "AOFF_INDEX_NAME": INDEX_NAME,
    "AOFF_PORT": str(WATCHDOG_PORT),
    "AOFF_LOG_LEVEL": "INFO",
}

_STACK_FILES = ["-f", "docker-compose.yml", "-f", "tests/docker-compose.test.yml"]
_WATCHDOG_FILES = ["-f", "docker-compose.watchdog.yml"]


# --- Werkzeuge --------------------------------------------------------------


def _env() -> dict[str, str]:
    return {**os.environ, **_COMPOSE_ENV}


def _compose(files: list[str], *args: str, check: bool = True, timeout: int = 900) -> str:
    """Ein ``docker compose``-Aufruf im Repo-Wurzelverzeichnis."""
    result = subprocess.run(
        ["docker", "compose", *files, *args],
        cwd=REPO_ROOT,
        env=_env(),
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


def _docker_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _require_docker() -> None:
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip("docker compose ist nicht verfuegbar")


def _wait(description: str, check, *, timeout_s: float, interval_s: float = 2.0):
    """Wartet, bis ``check()`` etwas Wahres liefert — sonst schlaegt der Test fehl."""
    deadline = time.monotonic() + timeout_s
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = check()
        except Exception as exc:
            # Beim Hochfahren ist jeder Fehler moeglich (Verbindung
            # abgelehnt, halb gestarteter Dienst) — er ist hier nur die
            # Begruendung fuer den naechsten Versuch.
            last = exc
        else:
            if last:
                return last
        time.sleep(interval_s)
    raise AssertionError(f"{description} nicht binnen {timeout_s:g} s — zuletzt: {last!r}")


def _dsn() -> str:
    return f"postgresql://acoustid:{_COMPOSE_ENV['AOFF_DB_PASSWORD']}@127.0.0.1:{DB_PORT}/acoustid"


def _status() -> dict:
    response = httpx.get(f"{WATCHDOG_URL}/status", timeout=10)
    response.raise_for_status()
    return response.json()


# --- Aufbau -----------------------------------------------------------------


@pytest.fixture(scope="module")
def sleeping_stack() -> Iterator[None]:
    """Angelegter, migrierter, **gestoppter** Stack mit laufendem Waechter."""
    _require_docker()
    try:
        # 1. Datenquellen hochfahren und einrichten. Ohne Schema und ohne
        #    angelegten Index wird die api nie gesund — der Bootstrap macht
        #    das im Betrieb, hier der Test.
        _compose(_STACK_FILES, "up", "-d", "db", "index")
        _wait("Postgres erreichbar", _db_reachable, timeout_s=120)
        with psycopg.connect(_dsn(), autocommit=True) as connection:
            apply(connection)
        _wait("Index-Server erreichbar", _index_reachable, timeout_s=180)
        FpIndexClient(INDEX_URL, INDEX_NAME).ensure_index()

        # 2. Die api anlegen und einmal gesund werden lassen.
        _compose(_STACK_FILES, "up", "-d", "--build", "api", timeout=1800)
        _wait("API gesund", lambda: _docker_healthy("acoustid-api"), timeout_s=300)

        # 3. Schlafen legen — der Ausgangszustand des Tests.
        _compose(_STACK_FILES, "stop")
        assert not any(_docker_running(name) for name in STACK_CONTAINERS)

        # 4. Erst jetzt der Waechter: er erhebt den Zustand beim Start.
        _compose(_WATCHDOG_FILES, "up", "-d", "--build", "watchdog", timeout=1800)
        _wait("Waechter erreichbar", _status, timeout_s=120)
        yield
    finally:
        _compose(_WATCHDOG_FILES, "down", "-v", check=False)
        _compose(_STACK_FILES, "down", "-v", check=False)


def _db_reachable() -> bool:
    with psycopg.connect(_dsn(), connect_timeout=5):
        return True


def _index_reachable() -> bool:
    return httpx.get(f"{INDEX_URL}/_health", timeout=5).status_code == 200


def _docker_healthy(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() == "healthy"


# --- Der Nachweis -----------------------------------------------------------


def test_lookup_wakes_the_sleeping_stack(sleeping_stack: None) -> None:
    """Anfrage bei schlafendem Stack -> Wecken -> Antwort.

    Die Definition of Done der Phase in einem Test. Geprueft wird die ganze
    Kette, nicht nur das Ergebnis:

    1. Der Waechter meldet vor der Anfrage ``schlafend`` und kein
       Stack-Container laeuft.
    2. Die Anfrage wird **gehalten**, nicht abgewiesen — sie kommt mit
       HTTP 200 und einer gueltigen AcoustID-Antwort zurueck.
    3. Danach laufen alle drei Container, und `/status` sagt ``bereit``.
    """
    assert _status()["stack"]["state"] == "sleeping"
    assert not any(_docker_running(name) for name in STACK_CONTAINERS)

    fingerprint = encode_fingerprint([0x0000FFFF, 0x0001FFFF, 0x0002FFFE])
    started_at = time.monotonic()
    response = httpx.get(
        f"{WATCHDOG_URL}/v2/lookup",
        params={"client": "e2e", "fingerprint": fingerprint, "duration": 641},
        # Grosszuegiger als `wake.hold_timeout_s` (90 s, §6): der Waechter
        # soll seine 503-Antwort geben duerfen, statt dass der Client
        # vorher abbricht — sonst saehe der Test einen Timeout, wo eine
        # klare Fehlermeldung steht.
        timeout=180,
    )
    waited_s = time.monotonic() - started_at

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["results"] == []

    assert all(_docker_running(name) for name in STACK_CONTAINERS)
    assert _status()["stack"]["state"] == "ready"
    # Fuer den Bericht: wie lange das Wecken auf dieser Maschine gedauert hat.
    print(json.dumps({"wake_seconds": round(waited_s, 1)}))


def test_second_request_is_answered_without_waking(sleeping_stack: None) -> None:
    """Ist der Stack wach, kostet die naechste Anfrage keinen Weckvorgang."""
    started_at = time.monotonic()
    response = httpx.get(
        f"{WATCHDOG_URL}/v2/lookup",
        params={"client": "e2e", "fingerprint": encode_fingerprint([1, 2, 3]), "duration": 10},
        timeout=60,
    )

    assert response.status_code == 200
    assert time.monotonic() - started_at < 30


def test_watchdog_passes_the_api_error_through(sleeping_stack: None) -> None:
    """Fehlerantworten der API bleiben unveraendert (kein eigenes Fehlerbild)."""
    response = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params={"client": "e2e"}, timeout=60)

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "error": {"code": 2, "message": 'missing required parameter "fingerprint"'},
    }


def test_a_stack_stopped_by_hand_is_noticed(sleeping_stack: None) -> None:
    """Phase 16: der Waechter merkt einen Stopp, den er nicht veranlasst hat.

    Der Betreiber stoppt den Stack ueber die Unraid-Oberflaeche — bis
    Phase 15 blieb der Waechter danach auf ``bereit`` stehen, die erste
    Anfrage lief in eine tote API (503) und erst die zweite weckte. Der
    Zustandsabgleich im Hintergrund (Takt: 15 s) schliesst die Luecke.

    Der Test laeuft absichtlich weit hinten: er nimmt dem Stack die Laufzeit
    weg, die die vorherigen Tests brauchen.

    **Eigene Anfrageparameter** (Phase 17): mit denen des zweiten Tests
    laege die Antwort im Lookup-Cache, und die Anfrage wuerde gar nicht mehr
    wecken — dann pruefte dieser Test nichts mehr.
    """
    assert _status()["stack"]["state"] == "ready"

    _compose(_STACK_FILES, "stop")
    assert not any(_docker_running(name) for name in STACK_CONTAINERS)

    # Ohne eine einzige Anfrage: der Waechter korrigiert seine Anzeige von
    # selbst. Grosszuegig gewartet — der Takt ist 15 s, die Maschine kann
    # unter Last stehen.
    _wait(
        "Waechter meldet den Stack als schlafend",
        lambda: _status()["stack"]["state"] == "sleeping",
        timeout_s=90,
        interval_s=3.0,
    )

    # Und die **erste** Anfrage danach weckt wieder, statt 503 zu geben.
    response = httpx.get(
        f"{WATCHDOG_URL}/v2/lookup",
        params={"client": "e2e", "fingerprint": encode_fingerprint([4, 5, 6]), "duration": 11},
        timeout=180,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert _status()["stack"]["state"] == "ready"


def test_a_cached_lookup_is_answered_without_waking(sleeping_stack: None) -> None:
    """Phase 17: derselbe Lookup ein zweites Mal — bei gestopptem Stack.

    Die Definition of Done der Cache-Phase am echten Stack, in drei
    Schritten:

    1. Ein Lookup mit frischen Parametern geht an die API und wird
       eingelagert (der Stack laeuft nach dem vorherigen Test).
    2. Der Stack wird **von Hand gestoppt** — wie der Betreiber es ueber die
       Unraid-Oberflaeche taete.
    3. Dieselbe Anfrage kommt bytegleich zurueck, und danach laeuft immer
       noch **kein** Container: kein Weckvorgang, kein Docker-Start.

    Der letzte Teil ist der eigentliche Nachweis. Ein Treffer, der doch
    weckt, wuerde hier nicht nur langsam sein — er waere sichtbar, weil die
    Container danach liefen.

    Der Test kostet kaum Laufzeit: ein ``compose stop`` und zwei Anfragen.
    """
    parameters = {
        "client": "e2e",
        "fingerprint": encode_fingerprint([0x00A0FF01, 0x00A0FF02, 0x00A0FF03]),
        "duration": 137,
    }
    _wait("Stack wieder bereit", lambda: _status()["stack"]["state"] == "ready", timeout_s=180)

    first = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params=parameters, timeout=180)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "ok"

    _compose(_STACK_FILES, "stop")
    assert not any(_docker_running(name) for name in STACK_CONTAINERS)

    started_at = time.monotonic()
    second = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params=parameters, timeout=180)
    answered_s = time.monotonic() - started_at

    assert second.status_code == 200, second.text
    assert second.content == first.content
    assert second.headers["content-type"] == first.headers["content-type"]
    # Der Nachweis: der Stack schlaeft immer noch.
    assert not any(_docker_running(name) for name in STACK_CONTAINERS)
    print(json.dumps({"cache_hit_seconds": round(answered_s, 2)}))
    # Fuer den naechsten Test: derselbe Eintrag liegt jetzt im Cache.
    # **Ohne** ``client`` — der gehoert nicht zum Schluessel (Phase 17) und
    # wird dort je Fall eigens gesetzt.
    CACHED_LOOKUP.update({name: value for name, value in parameters.items() if name != "client"})


#: Die Parameter des gerade eingelagerten Lookups, ohne ``client`` — der
#: letzte Test prueft an ihnen, dass die Key-Pruefung **auch** vor dem Cache
#: greift. Sie werden erst im vorherigen Test gefuellt; die Tests dieser
#: Datei laufen bewusst der Reihe nach auf demselben Stack.
CACHED_LOOKUP: dict[str, object] = {}

#: Der Key, den der letzte Test dem Waechter unterschiebt.
E2E_API_KEY = "e2e-testschluessel"

#: Was im Waechter-Container laeuft, um ``auth.mode`` umzustellen und einen
#: Key anzulegen. Beides sind Schreibvorgaenge auf dem Datenverzeichnis; die
#: Admin-UI (Phasen 25/26) wird dafuer spaeter Routen haben. Der Waechter
#: haelt seine Konfiguration im Speicher (bewusst kein Datei-Watcher) —
#: deshalb muss er danach neu starten.
_ENABLE_APIKEY = """
from acoustid_watchdog.auth import hash_key
from acoustid_watchdog.store import Database, utc_now
from shared.config import load_config, save_config
from shared.env import EnvSettings
from shared.models import AuthMode

settings = EnvSettings.from_env()
config = load_config(settings.config_path, create_if_missing=True)
save_config(
    config.model_copy(update={"auth": config.auth.model_copy(update={"mode": AuthMode.APIKEY})}),
    settings.config_path,
)
with Database.for_data_dir(settings.data_dir) as db:
    with db.transaction() as tx:
        tx.execute(
            "INSERT OR IGNORE INTO api_key (label, key_hash, active, created_at)"
            " VALUES (?, ?, 1, ?)",
            ("e2e", hash_key("__E2E_KEY__"), utc_now()),
        )
print("ok")
""".replace("__E2E_KEY__", E2E_API_KEY)


def test_apikey_mode_guards_even_the_cache(sleeping_stack: None) -> None:
    """Phase 18: die Key-Pruefung steht vor dem Cache — am echten Waechter.

    Der billigste moegliche Nachweis und zugleich der schaerfste: der Stack
    ist aus dem vorherigen Test **gestoppt**, die Antwort liegt im Cache. Es
    kostet einen Neustart des Waechter-Containers und drei Anfragen.

    1. ``auth.mode: apikey`` setzen und einen Key anlegen (im Container, wie
       es die Admin-UI spaeter tut), dann neu starten.
    2. Dieselbe, gecachte Anfrage **ohne** ``client`` -> 2/400, **mit
       falschem** ``client`` -> 4/400. Kein Container laeuft danach.
    3. Mit dem richtigen Key kommt sie aus dem Cache zurueck — und der Stack
       schlaeft immer noch.

    `/status` muss die ganze Zeit ohne Key antworten (§7).
    """
    assert CACHED_LOOKUP, "der vorherige Test hat nichts eingelagert"

    # Der Interpreter des Images liegt im venv, und das Arbeitsverzeichnis
    # ist bewusst ``/`` (Namespace-Paket-Falle, LEARNINGS) — beides steht so
    # im Dockerfile und im Healthcheck.
    _compose(
        _WATCHDOG_FILES, "exec", "-T", "watchdog", "/app/.venv/bin/python", "-c", _ENABLE_APIKEY
    )
    _compose(_WATCHDOG_FILES, "restart", "watchdog")
    _wait("Waechter wieder erreichbar", _status, timeout_s=120)
    assert not any(_docker_running(name) for name in STACK_CONTAINERS)

    without = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params=CACHED_LOOKUP, timeout=60)
    assert without.status_code == 400, without.text
    assert without.json() == {
        "status": "error",
        "error": {"code": 2, "message": 'missing required parameter "client"'},
    }

    wrong = dict(CACHED_LOOKUP, client="falscher-schluessel")
    denied = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params=wrong, timeout=60)
    assert denied.status_code == 400, denied.text
    assert denied.json() == {
        "status": "error",
        "error": {"code": 4, "message": "invalid API key"},
    }

    # Abgewiesen heisst: nichts angefasst (Invariante §8.2).
    assert not any(_docker_running(name) for name in STACK_CONTAINERS)

    allowed = dict(CACHED_LOOKUP, client=E2E_API_KEY)
    accepted = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params=allowed, timeout=60)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ok"
    assert not any(_docker_running(name) for name in STACK_CONTAINERS)
