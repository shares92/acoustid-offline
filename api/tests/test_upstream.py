"""Upstream-Weiterleitung (Phase 12) — Wire-Format, Drossel, Warteschlange.

**Kein echtes Netz.** An der Stelle von api.acoustid.org steht der
Mock-Transport aus `upstream_mock.py`: er sieht die fertige Anfrage so, wie
sie auf die Leitung ginge, und antwortet mit dem, was der Testfall braucht —
Erfolg, Fehlercode, 503, abgerissene Verbindung. Damit ist genau das
pruefbar, worauf es hier ankommt: **was** hinausgeht (Feld fuer Feld) und
**was** danach in der Datenbank steht.

Gewartet wird nie wirklich: Drossel und Backoff bekommen eine
:class:`upstream_mock.Clock`, und die Testfaelle pruefen die Wartezeiten als
Zahlenreihe (wie schon beim Downloader in Phase 6).

An der Stelle von Postgres steht :class:`stubs.FakeDb`. Dass die **echten**
Anweisungen dasselbe tun, zeigt `test_submit_integration.py`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from stubs import FakeDb, StubConnection, StubService
from upstream_mock import (
    APP_KEY,
    USER_KEY,
    Clock,
    MockUpstream,
    error_response,
    make_forwarder,
    ok_response,
)

from acoustid_api.store import ForwardCandidate
from acoustid_api.upstream import (
    BACKOFF_MAX_S,
    DRAIN_ATTEMPTS,
    MAX_ERROR_CHARS,
    MAX_FORWARD_ATTEMPTS,
    MAX_FORWARD_PER_REQUEST,
    MAX_REQUESTS_PER_SECOND,
    MIN_REQUEST_INTERVAL_S,
    UPSTREAM_FORWARDED_EVENT,
    UPSTREAM_GAVE_UP_EVENT,
    UPSTREAM_URL,
    Throttle,
    UpstreamForwarder,
    drain_queue,
    forward_after_submit,
    forward_groups,
    retry_forward,
)
from shared.config import Config
from shared.fingerprint import decode_fingerprint, encode_fingerprint
from shared.models import SubmitMode

MBID = "b81f83ee-4da4-11e0-9ed8-0025225356f3"
OTHER_MBID = "c0a1c0de-4da4-11e0-9ed8-0025225356f3"

#: Vollvektor, wie er in `local_submission.fingerprint` steht — signed
#: int32, also mit negativen Werten (dasselbe Bitmuster wie u32).
VECTOR = [0x22222220 + index * 16 - 2**32 for index in range(120)]


# --- Werkzeuge --------------------------------------------------------------


def candidate(local_track_id: int = 1, **fields: Any) -> ForwardCandidate:
    defaults: dict[str, Any] = {
        "hashes": VECTOR,
        "duration": 241,
        "mbids": (MBID,),
        "submitted_by": USER_KEY,
    }
    return ForwardCandidate(local_track_id=local_track_id, **{**defaults, **fields})


def upstream_service(
    forwarder: UpstreamForwarder | None, db: FakeDb, mode: SubmitMode = SubmitMode.LOCAL_UPSTREAM
) -> StubService:
    config = Config.model_validate({"submit": {"mode": mode, "upstream_app_key": APP_KEY}})
    return StubService(connection=StubConnection(handler=db), config=config, upstream=forwarder)


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def clock() -> Clock:
    return Clock()


# --- Drossel ----------------------------------------------------------------


def test_the_first_request_never_waits(clock: Clock) -> None:
    assert Throttle(sleep=clock.sleep, monotonic=clock.monotonic).acquire() == 0.0
    assert clock.sleeps == []


def test_the_throttle_holds_three_requests_per_second(clock: Clock) -> None:
    """Nutzungsbedingung von acoustid.org, hart eingehalten."""
    throttle = Throttle(sleep=clock.sleep, monotonic=clock.monotonic)
    delays = [throttle.acquire() for _ in range(5)]
    assert delays == pytest.approx([0.0] + [MIN_REQUEST_INTERVAL_S] * 4)
    # Vier Anfragen in einer Sekunde waeren zu viel, drei sind erlaubt.
    assert pytest.approx(1 / MAX_REQUESTS_PER_SECOND) == MIN_REQUEST_INTERVAL_S
    assert clock.now == pytest.approx(4 * MIN_REQUEST_INTERVAL_S)


def test_a_slow_caller_is_never_slowed_down(clock: Clock) -> None:
    throttle = Throttle(sleep=clock.sleep, monotonic=clock.monotonic)
    throttle.acquire()
    clock.advance(10.0)
    assert throttle.acquire() == 0.0
    assert clock.sleeps == []


def test_threads_share_one_time_slot_each() -> None:
    """Die Grenze gilt fuer den Prozess, nicht je Thread.

    Die Uhr steht still; jeder Aufrufer bekommt trotzdem sein eigenes
    Zeitfenster — die Wartezeiten sind in Summe genau die Treppe, egal in
    welcher Reihenfolge die Threads drankamen.
    """
    import threading

    throttle = Throttle(sleep=lambda _: None, monotonic=lambda: 0.0)
    delays: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        delay = throttle.acquire()
        with lock:
            delays.append(delay)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(delays) == pytest.approx([step * MIN_REQUEST_INTERVAL_S for step in range(10)])


# --- Aufbau und https-Zwang -------------------------------------------------


def test_the_default_target_is_the_original_endpoint() -> None:
    assert UPSTREAM_URL == "https://api.acoustid.org/v2/submit"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.acoustid.org/v2/submit",
        "http://127.0.0.1:8080/v2/submit",
        "ftp://api.acoustid.org/v2/submit",
        "//api.acoustid.org/v2/submit",
    ],
)
def test_a_target_without_https_is_refused(url: str) -> None:
    """Ueber die Leitung gehen fremde user-Keys (ARCHITECTURE §7)."""
    with pytest.raises(ValueError, match="https"):
        UpstreamForwarder(APP_KEY, url=url)


def test_https_is_recognised_case_insensitively() -> None:
    with UpstreamForwarder(APP_KEY, url="HTTPS://api.acoustid.org/v2/submit") as forwarder:
        assert forwarder.url.lower().startswith("https://")


def test_without_an_app_key_there_is_no_forwarder() -> None:
    with pytest.raises(ValueError, match="upstream_app_key"):
        UpstreamForwarder("")


def test_from_config_stays_silent_outside_the_upstream_mode() -> None:
    for mode in (SubmitMode.OFF, SubmitMode.LOCAL):
        config = Config.model_validate({"submit": {"mode": mode}})
        assert UpstreamForwarder.from_config(config) is None


def test_from_config_builds_the_forwarder_in_upstream_mode() -> None:
    config = Config.model_validate(
        {"submit": {"mode": SubmitMode.LOCAL_UPSTREAM, "upstream_app_key": APP_KEY}}
    )
    forwarder = UpstreamForwarder.from_config(config)
    assert forwarder is not None
    with forwarder:
        assert forwarder.url == UPSTREAM_URL


def test_the_repr_does_not_leak_the_app_key() -> None:
    with make_forwarder(MockUpstream()) as forwarder:
        assert APP_KEY not in repr(forwarder)


def test_a_borrowed_client_is_not_closed() -> None:
    borrowed = httpx.Client(transport=httpx.MockTransport(MockUpstream()))
    with UpstreamForwarder(APP_KEY, client=borrowed):
        pass
    assert not borrowed.is_closed
    borrowed.close()


# --- Wire-Format ------------------------------------------------------------


def test_the_request_is_the_original_submit_format() -> None:
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        assert forwarder.submit(candidate()).ok

    request = upstream.requests[0]
    assert request.method == "POST"
    assert str(request.url) == UPSTREAM_URL
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "acoustid-offline" in request.headers["user-agent"]
    assert upstream.field("format") == "json"


def test_our_own_application_key_is_the_client() -> None:
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(candidate())
    assert upstream.field("client") == APP_KEY


def test_the_user_key_of_the_client_is_passed_through_unchanged() -> None:
    """Zweckbindung: acoustid.org kennt kein „im Namen Dritter"."""
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(candidate(submitted_by="uNveRaenDert-42"))
    assert upstream.field("user") == "uNveRaenDert-42"


