"""E2E: eine Anfrage weckt den schlafenden Stack (Phasen 15-18, portiert in M1b).

Der Nachweis, den die Definition of Done verlangt — und der einzige Test im
Repo, der das **ganze** Zusammenspiel gegen echte Prozesse faehrt: Waechter
(Proxy + Prozess-Steuerung) -> supervisord -> API-Dienst (interner
Healthcheck) -> Postgres + acoustid-index.

**Was der Ein-Container-Umbau hier geaendert hat.** Die fuenf fachlichen
Nachweise sind dieselben geblieben; nur die Werkzeuge sind andere:

===============================  ==========================================
v1 (fuenf Container)             v2 (ein Container)
===============================  ==========================================
zwei Compose-Dateien             eine
``docker inspect <name>``        ``supervisorctl status <program>``
``docker compose stop``          ``supervisorctl stop db api``
„kein Container laeuft"          „``db`` und ``api`` sind ``STOPPED``"
===============================  ==========================================

Die letzte Zeile ist mehr als eine Uebersetzung: der Suchindex bleibt seit
E12 **resident**. „Schlafend" heisst also nicht mehr „nichts laeuft",
sondern „nichts beruehrt das Array" — Waechter und Index laufen weiter, und
beide liegen auf dem Cache.

**Ablauf.** Erst wird der Container einmal ordentlich eingerichtet
(Migrationen laufen, der Suchindex entsteht), dann werden Datenbank und API
wieder **gestoppt** — genau der Betriebszustand, um den es geht. Die eine
folgende ``/v2/lookup``-Anfrage muss sie wecken und beantwortet werden.

**Laufen lassen** (Docker noetig, das Image wird gebaut, dauert Minuten)::

    uv run pytest tests/test_wake_e2e.py --compose

Ohne ``--compose`` bzw. ``ACOUSTID_COMPOSE_TESTS=1`` wird der Test mit
Begruendung abgewaehlt; in der CI laeuft er nie (conftest.py im
Wurzelverzeichnis).

**Er kann keine echte Instanz anfassen.** Der Lauf bekommt einen eigenen,
zufaelligen Compose-Projektnamen (``-p musicmeta-e2e-<suffix>``); die
Produktions-Compose traegt bewusst keinen festen ``container_name`` mehr,
also gehoert jeder erzeugte Container zu genau diesem Projekt. Vor dem
Aufbau wird **fail-closed** geprueft, dass unter diesem Namen noch nichts
existiert — sonst bricht der Test ab, statt fremde Container zu ersetzen.
Die Daten liegen in einem Wegwerf-Verzeichnis unter ``tmp_path``.

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

from shared.fingerprint import encode_fingerprint

pytestmark = pytest.mark.compose

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Eigener Port, damit der Lauf nicht mit einem belegten 8080 auf dem
#: Entwicklerrechner kollidiert.
WATCHDOG_PORT = 18080
WATCHDOG_URL = f"http://127.0.0.1:{WATCHDOG_PORT}"
INDEX_NAME = "e2e"

#: Prozesse, die der Waechter steuert (``supervisor/supervisord.conf``).
#: ``index`` fehlt mit Absicht: er ist resident (E12) und wird beim
#: Idle-Stopp nicht angefasst.
SLEEPING_PROGRAMS = ("db", "api")

#: Eigener Compose-Projektname je Lauf. Er ist die **einzige** Klammer um
#: die erzeugten Ressourcen — deshalb zufaellig und nicht geraten: zwei
#: gleichzeitige Laeufe kaemen sich sonst ins Gehege, und ein fester Name
#: koennte eine echte Installation treffen.
PROJECT = f"musicmeta-e2e-{uuid.uuid4().hex[:8]}"

_COMPOSE = ["-p", PROJECT, "-f", "docker-compose.yml"]
_SUPERVISORCTL = ["supervisorctl", "-c", "/etc/supervisor/supervisord.conf"]

#: Migrationen und Suchindex anlegen — im Container, weil dort die
#: Zugaenge stehen (der Entrypoint erzeugt das Datenbank-Passwort selbst).
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


# --- Werkzeuge --------------------------------------------------------------


def _env(data_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "AOFF_PORT": str(WATCHDOG_PORT),
        "AOFF_INDEX_NAME": INDEX_NAME,
        "AOFF_LOG_LEVEL": "INFO",
        "MUSICMETA_IMAGE": "musicmeta-offline:e2e",
        "MUSICMETA_CONFIG_DIR": str(data_dir / "config"),
        "MUSICMETA_INDEX_DIR": str(data_dir / "index"),
        "MUSICMETA_DB_DIR": str(data_dir / "db"),
        "MUSICMETA_IMPORT_DIR": str(data_dir / "import"),
        "MUSICMETA_BACKUP_DIR": str(data_dir / "backup"),
    }


def _compose(data_dir: Path, *args: str, check: bool = True, timeout: int = 1800) -> str:
    """Ein ``docker compose``-Aufruf im Repo-Wurzelverzeichnis."""
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
    """Etwas im laufenden Container ausfuehren."""
    return _compose(data_dir, "exec", "-T", "app", *command, check=check, timeout=600)


def _states(data_dir: Path) -> dict[str, str]:
    """``supervisorctl status`` als Woerterbuch — der Ersatz fuer ``docker inspect``."""
    # `status` liefert Exit-Code 3, sobald ein Programm nicht laeuft — das
    # ist hier der Normalfall und kein Fehler.
    output = _exec(data_dir, *_SUPERVISORCTL, "status", check=False)
    states: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def _running(data_dir: Path, program: str) -> bool:
    return _states(data_dir).get(program) == "RUNNING"


def _sleeping(data_dir: Path) -> bool:
    """Schlafen Datenbank und API? (Der Index bleibt resident, E12.)"""
    states = _states(data_dir)
    return all(states.get(name) == "STOPPED" for name in SLEEPING_PROGRAMS)


def _require_docker() -> None:
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip("docker compose ist nicht verfuegbar")


def _require_own_project_is_empty(data_dir: Path) -> None:
    """Fail-closed: unter unserem Projektnamen darf noch nichts existieren.

    Der Test raeumt am Ende alles ab, was zu :data:`PROJECT` gehoert. Faende
    er dort etwas vor, das er nicht selbst angelegt hat, wuerde er es beim
    Aufbau ersetzen und beim Abbau entfernen — deshalb hier lieber
    abbrechen. Bei einem zufaelligen Namen ist das der unmoegliche Fall;
    genau deswegen ist die Pruefung billig und die Aussage eindeutig.
    """
    existing = _compose(data_dir, "ps", "--all", "--quiet", check=False).strip()
    if existing:
        raise AssertionError(
            f"Compose-Projekt {PROJECT!r} ist nicht leer — Abbruch, "
            f"statt fremde Container anzufassen:\n{existing}"
        )


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


def _status() -> dict:
    response = httpx.get(f"{WATCHDOG_URL}/status", timeout=10)
    response.raise_for_status()
    return response.json()


# --- Aufbau -----------------------------------------------------------------


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Wegwerf-Verzeichnis fuer die fuenf Bind-Mounts."""
    root = tmp_path_factory.mktemp("musicmeta-e2e")
    for name in ("config", "index", "db", "import", "backup"):
        (root / name).mkdir()
    return root


