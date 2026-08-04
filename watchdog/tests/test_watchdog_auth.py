"""Auth und Rate-Limit am Proxy (Phase 18).

Der Nachweis der Phase hat zwei Haelften:

1. **Der Modus ``none`` bleibt exakt, wie er war.** ``client`` wird
   akzeptiert und ignoriert; ob er fehlt, entscheidet weiterhin die API.
   Dafuer steht der erste Abschnitt — er ist der Regressionstest gegen die
   Phasen 15-17.
2. **Der Modus ``apikey`` schuetzt auch den billigen Weg.** Eine abgewiesene
   Anfrage beruehrt weder Docker noch API — und ein Cache-Treffer ist
   genauso geschuetzt wie eine weitergeleitete Anfrage. Gemessen wird das
   wie in Phase 17 mit dem :class:`Tripwire`: nach dem Scharfschalten laesst
   **jede** Beruehrung einer Gegenstelle den Test scheitern. Ein „das prueft
   schon vorher" waere sonst eine Behauptung ueber die Reihenfolge im Code;
   so ist es eine Messung.

Der dritte Abschnitt prueft das Rate-Limit an seiner eigenen Uhr (kein
``sleep`` in der Suite), der vierte den Speicherhaushalt des Limiters.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from watchdog_stubs import FakeDaemon, RecordingProxyTransport, probe, streamed

from acoustid_watchdog.auth import KNOWN_CLIENT_KEYS, ApiKeyAuthenticator, AuthOutcome, hash_key
from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.docker import DockerClient
from acoustid_watchdog.main import create_app
from acoustid_watchdog.proxy import ReverseProxy
from acoustid_watchdog.ratelimit import WINDOW_S, IpRateLimiter
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.store import Database, utc_now
from shared.env import EnvSettings
from shared.models import AuthMode

FINGERPRINT = "AQABz0qUkZK4oOfhL-CPc4e5C_wW2H2QH9uPLsdxHT2"
KEY = "geheimer-testschluessel"

Responder = Callable[[httpx.Request], httpx.Response]


# --- Werkzeuge --------------------------------------------------------------


class Tripwire:
    """Stolperdraht vor einer Gegenstelle (Muster aus Phase 17)."""

    def __init__(self) -> None:
        self.armed = False

    def guard(self, handler: Responder) -> Responder:
        def wrapped(request: httpx.Request) -> httpx.Response:
            if self.armed:
                raise AssertionError(f"Gegenstelle trotz Abweisung beruehrt: {request.url}")
            return handler(request)

        return wrapped


def lookup_ok(request: httpx.Request) -> httpx.Response:
    """Die Vorgabeantwort der API-Attrappe: ein erfolgreicher Lookup."""
    return streamed(
        200,
        json.dumps({"status": "ok", "results": []}).encode(),
        {"content-type": "application/json; charset=UTF-8", "access-control-allow-origin": "*"},
    )


#: Was ein Test in die Hand bekommt: Client, Stolperdraht, API-Attrappe.
Rig = tuple[TestClient, Tripwire, RecordingProxyTransport]


@contextmanager
def build(env_settings: EnvSettings, daemon: FakeDaemon) -> Iterator[Rig]:
    """Waechter mit schlafendem Stack und scharfschaltbaren Gegenstellen."""
    tripwire = Tripwire()
    upstream = RecordingProxyTransport(lookup_ok)
    health = tripwire.guard(lambda request: httpx.Response(200 if daemon.all_running else 503))
    service = WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        docker=DockerClient(
            client=httpx.Client(transport=httpx.MockTransport(tripwire.guard(daemon)))
        ),
        probe=probe(health),
        proxy=ReverseProxy(
            env_settings.api_base_url,
            client=httpx.AsyncClient(transport=httpx.MockTransport(tripwire.guard(upstream))),
        ),
    ).open()
    with TestClient(create_app(service)) as client:
        yield client, tripwire, upstream


@pytest.fixture
def rig(env_settings: EnvSettings, daemon: FakeDaemon) -> Iterator[Rig]:
    with build(env_settings, daemon) as built:
        yield built


def configure(client: TestClient, section: str, **values: object) -> None:
    """Aendert einen Abschnitt der laufenden Konfiguration."""
    service: WatchdogService = client.app.state.service
    config = service.config
    service.config_store.save(
        config.model_copy(update={section: getattr(config, section).model_copy(update=values)})
    )


def add_key(client: TestClient, key: str, *, label: str = "Testclient", active: bool = True) -> int:
    """Legt einen Key an — wie es die Admin-UI in Phase 26 tun wird."""
    service: WatchdogService = client.app.state.service
    with service.db.transaction() as tx:
        cursor = tx.execute(
            "INSERT INTO api_key (label, key_hash, active, created_at) VALUES (?, ?, ?, ?)",
            (label, hash_key(key), 1 if active else 0, utc_now()),
        )
    return int(cursor.lastrowid or 0)


def last_used(client: TestClient, key_id: int) -> str | None:
    service: WatchdogService = client.app.state.service
    with service.db.transaction() as tx:
        row = tx.execute("SELECT last_used_at FROM api_key WHERE id = ?", (key_id,)).fetchone()
    return None if row["last_used_at"] is None else str(row["last_used_at"])


def lookup(test_client: TestClient, **params: object) -> httpx.Response:
    """Ein ``GET /v2/lookup`` mit festen Parametern; ``client`` kommt dazu."""
    return test_client.get(
        "/v2/lookup", params={"fingerprint": FINGERPRINT, "duration": 641, **params}
    )


# --- Modus `none`: alles bleibt, wie es war ---------------------------------


def test_none_mode_accepts_and_ignores_the_client(rig: Rig) -> None:
    """§7: im Modus ``none`` wird ``client`` akzeptiert und ignoriert."""
    client, _, upstream = rig

    response = lookup(client, client="voellig-unbekannt")

    assert response.status_code == 200
    assert len(upstream.requests) == 1
    # Der Waechter hat gar nicht geprueft — der Zaehler steht auf null.
    assert client.app.state.service.auth.counters.checked == 0


def test_none_mode_leaves_a_missing_client_to_the_api(rig: Rig) -> None:
    """Ohne ``client`` entscheidet weiterhin die API (Fehler 2), nicht wir."""
    client, _, upstream = rig

    response = client.get("/v2/lookup", params={"fingerprint": FINGERPRINT})

    assert response.status_code == 200  # was die Attrappe eben antwortet
    assert len(upstream.requests) == 1
    assert client.app.state.service.auth.counters.checked == 0


# --- Modus `apikey` ---------------------------------------------------------


def test_apikey_mode_accepts_a_registered_key(rig: Rig) -> None:
    """Ein aktiver Key aus der Tabelle geht durch — und weckt wie bisher."""
    client, _, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)

    response = lookup(client, client=KEY)

    assert response.status_code == 200
    assert len(upstream.requests) == 1
    counters = client.app.state.service.auth.counters
    assert (counters.checked, counters.accepted, counters.rejected) == (1, 1, 0)


def test_apikey_mode_rejects_an_unknown_key_without_waking(rig: Rig) -> None:
    """Fehler 4/400 — und der Stolperdraht bleibt unberuehrt (§8.2)."""
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)

    tripwire.armed = True
    response = lookup(client, client="falsch")

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "error": {"code": 4, "message": "invalid API key"},
    }
    assert response.headers["access-control-allow-origin"] == "*"
    assert upstream.requests == []
    assert client.app.state.service.auth.counters.rejected_invalid == 1


def test_apikey_mode_rejects_a_missing_key_with_error_2(rig: Rig) -> None:
    """Fehlt ``client``, antwortet der Waechter wie das Original: Fehler 2."""
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)

    tripwire.armed = True
    response = client.get("/v2/lookup", params={"fingerprint": FINGERPRINT})

    assert response.status_code == 400
    assert response.json() == {
        "status": "error",
        "error": {"code": 2, "message": 'missing required parameter "client"'},
    }
    assert upstream.requests == []
    assert client.app.state.service.auth.counters.rejected_missing == 1


def test_apikey_mode_rejects_an_inactive_key_like_an_unknown_one(rig: Rig) -> None:
    """Ein gesperrter Key ist von einem unbekannten nicht zu unterscheiden."""
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY, active=False)

    tripwire.armed = True
    response = lookup(client, client=KEY)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == 4
    assert upstream.requests == []


def test_apikey_mode_reads_the_key_from_the_form_body(rig: Rig) -> None:
    """Picard schickt POST form-urlencoded — der Key steht dann im Rumpf."""
    client, _, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)

    response = client.post(
        "/v2/lookup",
        content=urlencode({"client": KEY, "fingerprint": FINGERPRINT, "duration": 641}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    # Der Rumpf ist trotz der Lesung unveraendert weitergegangen.
    assert f"client={KEY}" in upstream.payload()


def test_apikey_mode_reads_the_key_from_a_gzipped_body(rig: Rig) -> None:
    """pyacoustid/beets packen den Rumpf — gelesen wird trotzdem der Klartext."""
    client, _, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)
    body = gzip.compress(
        urlencode({"client": KEY, "fingerprint": FINGERPRINT, "duration": 641}).encode()
    )

    response = client.post(
        "/v2/lookup",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-encoding": "gzip",
        },
    )

    assert response.status_code == 200
    # Weitergeleitet wurde der **gepackte** Rumpf: das Entpacken bleibt
    # Sache der API (Phase 17).
    assert upstream.last.content == body


def test_apikey_mode_reads_the_key_from_the_batch_envelope(rig: Rig) -> None:
    """``/v2/lookup/batch`` traegt seinen ``client`` in der JSON-Huelle."""
    client, _, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)

    response = client.post(
        "/v2/lookup/batch",
        json={"client": KEY, "queries": [{"fingerprint": FINGERPRINT, "duration": 641}]},
    )

    assert response.status_code == 200
    assert len(upstream.requests) == 1


def test_apikey_mode_rejects_a_batch_without_client(rig: Rig) -> None:
    """Keine Huelle, kein Key: Fehler 2 — ohne den Stack anzufassen."""
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)

    tripwire.armed = True
    response = client.post(
        "/v2/lookup/batch",
        json={"queries": [{"fingerprint": FINGERPRINT, "duration": 641}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == 2
    assert upstream.requests == []


def test_apikey_mode_protects_submit_too(rig: Rig) -> None:
    """``/v2/submit`` ist keine Ausnahme — auch dort prueft der Waechter."""
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)

    tripwire.armed = True
    response = client.post(
        "/v2/submit",
        content=urlencode({"client": "falsch", "user": "u", "fingerprint.0": FINGERPRINT}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == 4
    assert upstream.requests == []


def test_apikey_mode_answers_413_when_the_body_hides_the_key(rig: Rig) -> None:
    """Ueber 1 MiB ist der Key nicht mehr auffindbar -> 19/413 wie die API.

    Die API antwortet auf einen solchen Rumpf ohnehin mit 19/413
    (``MAX_BODY_BYTES``); ihn dafuer erst durchzureichen hiesse, den Stack
    fuer eine Anfrage zu wecken, deren Antwort schon feststeht.
    """
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)
    oversized = b"fingerprint=" + b"A" * (1024 * 1024 + 16)

    tripwire.armed = True
    response = client.post(
        "/v2/lookup",
        content=oversized,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "status": "error",
        "error": {"code": 19, "message": "request too large"},
    }
    assert upstream.requests == []


def test_apikey_mode_finds_a_key_in_the_query_of_an_oversized_post(rig: Rig) -> None:
    """Steht der Key im Query-String, braucht es den Rumpf gar nicht."""
    client, _, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)

    response = client.post(
        f"/v2/lookup?client={KEY}",
        content=b"fingerprint=" + b"A" * (1024 * 1024 + 16),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert len(upstream.requests) == 1


def test_a_submit_still_forwards_its_body_and_clears_the_cache(rig: Rig) -> None:
    """Der ``apikey``-Modus puffert den Submit-Rumpf — geaendert wird nichts.

    Neu an diesem Weg ist die Pufferung: vor Phase 18 lief ein Submit-Rumpf
    ungelesen durch. Geprueft wird deshalb beides — dass genau dieselben
    Bytes ankommen und dass die Cache-Invalidierung (§8.6) weiter greift.
    """
    client, _, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)
    body = urlencode({"client": KEY, "user": "u", "fingerprint.0": FINGERPRINT, "duration.0": 641})

    # Erst etwas in den Cache legen, damit die Leerung messbar ist.
    assert lookup(client, client=KEY).status_code == 200
    assert client.app.state.service.cache.entries == 1

    response = client.post(
        "/v2/submit",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert upstream.payload() == body
    assert client.app.state.service.cache.entries == 0


def test_apikey_mode_answers_413_when_a_gzipped_body_unpacks_too_large(rig: Rig) -> None:
    """Auch entpackt gilt die 1-MiB-Grenze — und damit 19/413 wie in der API."""
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)
    body = gzip.compress(b"fingerprint=" + b"A" * (1024 * 1024 + 16))

    tripwire.armed = True
    response = client.post(
        "/v2/lookup",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-encoding": "gzip",
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == 19
    assert upstream.requests == []


def test_apikey_mode_treats_a_broken_gzip_body_as_a_missing_key(rig: Rig) -> None:
    """Kaputtes gzip gilt der API als leerer Rumpf — dann fehlt ``client``."""
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)

    tripwire.armed = True
    response = client.post(
        "/v2/lookup",
        content=b"das ist kein gzip",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-encoding": "gzip",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == 2
    assert upstream.requests == []


def test_status_stays_open_in_apikey_mode(rig: Rig) -> None:
    """`/status` ist die Bereitschaftsanzeige (§7) — sie braucht keinen Key."""
    client, _, _ = rig
    configure(client, "auth", mode=AuthMode.APIKEY)

    response = client.get("/status")

    assert response.status_code == 200
    assert response.json()["stack"]["state"] == "sleeping"


# --- Whitelist bekannter Drittclients ---------------------------------------


@pytest.mark.parametrize("known", sorted(KNOWN_CLIENT_KEYS))
def test_known_client_keys_are_rejected_by_default(rig: Rig, known: str) -> None:
    """Default aus: Picards und beets' Keys sind oeffentlich bekannt."""
    client, _, _ = rig
    configure(client, "auth", mode=AuthMode.APIKEY)

    assert lookup(client, client=known).json()["error"]["code"] == 4