def test_the_fingerprint_is_reencoded_from_the_stored_vector() -> None:
    """Die Zeichenkette des Clients steht nirgends — der Codec ist verlustfrei."""
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(candidate())
    encoded = upstream.field("fingerprint.0")
    assert encoded is not None
    unsigned = [value + 2**32 if value < 0 else value for value in VECTOR]
    assert list(decode_fingerprint(encoded).hashes) == unsigned
    assert encoded == encode_fingerprint(VECTOR)


def test_every_mbid_of_a_group_travels_in_one_request() -> None:
    """Drei Zeilen, eine Aufnahme, eine Anfrage mit dreifachem `mbid.0`."""
    upstream = MockUpstream()
    third = "0d1e2f30-4da4-11e0-9ed8-0025225356f3"
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(candidate(mbids=(MBID, OTHER_MBID, third)))
    assert upstream.count == 1
    assert upstream.values("mbid.0") == [MBID, OTHER_MBID, third]
    # Alles andere genau einmal — sonst waeren es mehrere Einreichungen.
    assert upstream.values("fingerprint.0") == [encode_fingerprint(VECTOR)]
    assert upstream.values("duration.0") == ["241"]


def test_a_group_without_mbid_still_goes_out() -> None:
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(candidate(mbids=(), track="Der Titel", artist="Die Band"))
    assert upstream.values("mbid.0") == []
    assert upstream.field("track.0") == "Der Titel"