@pytest.fixture(scope="module")
def sleeping_stack(data_dir: Path) -> Iterator[Path]:
    """Eingerichteter Container mit **gestoppter** Datenbank und API."""
    _require_docker()
    _require_own_project_is_empty(data_dir)
    try:
        # 1. Der ganze Container auf einmal. `--wait` wartet auf den
        #    Healthcheck (`GET /status`) — der beweist zugleich, dass der
        #    Waechter ohne Stack antwortet.
        _compose(data_dir, "up", "-d", "--build", "--wait", "--wait-timeout", "600")
        _wait("Waechter erreichbar", _status, timeout_s=120)

        # 2. Einrichten: Schema und Suchindex. Im Betrieb macht das der
        #    Bootstrap-Job; hier der Test. Dafuer muss die Datenbank laufen —
        #    also einmal von Hand starten, wie es der Betreiber taete.
        _exec(data_dir, *_SUPERVISORCTL, "start", "db")
        _wait(
            "Postgres nimmt Verbindungen an",
            lambda: "accepting connections" in _exec(data_dir, "pg_isready", check=False),
            timeout_s=120,
        )
        assert "bootstrap ok" in _exec(data_dir, "/app/.venv/bin/python", "-c", _BOOTSTRAP)

        # 3. Schlafen legen — der Ausgangszustand des Tests.
        _exec(data_dir, *_SUPERVISORCTL, "stop", "db")
        assert _sleeping(data_dir)
        # Der Index laeuft weiter: das ist E12, und es ist der Unterschied
        # zu v1 („kein Container laeuft").
        assert _running(data_dir, "index")
        yield data_dir
    finally:
        _compose(data_dir, "logs", "--tail", "80", check=False)
        # `down` ohne `-v`: es gibt keine benannten Volumes (E13, alles
        # Bind-Mounts), und der Aufruf gilt ausschliesslich fuer unser
        # eigenes Projekt.
        _compose(data_dir, "down", "--remove-orphans", check=False)


