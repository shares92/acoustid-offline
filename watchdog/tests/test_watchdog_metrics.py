"""``GET /metrics``: Format, Inhalt und die Zusage „weckt nie" (M2.5)."""

from __future__ import annotations

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient
from watchdog_stubs import FakeSupervisor

from acoustid_watchdog.metrics import CONTENT_TYPE, metric_names, render
from acoustid_watchdog.runs import RunKind, RunResult, finish_run, start_run
from acoustid_watchdog.service import WatchdogService
from shared.config import MetricsConfig


def _enable(service: WatchdogService, enabled: bool = True) -> None:
    service.config_store.save(
        service.config.model_copy(update={"metrics": MetricsConfig(enabled=enabled)})
    )


def _values(text: str, name: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(name)]


# --- Der Schalter -----------------------------------------------------------


def test_metrics_are_off_by_default(client: TestClient) -> None:
    """``metrics.enabled: false`` ist der Auslieferungszustand (§6)."""
    response = client.get("/metrics")

    assert response.status_code == 404
    # 404 und nicht 403: der Waechter verraet nicht, dass es den Endpunkt gibt.
    assert response.json()["status"] == "error"


def test_enabling_metrics_needs_no_restart(client: TestClient, service: WatchdogService) -> None:
    _enable(service)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert CONTENT_TYPE.startswith("text/plain")


def test_metrics_are_read_only(client: TestClient, service: WatchdogService) -> None:
    _enable(service)
    assert client.post("/metrics").status_code == 405


# --- Die Zusage: nichts wird geweckt ----------------------------------------


