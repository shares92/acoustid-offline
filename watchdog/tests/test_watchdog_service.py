"""Start des Waechter-Dienstes: Erststart, Neustart, Zustand (Phase 14)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from acoustid_watchdog.admin import load_admin_user
from acoustid_watchdog.events import EventLevel, recent_events
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.store import DB_FILENAME, SCHEMA_VERSION
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
    assert service.state.set(StackState.SLEEPING) == before

    started = service.state.set(StackState.STARTING)
    assert started.state is StackState.STARTING
    assert started.since >= before.since


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

    Der Waechter kommt ohne ``AOFF_DB_PASSWORD`` aus; wuerde hier je eine
    Datenbankressource entstehen, waere die Zusage „die Admin-UI arbeitet
    bei schlafendem Stack" gebrochen.
    """
    attributes = set(vars(service))
    assert attributes == {"settings", "db", "config_store", "state"}