# --- Der Nachweis -----------------------------------------------------------


def test_lookup_wakes_the_sleeping_stack(sleeping_stack: Path) -> None:
    """Anfrage bei schlafendem Stack -> Wecken -> Antwort.

    Die Definition of Done der Phase in einem Test. Geprueft wird die ganze
    Kette, nicht nur das Ergebnis:

    1. Der Waechter meldet vor der Anfrage ``schlafend``, und weder
       Datenbank noch API laufen.
    2. Die Anfrage wird **gehalten**, nicht abgewiesen — sie kommt mit
       HTTP 200 und einer gueltigen AcoustID-Antwort zurueck.
    3. Danach laufen alle drei Prozesse, und `/status` sagt ``bereit``.
    """
    assert _status()["stack"]["state"] == "sleeping"
    assert _sleeping(sleeping_stack)

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

    states = _states(sleeping_stack)
    assert states["db"] == "RUNNING"
    assert states["index"] == "RUNNING"
    assert states["api"] == "RUNNING"
    assert _status()["stack"]["state"] == "ready"
    # Fuer den Bericht: wie lange das Wecken auf dieser Maschine gedauert hat.
    print(json.dumps({"wake_seconds": round(waited_s, 1)}))


def test_second_request_is_answered_without_waking(sleeping_stack: Path) -> None:
    """Ist der Stack wach, kostet die naechste Anfrage keinen Weckvorgang."""
    started_at = time.monotonic()
    response = httpx.get(
        f"{WATCHDOG_URL}/v2/lookup",
        params={"client": "e2e", "fingerprint": encode_fingerprint([1, 2, 3]), "duration": 10},
        timeout=60,
    )

    assert response.status_code == 200
    assert time.monotonic() - started_at < 30


def test_watchdog_passes_the_api_error_through(sleeping_stack: Path) -> None:
    """Fehlerantworten der API bleiben unveraendert (kein eigenes Fehlerbild)."""
    response = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params={"client": "e2e"}, timeout=60)

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "error": {"code": 2, "message": 'missing required parameter "fingerprint"'},
    }


def test_the_internal_health_endpoint_is_not_reachable(sleeping_stack: Path) -> None:
    """Deny-Regel am echten Container (R12).

    Der API-Dienst lauscht auf ``127.0.0.1:8081`` **im Container** — von
    aussen fuehrt kein Weg dorthin ausser durch den Waechter, und der
    reicht ``/_health`` nicht weiter.
    """
    response = httpx.get(f"{WATCHDOG_URL}/_health", timeout=30)

    assert response.status_code == 404