def test_metrics_open_no_network_connection(
    client: TestClient, service: WatchdogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariante §8.2 — baulich geprueft, wie bei `/status`.

    Ein Scraper im 15-Sekunden-Takt darf weder den Stack wecken noch Last
    auf dem Steuerweg erzeugen: der Prozess-Zustand kommt aus der
    Momentaufnahme des Pollers, nicht aus einer eigenen Abfrage.
    """
    _enable(service)

    def verboten(*args: object, **kwargs: object) -> None:
        raise AssertionError("/metrics hat eine Netzwerkverbindung geoeffnet")

    monkeypatch.setattr(socket, "socket", verboten)
    monkeypatch.setattr(socket, "create_connection", verboten)

    assert client.get("/metrics").status_code == 200


# --- Der Inhalt -------------------------------------------------------------


def test_the_expected_series_are_there(service: WatchdogService) -> None:
    """Die Aufgabenliste: Lookups, Cache-Quote, Weckvorgaenge, Zustand, Laeufe."""
    names = set(metric_names(render(service)))

    assert {
        "musicmeta_build_info",
        "musicmeta_stack_state",
        "musicmeta_lookups_total",
        "musicmeta_lookup_cache_hits_total",
        "musicmeta_lookup_cache_misses_total",
        "musicmeta_wakes_total",
        "musicmeta_stops_total",
        "musicmeta_idle_seconds",
        "musicmeta_job_running",
        "musicmeta_notifications_sent_total",
    } <= names


def test_exactly_one_stack_state_is_one(service: WatchdogService) -> None:
    lines = _values(render(service), "musicmeta_stack_state{")
    assert len(lines) == 5  # fuenf Zustaende aus §9
    assert sum(int(line.rsplit(" ", 1)[1]) for line in lines) == 1
    assert 'musicmeta_stack_state{state="sleeping"} 1' in lines


def test_the_process_states_come_from_the_pollers_snapshot(
    service: WatchdogService, supervisor: FakeSupervisor
) -> None:
    """Gelesen wird die Momentaufnahme — nicht supervisord.

    Der Dienst hat sie beim Start einmal erhoben (``wake.refresh()``);
    danach fuehrt sie der Poller alle 15 s nach. Genau deshalb kostet
    `/metrics` keinen einzigen Aufruf auf dem Steuerweg.
    """
    text = render(service)
    assert 'musicmeta_process_up{program="db"} 0' in text
    assert 'musicmeta_process_state{program="db",state="STOPPED"} 1' in text

    calls_before = len(supervisor.calls)
    render(service)
    assert len(supervisor.calls) == calls_before

    # Erst der naechste Abgleich bringt den neuen Stand.
    supervisor.programs["db"] = supervisor.programs["db"].__class__.RUNNING
    assert 'musicmeta_process_up{program="db"} 0' in render(service)
    service.wake.observe()
    assert 'musicmeta_process_up{program="db"} 1' in render(service)


def test_a_woken_stack_shows_its_processes_as_up(service: WatchdogService) -> None:
    asyncio.run(service.wake.ensure_ready(timeout_s=5))
    service.wake.observe()

    text = render(service)

    assert 'musicmeta_process_up{program="api"} 1' in text
    assert 'musicmeta_stack_state{state="ready"} 1' in text


def test_runs_are_counted_by_kind_and_outcome(service: WatchdogService) -> None:
    done = start_run(service.db, RunKind.ACOUSTID_DELTA, started_at="2026-08-05T04:00:00.000Z")
    finish_run(service.db, done, RunResult.SUCCESS, finished_at="2026-08-05T04:10:00.000Z")
    start_run(service.db, RunKind.BACKUP)

    text = render(service)

    assert 'musicmeta_runs_total{kind="acoustid-delta",result="success"} 1' in text
    assert 'musicmeta_runs_total{kind="backup",result="running"} 1' in text
    assert 'musicmeta_last_run_duration_seconds{kind="acoustid-delta"} 600' in text


def test_an_open_run_has_no_duration(service: WatchdogService) -> None:
    """Fehlende Werte werden ausgelassen, nicht als 0 erfunden."""
    start_run(service.db, RunKind.BACKUP)

    assert _values(render(service), "musicmeta_last_run_duration_seconds") == []


def test_lookups_count_hits_and_forwarded_requests(
    client: TestClient, service: WatchdogService
) -> None:
    """Durchgelassene Anfragen — abgewiesene haben nie eine Antwort erzeugt."""
    _enable(service)
    client.get("/v2/lookup", params={"client": "test", "fingerprint": "AQAA", "duration": 10})
    client.get("/v2/lookup", params={"client": "test", "fingerprint": "AQAA", "duration": 10})

    text = client.get("/metrics").text
    hits = service.cache.counters.hits

    assert hits == 1  # die zweite Anfrage kam aus dem Cache
    assert f"musicmeta_lookups_total {hits + service.activity.requests}" in text
    assert "musicmeta_lookup_cache_hits_total 1" in text


def test_the_build_info_names_the_baked_in_components(service: WatchdogService) -> None:
    """Dieselben Angaben wie in `/status` — wer den Drift-Guard debuggt, braucht sie."""
    text = render(service)
    assert 'postgresql_major="18"' in text
    assert "musicmeta_build_info{" in text


def test_label_values_are_escaped() -> None:
    """Ein Anfuehrungszeichen im Label wuerde das Format sonst zerreissen."""
    from acoustid_watchdog.metrics import _escape

    assert _escape('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_the_output_ends_with_a_newline(service: WatchdogService) -> None:
    """Prometheus verlangt den Abschluss-Zeilenumbruch."""
    assert render(service).endswith("\n")


def test_every_series_declares_help_and_type(service: WatchdogService) -> None:
    """Ohne HELP/TYPE ist eine Reihe fuer einen Menschen wertlos."""
    text = render(service)
    declared = set(metric_names(text))
    used = {
        line.split("{")[0].split(" ")[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }
    assert used == declared


def test_a_broken_state_database_is_an_honest_error(
    client: TestClient, service: WatchdogService
) -> None:
    """Lieber ein Fehler als geschoente Zahlen (Haltung von `/status`)."""
    _enable(service)
    service.db.close()

    response = client.get("/metrics")

    assert response.status_code == 500
    assert response.text.startswith("#")