def test_all_optional_fields_reach_the_original() -> None:
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(
            candidate(
                bitrate=320,
                fileformat="FLAC",
                puid=OTHER_MBID,
                foreignid="spotify:4711",
                track="Der Titel",
                artist="Die Band",
                album="Das Album",
                album_artist="Diverse",
                track_no=4,
                disc_no=1,
                year=1999,
            )
        )
    fields = dict(upstream.fields())
    assert fields["bitrate.0"] == "320"
    assert fields["fileformat.0"] == "FLAC"
    assert fields["puid.0"] == OTHER_MBID
    assert fields["foreignid.0"] == "spotify:4711"
    assert fields["album.0"] == "Das Album"
    # Original-Schreibweisen, nicht unsere Spaltennamen.
    assert fields["albumartist.0"] == "Diverse"
    assert fields["trackno.0"] == "4"
    assert fields["discno.0"] == "1"
    assert fields["year.0"] == "1999"


def test_fields_that_are_not_set_are_left_out() -> None:
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(candidate())
    names = {name for name, _ in upstream.fields()}
    assert names == {
        "format",
        "client",
        "clientversion",
        "user",
        "duration.0",
        "fingerprint.0",
        "mbid.0",
    }


def test_a_zero_year_is_not_mistaken_for_absent() -> None:
    """`if value is not None`, nicht `if value` — 0 ist ein Wert."""
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        forwarder.submit(candidate(year=0, track_no=0))
    fields = dict(upstream.fields())
    assert fields["year.0"] == "0"
    assert fields["trackno.0"] == "0"


# --- Antworten und Fehlerklassen --------------------------------------------


def test_a_successful_submission_reports_the_upstream_ids() -> None:
    with make_forwarder(MockUpstream([ok_response(ids=(11, 12))])) as forwarder:
        result = forwarder.submit(candidate())
    assert result.ok
    assert result.submission_ids == (11, 12)
    assert result.error is None


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_server_side_trouble_counts_as_transient(status: int) -> None:
    with make_forwarder(MockUpstream([httpx.Response(status, text="spaeter")])) as forwarder:
        result = forwarder.submit(candidate())
    assert not result.ok
    assert result.transient
    assert str(status) in (result.error or "")


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_refused_submission_is_permanent(status: int) -> None:
    handler = MockUpstream([error_response(4, "invalid API key", status=status)])
    with make_forwarder(handler) as forwarder:
        result = forwarder.submit(candidate())
    assert not result.ok
    assert not result.transient
    assert "invalid API key" in (result.error or "")