@pytest.mark.parametrize("known", sorted(KNOWN_CLIENT_KEYS))
def test_known_client_keys_pass_when_the_switch_is_on(rig: Rig, known: str) -> None:
    """Eingeschaltet laeuft ein unveraenderter Picard gegen die Instanz."""
    client, _, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY, allow_known_client_keys=True)

    assert lookup(client, client=known).status_code == 200
    assert len(upstream.requests) == 1
    assert client.app.state.service.auth.counters.accepted_known_client == 1


def test_the_switch_does_not_replace_the_table(rig: Rig) -> None:
    """Eingeschaltet bleibt jeder andere unbekannte Key trotzdem draussen."""
    client, _, _ = rig
    configure(client, "auth", mode=AuthMode.APIKEY, allow_known_client_keys=True)

    assert lookup(client, client="irgendwas").json()["error"]["code"] == 4


# --- „Zuletzt benutzt" ------------------------------------------------------


def test_last_used_is_written_on_the_first_request(rig: Rig) -> None:
    client, _, _ = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    key_id = add_key(client, KEY)
    assert last_used(client, key_id) is None

    lookup(client, client=KEY)

    assert last_used(client, key_id) is not None


def test_last_used_is_throttled(env_settings: EnvSettings) -> None:
    """Nicht jede Anfrage kostet eine Schreibtransaktion (Modul-Entscheid).

    Geprueft am Baustein statt am Proxy: die Drosselung haengt an einer Uhr,
    und eine Uhr laesst sich nur dort stellen.
    """
    now = 1000.0
    # Marke statt Zeitvergleich: ``utc_now()`` hat Millisekunden, und zwei
    # Schreibvorgaenge in derselben Millisekunde waeren nicht zu
    # unterscheiden. Ueberschrieben wird die Marke nur, wenn wirklich
    # geschrieben wurde.
    marker = "1970-01-01T00:00:00.000Z"
    with Database.for_data_dir(env_settings.data_dir) as db:
        auth = ApiKeyAuthenticator(db, touch_interval_s=60.0, clock=lambda: now)
        with db.transaction() as tx:
            tx.execute(
                "INSERT INTO api_key (label, key_hash, active, created_at) VALUES (?, ?, 1, ?)",
                ("Test", hash_key(KEY), utc_now()),
            )

        assert auth.check(KEY, allow_known=False).ok
        assert _stored_last_used(db) is not None

        _mark_last_used(db, marker)
        now += 30.0
        assert auth.check(KEY, allow_known=False).ok
        assert _stored_last_used(db) == marker  # nichts geschrieben

        now += 31.0
        assert auth.check(KEY, allow_known=False).ok
        assert _stored_last_used(db) != marker  # Frist abgelaufen