def test_a_stack_stopped_by_hand_is_noticed(sleeping_stack: Path) -> None:
    """Phase 16: der Waechter merkt einen Stopp, den er nicht veranlasst hat.

    Der Betreiber stoppt die Prozesse selbst (frueher ueber die
    Unraid-Oberflaeche, jetzt mit ``supervisorctl``) — bis Phase 15 blieb
    der Waechter danach auf ``bereit`` stehen, die erste Anfrage lief in
    eine tote API (503) und erst die zweite weckte. Der Zustandsabgleich im
    Hintergrund (Takt: 15 s) schliesst die Luecke.

    **Eigene Anfrageparameter** (Phase 17): mit denen des zweiten Tests
    laege die Antwort im Lookup-Cache, und die Anfrage wuerde gar nicht mehr
    wecken — dann pruefte dieser Test nichts mehr.
    """
    assert _status()["stack"]["state"] == "ready"

    _exec(sleeping_stack, *_SUPERVISORCTL, "stop", "api", "db")
    assert _sleeping(sleeping_stack)

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


def test_a_crash_does_not_look_like_sleep(sleeping_stack: Path) -> None:
    """M1b: ein abgestuerzter Prozess ist ``fehler``, nicht ``schlafend`` (R8).

    Der Nachweis der neuen Zustandskante am echten System — und der
    Unterschied, den es unter Docker nicht gab: ``supervisorctl stop``
    fuehrt zu ``STOPPED`` (gutartig, siehe Test darueber), ein ``SIGKILL``
    zu ``EXITED``. Beides sah in v1 gleich aus.

    Gemessen wird an der **API**, und der Absturz muss **bleiben**:
    ``autorestart=unexpected`` heilt einen einzelnen Kill binnen Sekunden
    (das ist Absicht, E15) — der Zustand waere wieder ``bereit``, bevor der
    Poller hinsieht. Der Test toetet den Prozess deshalb schneller, als er
    seine ``startsecs`` ueberleben kann, bis ``startretries`` verbraucht
    sind und supervisord aufgibt. Genau das ist der Fall, den der Betreiber
    sehen muss.
    """
    _wait("Stack wieder bereit", lambda: _status()["stack"]["state"] == "ready", timeout_s=180)

    # Schneller als `startsecs` (5 s): so kommt der Prozess nie nach
    # RUNNING zurueck, der Wiederholungszaehler laeuft leer, und am Ende
    # steht FATAL.
    _exec(
        sleeping_stack,
        "/bin/sh",
        "-c",
        "for i in $(seq 1 40); do "
        "supervisorctl -c /etc/supervisor/supervisord.conf signal KILL api >/dev/null 2>&1; "
        "sleep 0.5; done",
        check=False,
    )
    _wait(
        "supervisord gibt den API-Prozess auf",
        lambda: _states(sleeping_stack).get("api") == "FATAL",
        timeout_s=60,
        interval_s=2.0,
    )

    _wait(
        "Waechter meldet den Fehlerzustand",
        lambda: _status()["stack"]["state"] == "error",
        timeout_s=90,
        interval_s=3.0,
    )

    status = _status()["stack"]
    assert status["state"] == "error"
    assert status["detail"] and "api" in status["detail"]
    # Und der Weg heraus: der naechste Weckversuch.
    _exec(sleeping_stack, *_SUPERVISORCTL, "start", "api", check=False)
    _wait(
        "Stack wieder bereit",
        lambda: _status()["stack"]["state"] == "ready",
        timeout_s=180,
        interval_s=3.0,
    )