def test_an_error_payload_with_http_200_is_permanent_too() -> None:
    handler = MockUpstream(
        [httpx.Response(200, json={"status": "error", "error": {"code": 12, "message": "no"}})]
    )
    with make_forwarder(handler) as forwarder:
        result = forwarder.submit(candidate())
    assert not result.ok
    assert not result.transient
    assert "Fehler 12" in (result.error or "")


def test_a_broken_connection_is_transient() -> None:
    handler = MockUpstream([httpx.ConnectError("kein Netz")])
    with make_forwarder(handler) as forwarder:
        result = forwarder.submit(candidate())
    assert not result.ok
    assert result.transient


def test_an_unreadable_answer_is_transient() -> None:
    handler = MockUpstream([httpx.Response(200, text="<html>Wartungsarbeiten")])
    with make_forwarder(handler) as forwarder:
        result = forwarder.submit(candidate())
    assert not result.ok
    assert result.transient


def test_a_missing_user_key_fails_without_asking_upstream() -> None:
    """Raten waere Zweckentfremdung eines fremden Keys."""
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        result = forwarder.submit(candidate(submitted_by=None))
    assert not result.ok
    assert not result.transient
    assert upstream.count == 0
    assert "user-Key" in (result.error or "")


# --- Backoff ----------------------------------------------------------------


def test_the_backoff_doubles_from_one_second(clock: Clock) -> None:
    handler = MockUpstream([httpx.Response(503)] * 5)
    with make_forwarder(handler, clock) as forwarder:
        result = forwarder.submit(candidate(), max_attempts=5)
    assert not result.ok
    assert result.attempts == 5
    # Vier Pausen zwischen fuenf Versuchen — und ausserdem die Drossel.
    assert [value for value in clock.sleeps if value >= 1.0] == [1.0, 2.0, 4.0, 8.0]


def test_the_backoff_is_capped_at_thirty_seconds(clock: Clock) -> None:
    handler = MockUpstream([httpx.Response(503)] * 8)
    with make_forwarder(handler, clock) as forwarder:
        forwarder.submit(candidate(), max_attempts=8)
    backoffs = [value for value in clock.sleeps if value >= 1.0]
    assert backoffs == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert max(clock.sleeps) == BACKOFF_MAX_S


def test_a_permanent_error_is_not_retried(clock: Clock) -> None:
    handler = MockUpstream([error_response(4, "invalid API key")])
    with make_forwarder(handler, clock) as forwarder:
        result = forwarder.submit(candidate(), max_attempts=5)
    assert handler.count == 1
    assert result.attempts == 1
    assert clock.sleeps == []


def test_a_later_attempt_can_still_succeed(clock: Clock) -> None:
    handler = MockUpstream([httpx.Response(503), ok_response()])
    with make_forwarder(handler, clock) as forwarder:
        result = forwarder.submit(candidate(), max_attempts=5)
    assert result.ok
    assert result.attempts == 2
    assert clock.sleeps == [1.0]


def test_every_attempt_passes_the_throttle(clock: Clock) -> None:
    handler = MockUpstream([httpx.Response(503), httpx.Response(503), ok_response()])
    with make_forwarder(handler, clock) as forwarder:
        forwarder.submit(candidate(), max_attempts=3)
    # Die Drossel wartet nur, wenn das Backoff nicht ohnehin laenger war.
    assert handler.count == 3
    assert clock.now == pytest.approx(3.0)


# --- Der Application-Key bleibt geheim --------------------------------------


def test_the_app_key_never_reaches_the_error_text() -> None:
    """Eine Fehlerseite, die den Rumpf zitiert, wuerde ihn sonst tragen."""
    handler = MockUpstream([httpx.Response(400, text=f"bad request: client={APP_KEY}&user=x")])
    with make_forwarder(handler) as forwarder:
        result = forwarder.submit(candidate())
    assert APP_KEY not in (result.error or "")
    assert "***" in (result.error or "")


def test_a_long_error_is_truncated() -> None:
    handler = MockUpstream([httpx.Response(400, text="x" * 5000)])
    with make_forwarder(handler) as forwarder:
        result = forwarder.submit(candidate())
    assert result.error is not None
    assert len(result.error) <= MAX_ERROR_CHARS


# --- Statuspfade ------------------------------------------------------------