def _stored_last_used(db: Database) -> str | None:
    with db.transaction() as tx:
        row = tx.execute("SELECT last_used_at FROM api_key").fetchone()
    return None if row["last_used_at"] is None else str(row["last_used_at"])


def _mark_last_used(db: Database, value: str) -> None:
    with db.transaction() as tx:
        tx.execute("UPDATE api_key SET last_used_at = ?", (value,))


def test_a_broken_database_does_not_open_the_door(env_settings: EnvSettings) -> None:
    """Ein Datenbankfehler heisst „nicht autorisiert", nicht „egal, durch"."""
    db = Database.for_data_dir(env_settings.data_dir)  # nie geoeffnet
    auth = ApiKeyAuthenticator(db)

    assert auth.check(KEY, allow_known=False).outcome is AuthOutcome.INVALID


# --- Cache-Hit-Pfad mit Auth ------------------------------------------------


def test_a_cache_hit_still_needs_a_valid_key(rig: Rig) -> None:
    """Die Definition of Done: Auth schuetzt auch den Weg ohne API.

    Erst wird die Antwort mit gueltigem Key eingelagert, dann werden alle
    Gegenstellen scharfgeschaltet. Danach:

    * ohne Key -> 400, obwohl die Antwort im Cache laege;
    * mit falschem Key -> 400;
    * mit gueltigem Key -> dieselbe Antwort, ohne dass irgendetwas geweckt
      wurde.
    """
    client, tripwire, upstream = rig
    configure(client, "auth", mode=AuthMode.APIKEY)
    add_key(client, KEY)

    first = lookup(client, client=KEY)
    assert first.status_code == 200
    assert len(upstream.requests) == 1

    tripwire.armed = True

    assert (
        client.get("/v2/lookup", params={"fingerprint": FINGERPRINT, "duration": 641}).json()[
            "error"
        ]["code"]
        == 2
    )
    assert lookup(client, client="falsch").json()["error"]["code"] == 4
    assert client.app.state.service.cache.counters.hits == 0

    second = lookup(client, client=KEY)
    assert second.status_code == 200
    assert second.content == first.content
    assert client.app.state.service.cache.counters.hits == 1
    assert len(upstream.requests) == 1


