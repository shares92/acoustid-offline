"""Circuit-Breaker vor der MusicBrainz-Verbindung (Phase 10).

Getestet wird mit einer gestellten Uhr — echte Wartezeiten haetten hier
nichts zu suchen, und die Uebergaenge haengen ausschliesslich an ihr.
"""

from __future__ import annotations

from shared.mb.breaker import CircuitBreaker


class Clock:
    """Monotone Uhr, die nur auf Zuruf weiterlaeuft."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(clock: Clock) -> CircuitBreaker:
    return CircuitBreaker(threshold=3, window_s=30.0, cooldown_s=30.0, clock=clock)


def test_a_fresh_breaker_lets_everything_through() -> None:
    breaker = make_breaker(Clock())
    assert breaker.allows() is True
    assert breaker.is_open is False


def test_it_opens_after_the_threshold() -> None:
    clock = Clock()
    breaker = make_breaker(clock)
    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    assert breaker.record_failure() is True  # nur der oeffnende Fehler meldet sich
    assert breaker.is_open is True
    assert breaker.allows() is False
    assert breaker.trips == 1


def test_failures_outside_the_window_do_not_count() -> None:
    clock = Clock()
    breaker = make_breaker(clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(31.0)
    # Die beiden alten Fehler sind aus dem Fenster gefallen.
    assert breaker.record_failure() is False
    assert breaker.allows() is True


def test_a_success_clears_the_window() -> None:
    breaker = make_breaker(Clock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.record_failure() is False
    assert breaker.allows() is True


def test_after_the_cooldown_one_attempt_is_allowed() -> None:
    clock = Clock()
    breaker = make_breaker(clock)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.allows() is False

    clock.advance(30.0)
    assert breaker.allows() is True  # halb offen
    assert breaker.is_open is False


def test_a_failed_attempt_reopens_without_a_second_trip() -> None:
    clock = Clock()
    breaker = make_breaker(clock)
    for _ in range(3):
        breaker.record_failure()
    clock.advance(30.0)

    assert breaker.record_failure() is False  # kein zweites lautes Log
    assert breaker.trips == 1
    assert breaker.allows() is False
    clock.advance(29.9)
    assert breaker.allows() is False


def test_a_successful_attempt_closes_the_breaker() -> None:
    clock = Clock()
    breaker = make_breaker(clock)
    for _ in range(3):
        breaker.record_failure()
    clock.advance(30.0)
    breaker.record_success()

    assert breaker.allows() is True
    assert breaker.is_open is False
    # Der Zaehler faengt wieder bei null an.
    assert breaker.record_failure() is False


def test_reset_returns_to_the_initial_state() -> None:
    breaker = make_breaker(Clock())
    for _ in range(3):
        breaker.record_failure()
    breaker.reset()
    assert breaker.allows() is True
    assert breaker.is_open is False
