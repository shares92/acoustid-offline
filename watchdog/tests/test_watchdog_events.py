"""Ereignis-Log: Schreiben, Filtern und die Ringpuffer-Grenze (Phase 14)."""

from __future__ import annotations

import logging

import pytest

from acoustid_watchdog.events import (
    EVENT_LOG_LIMIT,
    EventLevel,
    count_events,
    log_event,
    recent_events,
)
from acoustid_watchdog.store import Database


def test_event_round_trip(db: Database) -> None:
    written = log_event(
        db, EventLevel.WARNING, "scheduler", "Import fehlgeschlagen", {"exit_code": 8}
    )
    assert written.id > 0

    (event,) = recent_events(db)
    assert event.id == written.id
    assert event.level is EventLevel.WARNING
    assert event.source == "scheduler"
    assert event.message == "Import fehlgeschlagen"
    assert event.extra == {"exit_code": 8}
    assert event.ts == written.ts


def test_event_without_extra_reads_back_as_empty_dict(db: Database) -> None:
    log_event(db, EventLevel.INFO, "watchdog", "Waechter gestartet")
    (event,) = recent_events(db)
    assert event.extra == {}


def test_extra_falls_back_to_str_for_unserialisable_values(db: Database) -> None:
    """Dieselbe Regel wie im JSON-Log — SecretStr bleibt dadurch maskiert."""
    log_event(db, EventLevel.INFO, "watchdog", "mit Objekt", {"pfad": object()})
    (event,) = recent_events(db)
    assert event.extra["pfad"].startswith("<object object")


def test_recent_events_are_newest_first(db: Database) -> None:
    for number in range(5):
        log_event(db, EventLevel.INFO, "watchdog", f"Ereignis {number}")
    messages = [event.message for event in recent_events(db)]
    assert messages == ["Ereignis 4", "Ereignis 3", "Ereignis 2", "Ereignis 1", "Ereignis 0"]


def test_recent_events_honours_limit(db: Database) -> None:
    for number in range(10):
        log_event(db, EventLevel.INFO, "watchdog", f"Ereignis {number}")
    assert len(recent_events(db, limit=3)) == 3


def test_recent_events_filter_by_level_and_source(db: Database) -> None:
    log_event(db, EventLevel.INFO, "watchdog", "start")
    log_event(db, EventLevel.ERROR, "watchdog", "kaputt")
    log_event(db, EventLevel.ERROR, "scheduler", "auch kaputt")

    by_level = recent_events(db, level=EventLevel.ERROR)
    assert {event.message for event in by_level} == {"kaputt", "auch kaputt"}

    by_source = recent_events(db, source="scheduler")
    assert [event.message for event in by_source] == ["auch kaputt"]

    both = recent_events(db, level=EventLevel.ERROR, source="watchdog")
    assert [event.message for event in both] == ["kaputt"]


# --- Ringpuffer -------------------------------------------------------------


def test_ring_buffer_keeps_only_the_newest_entries(db: Database) -> None:
    limit = 5
    for number in range(limit + 7):
        log_event(db, EventLevel.INFO, "watchdog", f"Ereignis {number}", limit=limit)

    assert count_events(db) == limit
    messages = [event.message for event in recent_events(db, limit=100)]
    assert messages == [f"Ereignis {number}" for number in range(11, 6, -1)]


def test_ring_buffer_does_not_delete_below_the_limit(db: Database) -> None:
    for number in range(3):
        log_event(db, EventLevel.INFO, "watchdog", f"Ereignis {number}", limit=5)
    assert count_events(db) == 3


def test_ring_buffer_survives_gaps_in_the_id_sequence(db: Database) -> None:
    """Die Grenze zaehlt Zeilen, nicht Nummern.

    ``AUTOINCREMENT`` vergibt nach einem Loeschen keine Nummern erneut; eine
    Grenze, die ueber ``id <= MAX(id) - limit`` rechnet, wuerde die Luecken
    mitzaehlen und zu frueh loeschen. Die sortierte Auswahl tut das nicht.
    """
    limit = 4
    for number in range(10):
        log_event(db, EventLevel.INFO, "watchdog", f"Ereignis {number}", limit=0)

    # Luecke schlagen: die Nummern 5 bis 9 verschwinden, 10 bleibt stehen.
    with db.transaction() as tx:
        tx.execute("DELETE FROM event_log WHERE id BETWEEN 5 AND 9")
    assert count_events(db) == 5

    log_event(db, EventLevel.INFO, "watchdog", "neu", limit=limit)

    assert count_events(db) == limit
    assert [event.message for event in recent_events(db, limit=100)] == [
        "neu",
        "Ereignis 9",
        "Ereignis 3",
        "Ereignis 2",
    ]


def test_ring_buffer_can_be_switched_off(db: Database) -> None:
    for number in range(7):
        log_event(db, EventLevel.INFO, "watchdog", f"Ereignis {number}", limit=0)
    assert count_events(db) == 7


def test_default_limit_is_the_documented_one() -> None:
    assert EVENT_LOG_LIMIT == 5_000


# --- Doppelschrift ins Containerlog -----------------------------------------


def test_event_also_reaches_the_container_log(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Jedes Ereignis steht auch in `docker logs` — mit gebuendeltem `extra`.

    Die Anwendungsfelder tragen ein Praefix: die LogRecord-Standardnamen
    sind fuer ``extra`` gesperrt und wuerden zur Laufzeit einen KeyError
    ausloesen (LEARNINGS „reservierte LogRecord-Feldnamen in extra").
    """
    with caplog.at_level(logging.DEBUG):
        log_event(db, EventLevel.ERROR, "scheduler", "Import fehlgeschlagen", {"exit_code": 8})

    (record,) = [item for item in caplog.records if item.message == "Import fehlgeschlagen"]
    assert record.levelname == "ERROR"
    assert record.event_source == "scheduler"
    assert record.event_extra == {"exit_code": 8}


@pytest.mark.parametrize("level", list(EventLevel))
def test_every_level_maps_to_a_logging_level_and_a_german_label(level: EventLevel) -> None:
    assert level.logging_level == logging.getLevelNamesMapping()[level.value]
    assert level.display_name
