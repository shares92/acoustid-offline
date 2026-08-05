"""Start des Waechter-Dienstes: Erststart, Neustart, Zustand (Phase 14)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from acoustid_watchdog.admin import load_admin_user
from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.events import EventLevel, recent_events
from acoustid_watchdog.jobs import INDEX_BUSY_FILENAME
from acoustid_watchdog.runs import RunKind, RunResult, latest_run, running_runs, start_run
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.store import DB_FILENAME, SCHEMA_VERSION, Database, utc_now
from shared.env import EnvSettings
from shared.models import StackState


def test_first_start_creates_database_config_and_admin(
    env_settings: EnvSettings, data_dir: Path
) -> None:
    with WatchdogService.from_env(env_settings) as service:
        assert (data_dir / DB_FILENAME).is_file()
        assert env_settings.config_path.is_file()
        assert service.db.schema_version == SCHEMA_VERSION
        assert load_admin_user(service.db) is not None


def test_first_start_notes_the_password_event_without_the_password(
    env_settings: EnvSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """Das Klartext-Passwort steht nur im Containerlog, nie im ``event_log``.

    Das Ereignis-Log ist persistent und wird ueber `/admin/logs` angezeigt —
    also hinter genau der Anmeldung, fuer die das Passwort gilt.
    """
    with caplog.at_level(logging.WARNING), WatchdogService.from_env(env_settings) as service:
        events = recent_events(service.db, limit=10)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    password = logged.rsplit("Passwort: ", 1)[1].strip()
    assert password

    stored = "\n".join(f"{event.message} {event.extra}" for event in events)
    assert password not in stored
    assert any("Admin-Passwort erzeugt" in event.message for event in events)


def test_start_writes_a_start_event(env_settings: EnvSettings) -> None:
    with WatchdogService.from_env(env_settings) as service:
        messages = [event.message for event in recent_events(service.db, limit=10)]
    assert "Waechter gestartet" in messages


def test_restart_neither_recreates_the_admin_nor_repeats_the_first_start_event(
    env_settings: EnvSettings,
) -> None:
    with WatchdogService.from_env(env_settings) as first:
        before = load_admin_user(first.db)

    with WatchdogService.from_env(env_settings) as second:
        after = load_admin_user(second.db)
        events = recent_events(second.db, limit=20)

    assert before is not None and after is not None
    assert after.password_hash == before.password_hash
    first_start_events = [event for event in events if "Admin-Passwort erzeugt" in event.message]
    assert len(first_start_events) == 1


def test_fresh_service_reports_a_sleeping_stack(service: WatchdogService) -> None:
    """`sleeping` ist der Gutzustand, nicht der Mangel (ARCHITECTURE §9)."""
    assert service.state.state is StackState.SLEEPING


def test_state_change_updates_since_only_on_a_real_change(service: WatchdogService) -> None:
    before = service.state.status
    assert service.state.to(StackState.SLEEPING) == before

    started = service.state.to(StackState.STARTING)
    assert started.state is StackState.STARTING
    assert started.since >= before.since


def test_every_state_change_is_an_event(service: WatchdogService) -> None:
    """Der Lebenszyklus steht hinterher im Ereignis-Log (Phase 16)."""
    service.state.to(StackState.STARTING)
    service.state.to(StackState.READY)

    events = [event for event in recent_events(service.db, limit=10) if event.source == "stack"]
    assert [event.message for event in events] == [
        "Stack-Zustand: bereit",
        "Stack-Zustand: startet",
    ]
    assert events[0].extra == {"state": "ready", "state_previous": "starting", "detail": None}


def test_update_config_writes_the_file_signal_and_event(service: WatchdogService) -> None:
    marker = service.update_config(service.config, reason="test")
    assert marker.generation == 1
    assert service.config_store.signal.read() == marker

    events = recent_events(service.db, limit=5)
    change = next(event for event in events if event.message == "Konfiguration geaendert")
    assert change.level is EventLevel.INFO
    assert change.extra == {"reason": "test", "reload_generation": 1}


def test_service_holds_no_connection_to_the_array(service: WatchdogService) -> None:
    """Kein Postgres-Pool, kein Index-Client — das ist die Invariante §8.2.

    Der Waechter kommt ohne ``MMO_DB_PASSWORD`` aus; wuerde hier je eine
    Datenbankressource entstehen, waere die Zusage „die Admin-UI arbeitet
    bei schlafendem Stack" gebrochen.

    Seit Phase 15 gehoeren Prozess-Steuerung, Bereitschaftsfrage, Proxy und
    Weck-Koordination dazu. Alle vier sprechen nur mit supervisord bzw. mit
    dem API-Dienst — und zwar erst, wenn eine ``/v2``-Anfrage kommt. Kein
    Postgres, kein Suchindex, kein MusicBrainz. Auch das Gate der Datenbank
    macht daraus keine Verbindung: es fragt ``pg_isready``, einen fremden
    Prozess, und haelt weder Pool noch Treiber (M1b).

    Seit Phase 16 kommen die Lebenszyklus-Teile dazu: Aktivitaetsuhr,
    Job-Auskunft, Idle-Stopp und Zustandsabgleich. Die Uhr rechnet, die
    Job-Auskunft liest die eigene SQLite, die beiden Dauerlaeufer benutzen
    Prozess-Steuerung und Bereitschaftsfrage — auch hier keine Verbindung
    zum Array.

    Seit Phase 17 kommt der Lookup-Cache dazu — eine zweite SQLite-Datei
    auf demselben Cache-Pool. Er ist der Grund, warum eine Anfrage das
    Array *gar nicht mehr* braucht; eine Verbindung dorthin ist er nicht.

    Seit Phase 18 stehen Key-Pruefung und Rate-Limit am Eingang. Beide
    gehoeren ausdruecklich hierher: sie rechnen im Speicher bzw. lesen die
    eigene SQLite und muessen deshalb auch bei schlafendem Stack
    funktionieren — sonst waere ausgerechnet ein Cache-Treffer ungeschuetzt.

    Seit M2.5 kommen die Benachrichtigungen dazu. Sie sprechen nach
    **draussen** (ntfy, SMTP) und nicht mit dem Array — und sie sind der
    Grund, warum der Betreiber von einem schlafenden Stack ueberhaupt
    erfaehrt.

    Ebenfalls seit M2.5: Job-Manager und Zeitplan. Sie **wecken** den
    Stack, wenn ein Termin faellig ist — sie halten aber selbst keine
    Verbindung: die Datenbank sieht erst der Subprozess (E10).
    """
    attributes = set(vars(service))
    assert attributes == {
        "settings",
        "db",
        "cache",
        "config_store",
        "state",
        "auth",
        "ratelimit",
        "supervisor",
        "probe",
        "proxy",
        "stack",
        "notify",
        "wake",
        "activity",
        "jobs",
        "idle",
        "poller",
        "job_manager",
        "scheduler",
        "logrotate",
    }
    assert not {"pool", "index", "mb", "matcher"} & attributes


# --- Rekonziliation nach einem harten Ende (K1, F7) -------------------------


def test_an_open_run_from_a_former_life_is_closed_on_start(
    env_settings: EnvSettings, service: WatchdogService
) -> None:
    """Stirbt der Waechter hart, bleibt die Zeile sonst fuer immer offen.

    „Laeuft noch" ist die Job-Sperre des Idle-Stopps (§8.5) — die Instanz
    laege danach dauerhaft wach und zeigte in `/status` einen Import an,
    den es nicht gibt.
    """
    # Ein Lauf aus der Vergangenheit, wie ihn ein abgestuerzter Prozess
    # hinterlaesst.
    start_run(service.db, RunKind.ACOUSTID_DELTA, started_at="2020-01-01T00:00:00.000Z")
    service.db.close()

    with WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        stack=service.stack,
        probe=service.probe,
    ) as restarted:
        assert running_runs(restarted.db) == []
        run = latest_run(restarted.db, RunKind.ACOUSTID_DELTA)
        assert run is not None
        assert run.result is RunResult.ABORTED
        assert "Waechter wurde beendet" in (run.error or "")
        assert restarted.jobs.running_jobs() == []


def test_a_fresh_instance_reconciles_nothing(service: WatchdogService) -> None:
    """Der Normalfall meldet sich nicht — sonst waere die Warnung wertlos."""
    assert running_runs(service.db) == []


def test_an_expired_index_marker_is_removed_on_start(
    env_settings: EnvSettings, service: WatchdogService
) -> None:
    """F7: eine verwaiste Marke haelt eigene Einreichungen sonst dauerhaft zurueck."""
    marker = env_settings.data_dir / INDEX_BUSY_FILENAME
    marker.write_text("2020-01-01T00:00:00.000Z", encoding="utf-8")
    start_run(service.db, RunKind.ACOUSTID_DELTA, started_at="2020-01-01T00:00:00.000Z")
    service.db.close()

    with WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        stack=service.stack,
        probe=service.probe,
    ):
        assert marker.exists() is False


def test_a_fresh_index_marker_survives_the_restart(
    env_settings: EnvSettings, service: WatchdogService
) -> None:
    """Sie kann zu einem Importer gehoeren, der den Waechter ueberlebt hat.

    Blind zu loeschen oeffnete genau das Kollisionsfenster, das die Marke
    schliessen soll — der Waise schreibt weiter in den Index (§8.12).
    """
    marker = env_settings.data_dir / INDEX_BUSY_FILENAME
    marker.write_text(utc_now(), encoding="utf-8")
    start_run(service.db, RunKind.ACOUSTID_DELTA, started_at="2020-01-01T00:00:00.000Z")
    service.db.close()

    with WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        stack=service.stack,
        probe=service.probe,
    ):
        assert marker.exists() is True