def test_a_rate_limited_request_never_reaches_the_cache_or_the_stack(rig: Rig) -> None:
    """Das Limit steht ganz vorn — auch vor dem Cache."""
    client, tripwire, upstream = rig
    configure(client, "ratelimit", per_ip_per_min=2)

    assert lookup(client, client="a").status_code == 200
    assert lookup(client, client="a").status_code == 200  # Treffer aus dem Cache

    tripwire.armed = True
    third = lookup(client, client="a")

    assert third.status_code == 429
    assert len(upstream.requests) == 1
    counters = client.app.state.service.ratelimit.counters
    assert (counters.allowed, counters.rejected) == (2, 1)


# --- Rate-Limit -------------------------------------------------------------


def test_rate_limit_answers_429_with_retry_after(rig: Rig) -> None:
    """§7 „Fehlerverhalten": ``429`` + ``Retry-After``, im AcoustID-Format."""
    client, _, _ = rig
    configure(client, "ratelimit", per_ip_per_min=3)

    for _ in range(3):
        assert lookup(client, client="a").status_code == 200
    response = lookup(client, client="a")

    assert response.status_code == 429
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == 14
    # Der Wortlaut des Originals, mit unserem Limit als Rate je Sekunde.
    assert response.json()["error"]["message"] == (
        "rate limit (0.050000 requests per second) exceeded, try again later"
    )
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert response.headers["access-control-allow-origin"] == "*"