def test_a_forwarded_group_reaches_the_status_forwarded(db: FakeDb) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    with make_forwarder(MockUpstream()) as forwarder:
        report = forward_groups(connection, forwarder, [candidate()], max_attempts=1)
    assert report.forwarded == 1
    assert db.status_by_track == {1: {"forwarded"}}
    assert db.rows[0]["forwarded_at"] is not None


def test_a_failed_group_keeps_attempts_and_reason(db: FakeDb) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    handler = MockUpstream([error_response(4, "invalid API key")])
    with make_forwarder(handler) as forwarder:
        report = forward_groups(connection, forwarder, [candidate()], max_attempts=1)
    assert (report.forwarded, report.failed) == (0, 1)
    assert db.status_by_track == {1: {"forward_failed"}}
    assert db.attempts_by_track == {1: 1}
    assert "invalid API key" in (db.errors_by_track[1] or "")


def test_a_later_success_clears_the_error_but_keeps_the_history(db: FakeDb) -> None:
    db.add(local_track_id=1, mbid=MBID, status="forward_failed", forward_attempts=3)
    db.rows[0]["forward_error"] = "vorher kaputt"
    connection = StubConnection(handler=db)
    with make_forwarder(MockUpstream()) as forwarder:
        forward_groups(connection, forwarder, [candidate()], max_attempts=1)
    assert db.status_by_track == {1: {"forwarded"}}
    assert db.errors_by_track == {1: None}
    # Der Zaehler ist Historie und wird nicht geschoent.
    assert db.attempts_by_track == {1: 3}


def test_all_rows_of_a_group_switch_together(db: FakeDb) -> None:
    db.add(local_track_id=1, mbid=MBID)
    db.add(local_track_id=1, mbid=OTHER_MBID)
    connection = StubConnection(handler=db)
    with make_forwarder(MockUpstream()) as forwarder:
        forward_groups(connection, forwarder, [candidate()], max_attempts=1)
    assert [row["status"] for row in db.rows] == ["forwarded", "forwarded"]


def test_a_submission_that_is_not_indexed_yet_is_not_in_the_queue(db: FakeDb) -> None:
    """`new` heisst: der Suchindex kennt sie nicht — das darf nicht verloren gehen."""
    db.add(local_track_id=1, mbid=MBID, status="new")
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db))
    assert report.empty
    assert upstream.count == 0
    assert db.status_by_track == {1: {"new"}}


def test_a_forwarded_group_is_never_forwarded_twice(db: FakeDb) -> None:
    db.add(local_track_id=1, mbid=MBID, status="forwarded")
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db))
    assert report.empty
    assert upstream.count == 0


# --- Warteschlangenlauf -----------------------------------------------------


def test_the_drain_takes_the_oldest_first(db: FakeDb) -> None:
    for number in (1, 2, 3):
        db.add(local_track_id=number, mbid=MBID)
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db))
    assert report.forwarded == 3
    assert [upstream.field("duration.0", position) for position in range(3)] == ["241"] * 3
    assert db.status_by_track == {1: {"forwarded"}, 2: {"forwarded"}, 3: {"forwarded"}}


def test_the_drain_retries_what_failed_before(db: FakeDb) -> None:
    """Der Kern der Warteschlange: der naechste Lauf holt es nach (§8.9)."""
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    service = upstream_service(None, db)

    with make_forwarder(MockUpstream([httpx.Response(503)])) as broken:
        service.upstream = broken
        drain_queue(connection, service, max_attempts=1)
    assert db.status_by_track == {1: {"forward_failed"}}

    with make_forwarder(MockUpstream()) as healthy:
        service.upstream = healthy
        report = drain_queue(connection, service, max_attempts=1)
    assert report.forwarded == 1
    assert db.status_by_track == {1: {"forwarded"}}


def test_an_unreachable_upstream_stops_the_run(db: FakeDb) -> None:
    """Weitermachen kostete nur Zeit und Fehlversuche."""
    for number in (1, 2, 3):
        db.add(local_track_id=number, mbid=MBID)
    connection = StubConnection(handler=db)
    upstream = MockUpstream([httpx.Response(503)])
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db), max_attempts=1)
    assert (report.attempted, report.failed, report.skipped) == (1, 1, 2)
    assert upstream.count == 1
    assert db.status_by_track == {1: {"forward_failed"}, 2: {"indexed"}, 3: {"indexed"}}