def test_a_cached_lookup_is_answered_without_waking(sleeping_stack: Path) -> None:
    """Phase 17: derselbe Lookup ein zweites Mal — bei gestopptem Stack.

    Die Definition of Done der Cache-Phase am echten Stack, in drei
    Schritten:

    1. Ein Lookup mit frischen Parametern geht an die API und wird
       eingelagert.
    2. Datenbank und API werden **von Hand gestoppt**.
    3. Dieselbe Anfrage kommt bytegleich zurueck, und danach schlafen sie
       immer noch: kein Weckvorgang.

    Der letzte Teil ist der eigentliche Nachweis. Ein Treffer, der doch
    weckt, waere hier nicht nur langsam — er waere sichtbar, weil die
    Prozesse danach liefen.
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

    _exec(sleeping_stack, *_SUPERVISORCTL, "stop", "api", "db")
    assert _sleeping(sleeping_stack)

    started_at = time.monotonic()
    second = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params=parameters, timeout=180)
    answered_s = time.monotonic() - started_at

    assert second.status_code == 200, second.text
    assert second.content == first.content
    assert second.headers["content-type"] == first.headers["content-type"]
    # Der Nachweis: Datenbank und API schlafen immer noch.
    assert _sleeping(sleeping_stack)
    print(json.dumps({"cache_hit_seconds": round(answered_s, 2)}))
    # Fuer den naechsten Test: derselbe Eintrag liegt jetzt im Cache.
    # **Ohne** ``client`` — der gehoert nicht zum Schluessel (Phase 17) und
    # wird dort je Fall eigens gesetzt.
    CACHED_LOOKUP.update({name: value for name, value in parameters.items() if name != "client"})


#: Die Parameter des gerade eingelagerten Lookups, ohne ``client`` — der
#: letzte Test prueft an ihnen, dass die Key-Pruefung **auch** vor dem Cache
#: greift. Sie werden erst im vorherigen Test gefuellt; die Tests dieser
#: Datei laufen bewusst der Reihe nach auf demselben Container.
CACHED_LOOKUP: dict[str, object] = {}

#: Der Key, den der letzte Test dem Waechter unterschiebt.
E2E_API_KEY = "e2e-testschluessel"

#: Was im Container laeuft, um ``auth.mode`` umzustellen und einen Key
#: anzulegen. Beides sind Schreibvorgaenge auf dem Datenverzeichnis; die
#: Admin-UI (M8) wird dafuer spaeter Routen haben. Der Waechter haelt seine
#: Konfiguration im Speicher (bewusst kein Datei-Watcher) — deshalb muss er
#: danach neu starten.
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


def test_apikey_mode_guards_even_the_cache(sleeping_stack: Path) -> None:
    """Phase 18: die Key-Pruefung steht vor dem Cache — am echten Waechter.

    Der billigste moegliche Nachweis und zugleich der schaerfste: Datenbank
    und API sind aus dem vorherigen Test **gestoppt**, die Antwort liegt im
    Cache. Es kostet einen Neustart des Waechter-**Prozesses** (nicht des
    Containers — das ist der Gewinn des Umbaus) und drei Anfragen.

    1. ``auth.mode: apikey`` setzen und einen Key anlegen (im Container, wie
       es die Admin-UI spaeter tut), dann den Waechter neu starten.
    2. Dieselbe, gecachte Anfrage **ohne** ``client`` -> 2/400, **mit
       falschem** ``client`` -> 4/400. Nichts wacht dabei auf.
    3. Mit dem richtigen Key kommt sie aus dem Cache zurueck — und die
       Prozesse schlafen immer noch.

    `/status` muss die ganze Zeit ohne Key antworten (§7).
    """
    assert CACHED_LOOKUP, "der vorherige Test hat nichts eingelagert"

    # Der Interpreter des Images liegt im venv, und das Arbeitsverzeichnis
    # ist bewusst ``/`` (Namespace-Paket-Falle, LEARNINGS) — beides steht so
    # im Dockerfile.
    _exec(sleeping_stack, "/app/.venv/bin/python", "-c", _ENABLE_APIKEY)
    _exec(sleeping_stack, *_SUPERVISORCTL, "restart", "watchdog")
    _wait("Waechter wieder erreichbar", _status, timeout_s=120)
    assert _sleeping(sleeping_stack)

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
    assert _sleeping(sleeping_stack)

    allowed = dict(CACHED_LOOKUP, client=E2E_API_KEY)
    accepted = httpx.get(f"{WATCHDOG_URL}/v2/lookup", params=allowed, timeout=60)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ok"
    assert _sleeping(sleeping_stack)