def test_rate_limit_is_active_in_mode_none(rig: Rig) -> None:
    """Das Limit haengt nicht am Auth-Modus (§7)."""
    client, _, _ = rig
    service: WatchdogService = client.app.state.service
    assert service.config.auth.mode.value == "none"
    configure(client, "ratelimit", per_ip_per_min=1)

    assert lookup(client, client="a").status_code == 200
    assert lookup(client, client="b").status_code == 429


def test_rate_limit_does_not_apply_to_status(rig: Rig) -> None:
    """`/status` bleibt erreichbar — auch wenn `/v2/*` schon abwehrt."""
    client, _, _ = rig
    configure(client, "ratelimit", per_ip_per_min=1)

    assert lookup(client, client="a").status_code == 200
    assert lookup(client, client="a").status_code == 429
    for _ in range(5):
        assert client.get("/status").status_code == 200


def test_the_window_slides(rig: Rig) -> None:
    """Nach 60 s ist das Kontingent wieder da — gemessen an eigener Uhr."""
    client, _, _ = rig
    configure(client, "ratelimit", per_ip_per_min=2)
    service: WatchdogService = client.app.state.service
    now = 500.0
    service.ratelimit = IpRateLimiter(clock=lambda: now)

    assert lookup(client, client="a").status_code == 200
    assert lookup(client, client="b").status_code == 200
    assert lookup(client, client="c").status_code == 429

    now += WINDOW_S + 0.1
    assert lookup(client, client="d").status_code == 200