def test_a_single_refused_submission_does_not_stop_the_run(db: FakeDb) -> None:
    for number in (1, 2, 3):
        db.add(local_track_id=number, mbid=MBID)
    connection = StubConnection(handler=db)
    upstream = MockUpstream([error_response(7, "invalid UUID")])
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db), max_attempts=1)
    assert (report.attempted, report.forwarded, report.failed, report.skipped) == (3, 2, 1, 0)
    assert db.status_by_track == {1: {"forward_failed"}, 2: {"forwarded"}, 3: {"forwarded"}}


def test_the_drain_stops_at_its_limit(db: FakeDb) -> None:
    for number in range(1, 6):
        db.add(local_track_id=number, mbid=MBID)
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db), limit=2)
    assert report.forwarded == 2
    assert upstream.count == 2
    assert sum(1 for row in db.rows if row["status"] == "indexed") == 3


@pytest.mark.parametrize("mode", [SubmitMode.OFF, SubmitMode.LOCAL])
def test_outside_the_upstream_mode_nothing_is_forwarded(db: FakeDb, mode: SubmitMode) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db, mode=mode))
    assert report.empty
    assert upstream.count == 0
    assert db.status_by_track == {1: {"indexed"}}


def test_a_missing_forwarder_is_a_warning_not_a_crash(db: FakeDb, caplog: Any) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    with caplog.at_level(logging.WARNING):
        report = drain_queue(connection, upstream_service(None, db))
    assert report.empty
    assert "local+upstream" in caplog.text


# --- Die 7-Fehler-Grenze (§8.9) --------------------------------------------


def _fail_once(db: FakeDb, connection: StubConnection) -> None:
    """Ein Warteschlangenlauf, den upstream inhaltlich ablehnt (kein Abbruch)."""
    refuse = MockUpstream(default=lambda: error_response(4, "invalid API key"))
    with make_forwarder(refuse) as forwarder:
        drain_queue(connection, upstream_service(forwarder, db), max_attempts=1)


def test_after_seven_failures_there_is_no_automatic_retry(db: FakeDb) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    for _ in range(MAX_FORWARD_ATTEMPTS):
        _fail_once(db, connection)
    assert db.attempts_by_track == {1: MAX_FORWARD_ATTEMPTS}

    # Der achte Lauf fasst die Gruppe nicht mehr an — auch nicht mit einem
    # Upstream, der wieder antworten wuerde.
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = drain_queue(connection, upstream_service(forwarder, db))
    assert report.empty
    assert upstream.count == 0
    assert db.status_by_track == {1: {"forward_failed"}}


def test_the_seventh_failure_logs_the_event_for_the_notification(db: FakeDb, caplog: Any) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    with caplog.at_level(logging.INFO):
        for _ in range(MAX_FORWARD_ATTEMPTS):
            _fail_once(db, connection)

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == UPSTREAM_GAVE_UP_EVENT
    ]
    assert len(events) == 1
    assert events[0].levelno == logging.ERROR
    assert events[0].local_track_id == 1
    assert events[0].forward_attempts == MAX_FORWARD_ATTEMPTS


def test_a_success_logs_the_event_with_the_upstream_ids(db: FakeDb, caplog: Any) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    with (
        caplog.at_level(logging.INFO),
        make_forwarder(MockUpstream([ok_response(ids=(99,))])) as forwarder,
    ):
        forward_groups(connection, forwarder, [candidate()], max_attempts=1)

    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == UPSTREAM_FORWARDED_EVENT
    ]
    assert len(events) == 1
    assert events[0].upstream_submission_ids == [99]
    # Die IDs des Originals landen NICHT in der Datenbank (kein Vermischen).
    assert all("upstream" not in key for key in db.rows[0])


# --- Manueller Wiederholungsversuch ----------------------------------------


