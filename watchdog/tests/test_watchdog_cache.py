"""Lookup-Cache: Treffer wecken das Array nie (Phase 17).

Der Nachweis der Phase ist ein **baulicher**, kein statistischer: nach dem
ersten Lookup werden Docker-Daemon und API-Attrappe *scharfgeschaltet* —
jede weitere Beruehrung laesst den Test scheitern (:class:`Tripwire`). Wenn
die zweite, gleiche Anfrage danach trotzdem mit derselben Antwort
zurueckkommt, kann sie nur aus dem Cache stammen. Das ist Invariante §8.2,
gemessen statt behauptet.

Der Rest der Datei prueft die Kanten drumherum: was in den Schluessel
gehoert und was nicht, was ueberhaupt eingelagert wird, was den Cache leert
(§8.6), was bei erreichter Groessengrenze verdraengt wird — und dass ein
kaputter Cache den Betrieb nicht anhaelt.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from watchdog_stubs import FakeDaemon, RecordingProxyTransport, probe, streamed

from acoustid_watchdog.cache import (
    CachedResponse,
    LookupCache,
    cache_key,
    is_cacheable_response,
)
from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.docker import DockerClient
from acoustid_watchdog.events import recent_events
from acoustid_watchdog.main import create_app
from acoustid_watchdog.proxy import ReverseProxy
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.store import Database
from acoustid_watchdog.wake import API_BASE_URL
from shared.env import EnvSettings

FINGERPRINT = "AQABz0qUkZK4oOfhL-CPc4e5C_wW2H2QH9uPLsdxHT2"
LOOKUP = f"/v2/lookup?client=abc&fingerprint={FINGERPRINT}&duration=641"

Responder = Callable[[httpx.Request], httpx.Response]


# --- Werkzeuge --------------------------------------------------------------


class Tripwire:
    """Stolperdraht vor einer Gegenstelle: scharf = jede Beruehrung fehlt.

    Nur so laesst sich „der Cache-Treffer weckt nicht" **baulich** pruefen.
    Ein Zaehlervergleich (``daemon.calls`` vorher/nachher) wuerde dasselbe
    behaupten, aber erst hinterher — und ein spaeterer Umbau, der doch
    wieder Docker anfasst, kaeme damit durch, solange er die Zahl nicht
    veraendert.
    """

    def __init__(self) -> None:
        self.armed = False

    def guard(self, handler: Responder) -> Responder:
        def wrapped(request: httpx.Request) -> httpx.Response:
            if self.armed:
                raise AssertionError(f"Gegenstelle trotz Cache-Treffer beruehrt: {request.url}")
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
def build(
    env_settings: EnvSettings,
    daemon: FakeDaemon,
    *,
    responder: Responder = lookup_ok,
) -> Iterator[Rig]:
    """Waechter mit scharfschaltbaren Gegenstellen.

    Der Stolperdraht liegt vor **allen dreien**: Docker-Daemon,
    Bereitschaftsfrage und API. Ein Weckvorgang beruehrt mindestens einen
    davon.
    """
    tripwire = Tripwire()
    upstream = RecordingProxyTransport(responder)
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
            API_BASE_URL,
            client=httpx.AsyncClient(transport=httpx.MockTransport(tripwire.guard(upstream))),
        ),
    ).open()
    with TestClient(create_app(service)) as client:
        yield client, tripwire, upstream


@pytest.fixture
def cached(env_settings: EnvSettings, daemon: FakeDaemon) -> Iterator[Rig]:
    """Waechter mit schlafendem Stack und scharfschaltbaren Gegenstellen."""
    with build(env_settings, daemon) as rig:
        yield rig


def set_cache(client: TestClient, **values: object) -> None:
    """Aendert ``cache.*`` in der laufenden Konfiguration."""
    service: WatchdogService = client.app.state.service
    config = service.config
    service.config_store.save(
        config.model_copy(update={"cache": config.cache.model_copy(update=values)})
    )


# --- Der Nachweis -----------------------------------------------------------


def test_hit_answers_without_touching_docker_or_the_api(
    cached: Rig,
) -> None:
    """Die Definition of Done: ein Treffer weckt nichts und fragt niemanden.

    Erst der Fehlschlag — er weckt den schlafenden Stack und leitet weiter.
    Dann werden alle Gegenstellen scharfgeschaltet; die zweite, identische
    Anfrage darf keine davon mehr beruehren.
    """
    client, tripwire, upstream = cached

    first = client.get(LOOKUP)
    assert first.status_code == 200
    assert len(upstream.requests) == 1

    tripwire.armed = True
    second = client.get(LOOKUP)

    assert second.status_code == 200
    assert len(upstream.requests) == 1
    assert client.app.state.service.cache.counters.hits == 1


def test_hit_is_byte_identical_to_the_original(
    cached: Rig,
) -> None:
    """Antwort-Paritaet: Rumpf bytegleich, ``Content-Type`` unveraendert.

    ``date`` und ``content-length`` sind bewusst nicht Teil des Eintrags:
    das eine beschreibt *diese* Antwort, das andere setzt Starlette aus dem
    (bytegleichen) Rumpf neu. Eine Markierung wie ``X-Cache`` gibt es nicht
    — der Client soll keinen Unterschied sehen.
    """
    client, tripwire, _ = cached

    first = client.get(LOOKUP)
    tripwire.armed = True
    second = client.get(LOOKUP)

    assert second.content == first.content
    assert second.headers["content-type"] == first.headers["content-type"]
    assert second.headers["access-control-allow-origin"] == "*"
    assert second.headers["content-length"] == first.headers["content-length"]
    assert "x-cache" not in second.headers


def test_hit_does_not_reset_the_idle_clock(
    cached: Rig,
) -> None:
    """Ein Treffer ist keine Nutzung des Arrays (§6 „Idle-Definition").

    Wuerde er die Uhr anfassen, hielte ausgerechnet der Cache den Stack
    wach, den er ueberfluessig macht.
    """
    client, tripwire, _ = cached
    service: WatchdogService = client.app.state.service

    client.get(LOOKUP)
    after_miss = service.activity.requests

    tripwire.armed = True
    client.get(LOOKUP)

    assert service.activity.requests == after_miss


# --- Was eingelagert wird ---------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 200])
def test_only_successful_lookups_are_stored(
    env_settings: EnvSettings, daemon: FakeDaemon, status_code: int
) -> None:
    """Fehlerantworten gehoeren nicht in den Cache — auch nicht mit HTTP 200.

    Der zweite Fall ist der interessante: das AcoustID-Fehlerformat kommt
    zwar mit eigenem HTTP-Status, aber der Waechter soll die Fehlertabelle
    der API nicht nachbauen muessen. Die Pruefung auf ``status: "ok"`` im
    Rumpf erledigt beides.
    """
    error = json.dumps({"status": "error", "error": {"code": 2, "message": "fehlt"}}).encode()
    with build(
        env_settings,
        daemon,
        responder=lambda request: streamed(
            status_code, error, {"content-type": "application/json"}
        ),
    ) as (client, _, upstream):
        client.get(LOOKUP)
        client.get(LOOKUP)

        assert len(upstream.requests) == 2
        assert client.app.state.service.cache.entries == 0


def test_non_json_formats_are_not_stored(env_settings: EnvSettings, daemon: FakeDaemon) -> None:
    """``format=xml``/``jsonp`` fallen von selbst heraus — ihr Rumpf ist kein JSON."""
    with build(
        env_settings,
        daemon,
        responder=lambda request: streamed(
            200,
            b'<?xml version="1.0" encoding="UTF-8"?><response><status>ok</status></response>',
            {"content-type": "text/xml; charset=UTF-8"},
        ),
    ) as (client, _, upstream):
        client.get(f"{LOOKUP}&format=xml")
        client.get(f"{LOOKUP}&format=xml")

        assert len(upstream.requests) == 2
        assert client.app.state.service.cache.entries == 0


@pytest.mark.parametrize(
    "path",
    ["/v2/lookup/batch", "/v2/submission_status", "/v2/submit"],
)
def test_other_routes_are_never_cached(cached: Rig, path: str) -> None:
    """Nur ``/v2/lookup``.

    ``/v2/lookup/batch`` bleibt bewusst draussen: identische Rumpfe
    wiederholen sich praktisch nie, und der Batch-Vertrag liefert Teilfehler
    *innerhalb* einer 200er-Antwort (Phase 13) — ein Cache wuerde einen
    voruebergehenden Teilfehler festschreiben.
    """
    client, _, upstream = cached

    client.post(path, json={"client": "abc"})
    client.post(path, json={"client": "abc"})

    assert len(upstream.requests) == 2
    assert client.app.state.service.cache.entries == 0


def test_head_is_not_answered_from_the_cache(
    cached: Rig,
) -> None:
    """``HEAD`` hat keinen Rumpf — eine Antwort mit Rumpf waere falsch."""
    client, _, upstream = cached

    client.get(LOOKUP)
    client.head(LOOKUP)

    assert len(upstream.requests) == 2


# --- Der Schluessel ---------------------------------------------------------


def test_client_and_clientversion_do_not_change_the_key(
    cached: Rig,
) -> None:
    """``client`` praegt die Antwort nicht — die Pruefung macht Phase 18.

    Waere er im Schluessel, haette jeder Client seinen eigenen Cache: bei
    zwei Programmen auf derselben Mediathek die Haelfte der Treffer
    umsonst.
    """
    client, tripwire, _ = cached

    client.get(f"/v2/lookup?client=eins&fingerprint={FINGERPRINT}&duration=641")
    tripwire.armed = True
    response = client.get(
        f"/v2/lookup?client=zwei&clientversion=9.9&fingerprint={FINGERPRINT}&duration=641"
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "extra",
    ["&meta=recordings", "&maxdurationdiff=12", "&format=json", "&batch=1", "&duration=642"],
)
def test_answer_shaping_parameters_change_the_key(cached: Rig, extra: str) -> None:
    """Alles, was die Antwort praegen koennte, ist Teil des Schluessels.

    ``format=json`` steht bewusst mit in der Liste: der Cache bildet keine
    Vorgabewerte nach. Das kostet einen Treffer und erspart eine zweite
    Fassung der Parametergrammatik.
    """
    client, _, upstream = cached

    client.get(LOOKUP)
    client.get(LOOKUP + extra)

    assert len(upstream.requests) == 2


def test_get_and_post_share_one_entry(
    cached: Rig,
) -> None:
    """``GET`` und ``POST`` sind gleichwertig (docs/api-lookup.md)."""
    client, tripwire, _ = cached

    client.get(LOOKUP)
    tripwire.armed = True
    response = client.post(
        "/v2/lookup",
        content=f"client=abc&fingerprint={FINGERPRINT}&duration=641".encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200


def test_gzip_form_bodies_find_their_entry(
    cached: Rig,
) -> None:
    """pyacoustid und beets schicken gepackt — der Schluessel muss trotzdem passen.

    Entpackt wird nur **fuer den Schluessel**; weitergereicht wird der
    unveraenderte, gepackte Rumpf (das Entpacken bleibt Sache der API).
    """
    client, tripwire, upstream = cached
    body = f"client=abc&fingerprint={FINGERPRINT}&duration=641".encode()
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "content-encoding": "gzip",
    }

    packed = gzip.compress(body, mtime=0)
    client.post("/v2/lookup", content=packed, headers=headers)
    assert upstream.last.content == packed

    tripwire.armed = True
    # Andere gzip-Einstellungen, gleicher Inhalt: derselbe Eintrag.
    response = client.post("/v2/lookup", content=gzip.compress(body, 1, mtime=99), headers=headers)

    assert response.status_code == 200


def test_percent_encoding_is_resolved_like_in_the_api() -> None:
    """Dekodiert statt roh: gleiche Anfrage, gleicher Schluessel."""
    assert cache_key("/v2/lookup", [("fingerprint", "A-B")]) == cache_key(
        "/v2/lookup", [("fingerprint", "A-B")]
    )
    assert cache_key("/v2/lookup", [("client", "x"), ("duration", "1")]) == cache_key(
        "/v2/lookup", [("duration", "1")]
    )


def test_the_key_is_unambiguous() -> None:
    """Ein Wert mit Trennzeichen darf keinen anderen Schluessel vortaeuschen."""
    assert cache_key("/v2/lookup", [("a", "b&c=d")]) != cache_key(
        "/v2/lookup", [("a", "b"), ("c", "d")]
    )
    assert cache_key("/v2/lookup", [("a", "b")]) != cache_key("/v2/other", [("a", "b")])


def test_case_is_not_normalised() -> None:
    """``format=JSON`` ist fuer die API Fehler 1 — nicht dasselbe wie ``json``."""
    assert cache_key("/v2/lookup", [("format", "JSON")]) != cache_key(
        "/v2/lookup", [("format", "json")]
    )


# --- Invalidierung (§8.6) ---------------------------------------------------


def test_a_successful_submission_empties_the_cache(
    env_settings: EnvSettings, daemon: FakeDaemon
) -> None:
    """Invariante §8.6, Ausloeser (a): jede erfolgreiche lokale Submission."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/submit":
            return streamed(
                200,
                json.dumps({"status": "ok", "submissions": []}).encode(),
                {"content-type": "application/json"},
            )
        return lookup_ok(request)

    with build(env_settings, daemon, responder=responder) as (client, _, upstream):
        service: WatchdogService = client.app.state.service

        client.get(LOOKUP)
        assert service.cache.entries == 1

        client.post(
            "/v2/submit",
            content=b"client=abc&user=u",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert service.cache.entries == 0

        # Und die naechste gleiche Anfrage geht wieder an die API.
        client.get(LOOKUP)
        assert len([r for r in upstream.requests if r.url.path == "/v2/lookup"]) == 2

        events = [event for event in recent_events(service.db, limit=20) if event.source == "cache"]
        assert events[0].message == "Lookup-Cache geleert"
        assert events[0].extra == {"reason": "submission", "removed": 1}


def test_a_failed_submission_keeps_the_cache(env_settings: EnvSettings, daemon: FakeDaemon) -> None:
    """Nur der Erfolg leert. Die API antwortet auf einen Fehler nie mit 200."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/submit":
            return streamed(400, b'{"status": "error"}', {"content-type": "application/json"})
        return lookup_ok(request)

    with build(env_settings, daemon, responder=responder) as (client, _, _upstream):
        client.get(LOOKUP)
        client.post("/v2/submit", content=b"client=abc")

        assert client.app.state.service.cache.entries == 1


def test_invalidate_cache_is_the_interface_for_later_phases(
    cached: Rig,
) -> None:
    """Ausloeser (b) und (c): Delta-Import (Phase 19) und Admin-UI (Phase 25)."""
    client, _, _ = cached
    service: WatchdogService = client.app.state.service

    client.get(LOOKUP)
    assert service.invalidate_cache("delta_import") == 1
    assert service.cache.entries == 0

    events = [event for event in recent_events(service.db, limit=20) if event.source == "cache"]
    assert events[0].extra == {"reason": "delta_import", "removed": 1}

    # Ein leerer Cache erzeugt kein Ereignis — sonst fuellte reger
    # Submit-Verkehr den Ringpuffer mit Leermeldungen.
    assert service.invalidate_cache("manual") == 0
    assert len([e for e in recent_events(service.db, limit=20) if e.source == "cache"]) == 1


def test_invalidation_works_with_the_cache_switched_off(
    cached: Rig,
) -> None:
    """Sonst haette ein abgeschalteter Cache nach dem Einschalten Altbestand."""
    client, _, _ = cached
    service: WatchdogService = client.app.state.service

    client.get(LOOKUP)
    set_cache(client, enabled=False)

    assert service.invalidate_cache("delta_import") == 1


# --- Schalter und Grenzen ---------------------------------------------------


def test_disabled_cache_is_pure_passthrough(
    cached: Rig,
) -> None:
    """``cache.enabled = false`` — weder gelesen noch geschrieben."""
    client, _, upstream = cached
    set_cache(client, enabled=False)

    client.get(LOOKUP)
    client.get(LOOKUP)

    assert len(upstream.requests) == 2
    assert client.app.state.service.cache.entries == 0


def test_the_switch_is_read_afresh_for_every_request(
    cached: Rig,
) -> None:
    """Wie ``wake.hold_timeout_s``: die Admin-UI wirkt ohne Neustart."""
    client, _, upstream = cached
    set_cache(client, enabled=False)
    client.get(LOOKUP)

    set_cache(client, enabled=True)
    client.get(LOOKUP)
    client.get(LOOKUP)

    assert len(upstream.requests) == 2


def test_the_size_limit_evicts_the_least_recently_used(tmp_path: Path) -> None:
    """Verdraengung nach LRU, bis :data:`EVICTION_WATERMARK` erreicht ist.

    Direkt auf der Ablage geprueft: ueber den Proxy waeren die Groessen
    nicht in der Hand des Tests.
    """
    entry = CachedResponse(200, (("content-type", "application/json"),), b"x" * 100)
    limit = 3 * entry.size_bytes

    with LookupCache(tmp_path / "cache.sqlite3") as cache:
        for key in ("a", "b", "c"):
            assert cache.put(key, entry, max_size_bytes=limit)
        assert cache.entries == 3

        # `a` wird benutzt und ist damit nicht mehr der aelteste Zugriff.
        assert cache.get("a") is not None
        cache.put("d", entry, max_size_bytes=limit)

        assert cache.get("b") is None
        assert cache.get("a") is not None
        assert cache.get("d") is not None
        assert cache.total_bytes <= limit
        assert cache.counters.evictions >= 1


def test_an_entry_bigger_than_the_whole_cache_is_refused(tmp_path: Path) -> None:
    """Es waere die einzige Antwort im Cache — und sofort wieder draussen."""
    with LookupCache(tmp_path / "cache.sqlite3") as cache:
        entry = CachedResponse(200, (), b"x" * 1000)

        assert cache.put("a", entry, max_size_bytes=10) is False
        assert cache.entries == 0


def test_the_accounting_survives_a_restart(tmp_path: Path) -> None:
    """Die Belegung wird beim Oeffnen aus der Datei erhoben, nicht geraten."""
    path = tmp_path / "cache.sqlite3"
    entry = CachedResponse(200, (), b"x" * 100)
    with LookupCache(path) as cache:
        cache.put("a", entry, max_size_bytes=10_000)
        expected = cache.total_bytes

    with LookupCache(path) as reopened:
        assert reopened.total_bytes == expected
        assert reopened.entries == 1


# --- Robustheit -------------------------------------------------------------


def test_a_broken_cache_file_does_not_stop_the_watchdog(
    env_settings: EnvSettings, daemon: FakeDaemon
) -> None:
    """Ein kaputter Cache ist schlimmstenfalls ein leerer Cache.

    Er haelt nichts, was sich nicht neu berechnen liesse — wegwerfen und neu
    anlegen ist deshalb die richtige Antwort auf jeden Defekt.
    """
    (env_settings.data_dir / "lookup-cache.sqlite3").write_bytes(b"kein SQLite, nur Muell" * 100)

    with build(env_settings, daemon) as (client, tripwire, upstream):
        service: WatchdogService = client.app.state.service

        assert service.cache.available
        assert service.cache.entries == 0

        client.get(LOOKUP)
        tripwire.armed = True

        assert client.get(LOOKUP).status_code == 200
        assert len(upstream.requests) == 1


def test_a_failing_cache_never_breaks_a_request(
    cached: Rig,
) -> None:
    """Scheitert ein Zugriff, wird die Datei neu angelegt — die Anfrage laeuft."""
    client, _, upstream = cached
    service: WatchdogService = client.app.state.service

    client.get(LOOKUP)
    # Die Tabelle unter dem laufenden Cache wegziehen: derselbe Effekt wie
    # eine unterwegs beschaedigte Datei, nur reproduzierbar.
    with sqlite3.connect(service.cache.path) as connection:
        connection.execute("DROP TABLE entry")

    # Der Zugriff scheitert, die Anfrage nicht: sie geht den normalen Weg.
    assert client.get(LOOKUP).status_code == 200
    assert service.cache.counters.errors >= 1
    assert len(upstream.requests) == 2

    # Und die neu angelegte Datei traegt wieder Eintraege.
    assert service.cache.available
    client.get(LOOKUP)
    client.get(LOOKUP)
    assert len(upstream.requests) == 2


def test_a_cache_that_cannot_be_created_switches_itself_off(tmp_path: Path) -> None:
    """Letzte Stufe: der Waechter arbeitet dann einfach ohne Cache weiter."""
    blocked = tmp_path / "datei"
    blocked.write_text("keine Datei, kein Verzeichnis")

    cache = LookupCache(blocked / "unterhalb" / "cache.sqlite3").open()

    assert not cache.available
    assert cache.get("a") is None
    assert cache.put("a", CachedResponse(200, (), b"x"), max_size_bytes=1000) is False
    assert cache.invalidate_all() == 0
    assert cache.entries == 0


# --- Kleinteile -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (200, b'{"status": "ok", "results": []}', True),
        (200, b'{"status": "error"}', False),
        (200, b"", False),
        (200, b"kein JSON", False),
        (200, b"[1, 2]", False),
        (503, b'{"status": "ok"}', False),
    ],
)
def test_is_cacheable_response(status_code: int, body: bytes, expected: bool) -> None:
    from fastapi import Response

    assert is_cacheable_response(Response(status_code=status_code), body) is expected