def test_retry_after_names_the_moment_the_oldest_hit_expires() -> None:
    """``Retry-After`` ist gerechnet, nicht geraten."""
    now = 0.0
    limiter = IpRateLimiter(clock=lambda: now)

    assert limiter.check("10.0.0.1", limit=2).allowed
    now += 10.0
    assert limiter.check("10.0.0.1", limit=2).allowed
    now += 5.0

    decision = limiter.check("10.0.0.1", limit=2)
    assert decision.rejected
    # Der aelteste Eintrag liegt 15 s zurueck, faellt also in 45 s heraus.
    assert decision.retry_after_s == 45


def test_a_rejected_request_does_not_extend_the_block() -> None:
    """Wer weiter anfragt, sperrt sich nicht selbst dauerhaft aus."""
    now = 0.0
    limiter = IpRateLimiter(clock=lambda: now)

    assert limiter.check("10.0.0.1", limit=1).allowed
    now += 30.0
    for _ in range(5):
        assert limiter.check("10.0.0.1", limit=1).rejected

    now += 30.1
    assert limiter.check("10.0.0.1", limit=1).allowed


def test_each_ip_has_its_own_budget() -> None:
    """Ein lauter Nachbar bremst niemanden sonst."""
    limiter = IpRateLimiter()

    assert limiter.check("10.0.0.1", limit=1).allowed
    assert limiter.check("10.0.0.1", limit=1).rejected
    assert limiter.check("10.0.0.2", limit=1).allowed


def test_a_lowered_limit_takes_effect_immediately() -> None:
    """Die Konfiguration wird je Anfrage frisch gelesen (Muster Phase 17)."""
    limiter = IpRateLimiter(clock=lambda: 0.0)

    for _ in range(5):
        assert limiter.check("10.0.0.1", limit=10).allowed
    assert limiter.check("10.0.0.1", limit=3).rejected


# --- Speicherhaushalt des Limiters ------------------------------------------


def test_the_limiter_caps_the_number_of_tracked_ips() -> None:
    """Der Deckel haelt — auch unter gefaelschten Absendern."""
    limiter = IpRateLimiter(max_tracked_ips=8, clock=lambda: 0.0)

    for number in range(200):
        limiter.check(f"10.0.0.{number}", limit=5)

    assert limiter.tracked_ips == 8
    assert limiter.counters.evicted == 192


def test_the_limiter_forgets_idle_ips() -> None:
    """Wer laenger als das Fenster schweigt, gibt seinen Platz zurueck."""
    now = 0.0
    limiter = IpRateLimiter(clock=lambda: now)

    for number in range(50):
        limiter.check(f"10.0.1.{number}", limit=5)
    assert limiter.tracked_ips == 50

    # Der Aufraeumlauf laeuft hoechstens einmal je Minute; nach Fenster
    # **und** Intervall ist jeder alte Eintrag faellig.
    now += WINDOW_S + 1.0
    limiter.check("10.0.2.1", limit=5)

    assert limiter.tracked_ips == 1