def test_the_manual_retry_revives_a_group_that_gave_up(db: FakeDb) -> None:
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    for _ in range(MAX_FORWARD_ATTEMPTS):
        _fail_once(db, connection)

    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = retry_forward(connection, upstream_service(forwarder, db), max_attempts=1)
    assert report.forwarded == 1
    assert upstream.count == 1
    assert db.status_by_track == {1: {"forwarded"}}
    assert db.attempts_by_track == {1: 0}


def test_the_manual_retry_leaves_groups_below_the_limit_alone(db: FakeDb) -> None:
    """Sie kommen beim naechsten Lauf ohnehin dran."""
    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=db)
    _fail_once(db, connection)

    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = retry_forward(connection, upstream_service(forwarder, db), max_attempts=1)
    assert report.empty
    assert upstream.count == 0
    assert db.attempts_by_track == {1: 1}


def test_the_manual_retry_can_name_a_single_group(db: FakeDb) -> None:
    """Mit Namensnennung greift der Hook auch unterhalb der Grenze."""
    for number in (1, 2):
        db.add(local_track_id=number, mbid=MBID)
    connection = StubConnection(handler=db)
    _fail_once(db, connection)
    assert db.attempts_by_track == {1: 1, 2: 1}

    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = retry_forward(
            connection, upstream_service(forwarder, db), local_track_ids=[1], max_attempts=1
        )
    assert report.forwarded == 1
    assert upstream.count == 1
    assert db.status_by_track == {1: {"forwarded"}, 2: {"forward_failed"}}
    assert db.attempts_by_track == {1: 0, 2: 1}


def test_the_manual_retry_does_nothing_when_there_is_nothing_to_revive(db: FakeDb) -> None:
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        assert retry_forward(connection, upstream_service(forwarder, db)).empty
    assert upstream.count == 0


# --- Weiterleiten in der Submit-Anfrage -------------------------------------


def test_the_request_path_only_forwards_its_own_submissions(db: FakeDb) -> None:
    older = db.add(local_track_id=1, mbid=MBID)
    fresh = db.add(local_track_id=2, mbid=OTHER_MBID)
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = forward_after_submit(
            connection, upstream_service(forwarder, db), [fresh["local_track_id"]]
        )
    assert report.forwarded == 1
    assert upstream.count == 1
    assert db.status_by_track[older["local_track_id"]] == {"indexed"}
    assert db.status_by_track[fresh["local_track_id"]] == {"forwarded"}


def test_the_request_path_is_capped(db: FakeDb) -> None:
    """Ein grosses Picard-Paket darf die Antwort nicht minutenlang aufhalten."""
    ids = list(range(1, MAX_FORWARD_PER_REQUEST + 4))
    for number in ids:
        db.add(local_track_id=number, mbid=MBID)
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        report = forward_after_submit(connection, upstream_service(forwarder, db), ids)
    assert report.forwarded == MAX_FORWARD_PER_REQUEST
    assert sum(1 for row in db.rows if row["status"] == "indexed") == 3


def test_the_request_path_never_raises(db: FakeDb, caplog: Any) -> None:
    """Lokal gespeichert ist die Wahrheit — ein Upstream-Problem kippt nichts."""

    def explode(query: str, params: Any) -> Any:
        if "forward_attempts" in query:
            raise RuntimeError("Datenbank weg")
        return db(query, params)

    db.add(local_track_id=1, mbid=MBID)
    connection = StubConnection(handler=explode)
    with caplog.at_level(logging.ERROR), make_forwarder(MockUpstream()) as forwarder:
        report = forward_after_submit(connection, upstream_service(forwarder, db), [1])
    assert report.empty
    assert "Datenbank weg" in caplog.text


def test_nothing_stored_means_nothing_forwarded(db: FakeDb) -> None:
    connection = StubConnection(handler=db)
    upstream = MockUpstream()
    with make_forwarder(upstream) as forwarder:
        assert forward_after_submit(connection, upstream_service(forwarder, db), []).empty
    assert upstream.count == 0


def test_the_drain_uses_more_attempts_than_the_request_path() -> None:
    """Der Update-Lauf darf warten, eine offene Submit-Anfrage nicht."""
    assert DRAIN_ATTEMPTS > 1
