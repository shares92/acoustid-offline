"""Benachrichtigungen: Kanaele, Ereignisse, Testnachricht (M2.5).

Beide Kanaele werden gegen Attrappen geprueft — der ntfy-Kanal ueber einen
``httpx.MockTransport`` (der **echte** Client laeuft also mit, samt
Kopfzeilen-Kodierung), der Mail-Kanal ueber eine eingesetzte Fabrik. Ein
echter SMTP-Server im Unit-Test waere weder deterministisch noch noetig:
was ``smtplib`` aus einer ``EmailMessage`` macht, ist nicht unser Vertrag —
unserer ist, **dass** und **womit** wir zustellen.
"""

from __future__ import annotations

import httpx
import pytest

from acoustid_watchdog.events import EventLevel
from acoustid_watchdog.notify import (
    ChannelResult,
    Notification,
    Notifier,
    NotifyEvent,
    NtfyChannel,
    SmtpChannel,
    disk_low,
    import_failed,
    stack_start_failed,
    upstream_gave_up,
    version_drift,
)
from shared.config import Config, NotifyConfig, NtfyConfig, SmtpConfig

NTFY_URL = "https://ntfy.example.org/musicmeta"


# --- Attrappen --------------------------------------------------------------


class RecordingNtfy:
    """Nimmt ntfy-POSTs entgegen und merkt sich Kopfzeilen und Rumpf."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return httpx.Response(self.status_code)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


class RecordingSmtp:
    """Mail-Kanal ohne Server; scheitert auf Wunsch."""

    name = "smtp"

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.messages: list[Notification] = []

    def send(self, notification: Notification) -> str:
        if self.fail is not None:
            raise OSError(self.fail)
        self.messages.append(notification)
        return "betreiber@example.org"


def _config(*, ntfy: bool = False, smtp: bool = False) -> Config:
    """Konfiguration mit den gewuenschten Kanaelen — sonst alles Default."""
    return Config(
        notify=NotifyConfig(
            ntfy=NtfyConfig(url=NTFY_URL if ntfy else ""),
            smtp=SmtpConfig(
                host="mail.example.org" if smtp else "",
                **{"from": "instanz@example.org", "to": "betreiber@example.org"},
            ),
        )
    )


def _notifier(
    config: Config,
    *,
    ntfy: RecordingNtfy | None = None,
    smtp: RecordingSmtp | None = None,
    events: list[tuple[EventLevel, str, dict]] | None = None,
) -> Notifier:
    transport = httpx.MockTransport(ntfy or RecordingNtfy())

    def log_event(level: EventLevel, message: str, extra: dict | None = None) -> None:
        if events is not None:
            events.append((level, message, extra or {}))

    return Notifier(
        lambda: config,
        log_event=log_event if events is not None else None,
        client=httpx.Client(transport=transport),
        smtp_channel=lambda _: smtp or RecordingSmtp(),
    )


# --- Leere Konfiguration = aus ----------------------------------------------


def test_without_a_channel_nothing_is_sent() -> None:
    """Der Auslieferungszustand: beide Kanaele leer, also passiert nichts."""
    notifier = _notifier(Config())
    outcome = notifier.send(stack_start_failed("db startet nicht"))

    assert notifier.channels() == []
    assert outcome.attempted is False
    assert outcome.sent == ()
    assert outcome.ok is False


def test_an_empty_channel_writes_no_event(caplog: pytest.LogCaptureFixture) -> None:
    """Sonst fuellte eine Instanz ohne Benachrichtigungen den Ringpuffer."""
    events: list[tuple[EventLevel, str, dict]] = []
    _notifier(Config(), events=events).send(stack_start_failed("egal"))
    assert events == []


# --- ntfy -------------------------------------------------------------------


def test_ntfy_sends_title_priority_and_body() -> None:
    ntfy = RecordingNtfy()
    outcome = _notifier(_config(ntfy=True), ntfy=ntfy).send(
        disk_low("/import", free_gb=12.0, min_free_gb=100)
    )

    assert outcome.sent == ("ntfy",)
    request = ntfy.last
    assert str(request.url) == NTFY_URL
    assert request.headers["Title"] == "musicmeta-offline: Plattenplatz knapp"
    assert request.headers["Priority"] == "high"
    assert request.headers["Tags"] == "floppy_disk"
    body = request.content.decode("utf-8")
    assert "/import" in body
    # Die Zusatzangaben stehen als Zeilen unter der Meldung.
    assert "pfad: /import" in body
    assert "gefordert_gib: 100" in body


def test_ntfy_encodes_non_ascii_headers() -> None:
    """Ein Umlaut im Titel darf den Versand nicht mit einem Encode-Fehler kippen.

    HTTP-Kopfzeilen sind latin-1; ohne RFC-2047-Kodierung braeche httpx
    genau dann ab, wenn es etwas zu melden gibt.
    """
    ntfy = RecordingNtfy()
    notification = Notification(
        event=NotifyEvent.TEST,
        title="Nachzügler übersprungen",
        message="Der Lauf über Nacht ist ausgefallen.",
        level=EventLevel.INFO,
    )
    outcome = _notifier(_config(ntfy=True), ntfy=ntfy).send(notification)

    assert outcome.ok is True
    title = ntfy.last.headers["Title"]
    assert title.startswith("=?utf-8?") and "ü" not in title
    # Der Rumpf bleibt UTF-8 und damit lesbar.
    assert "über Nacht" in ntfy.last.content.decode("utf-8")


def test_ntfy_error_status_is_a_failure_not_an_exception() -> None:
    """Ein 500 der Gegenstelle ist eine Warnung — nicht das Ende des Laufs."""
    notifier = _notifier(_config(ntfy=True), ntfy=RecordingNtfy(status_code=500))
    outcome = notifier.send(stack_start_failed("db startet nicht"))

    assert outcome.failed == ("ntfy",)
    assert outcome.ok is False
    assert notifier.failures == 1
    assert outcome.results[0].error and "HTTPStatusError" in outcome.results[0].error


# --- SMTP -------------------------------------------------------------------


def test_smtp_receives_the_notification() -> None:
    smtp = RecordingSmtp()
    outcome = _notifier(_config(smtp=True), smtp=smtp).send(
        import_failed("AcoustID-Delta", result="import_failed", error="Zeile 128", run_id=7)
    )

    assert outcome.sent == ("smtp",)
    assert outcome.results[0].detail == "betreiber@example.org"
    assert smtp.messages[0].event is NotifyEvent.IMPORT_FAILED
    assert "#7" in smtp.messages[0].message


def test_smtp_recipients_accept_comma_and_semicolon() -> None:
    """Beides ist in Mailprogrammen ueblich — eines davon zu verlangen waere eine Falle."""
    config = SmtpConfig(
        host="mail.example.org",
        **{"from": "a@example.org", "to": "eins@example.org; zwei@example.org,drei@example.org"},
    )
    channel = SmtpChannel(config, timeout_s=1.0)
    sent: dict[str, object] = {}

    class FakeServer:
        def __enter__(self) -> FakeServer:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def login(self, user: str, password: str) -> None:  # pragma: no cover - kein Benutzer
            raise AssertionError("ohne notify.smtp.user wird nicht angemeldet")

        def send_message(self, message: object, *, from_addr: str, to_addrs: list[str]) -> None:
            sent["to"] = to_addrs
            sent["from"] = from_addr

    channel._connect = lambda: FakeServer()  # type: ignore[method-assign,assignment]
    detail = channel.send(Notification(NotifyEvent.TEST, "Titel", "Text", level=EventLevel.INFO))

    assert sent["to"] == ["eins@example.org", "zwei@example.org", "drei@example.org"]
    assert detail == "eins@example.org, zwei@example.org, drei@example.org"


def test_a_failing_channel_does_not_stop_the_other() -> None:
    """Beide Kanaele sind unabhaengig — sonst haette ein toter Mailserver
    auch die Push-Nachricht verhindert."""
    ntfy = RecordingNtfy()
    notifier = _notifier(
        _config(ntfy=True, smtp=True), ntfy=ntfy, smtp=RecordingSmtp(fail="Verbindung abgelehnt")
    )
    outcome = notifier.send(stack_start_failed("index fehlt"))

    assert outcome.sent == ("ntfy",)
    assert outcome.failed == ("smtp",)
    assert len(ntfy.requests) == 1


# --- Die fuenf Ereignisse ---------------------------------------------------


def test_all_five_events_reach_both_channels() -> None:
    """Definition of Done: alle vier Betriebs-Ereignisse plus Versions-Drift."""
    ntfy = RecordingNtfy()
    smtp = RecordingSmtp()
    notifier = _notifier(_config(ntfy=True, smtp=True), ntfy=ntfy, smtp=smtp)

    notifications = [
        import_failed("AcoustID-Delta", result="import_failed", error="x", run_id=1),
        disk_low("/data/db", free_gb=3.0, min_free_gb=100),
        stack_start_failed("db war nach 180 s nicht bereit"),
        upstream_gave_up(local_track_ids=[17, 18], forward_attempts=7, forward_error="HTTP 500"),
        version_drift(expected=18, found=[17], detail="PG 17 gefunden"),
    ]
    for notification in notifications:
        assert notifier.send(notification).ok is True

    assert len(ntfy.requests) == 5
    assert [message.event for message in smtp.messages] == [
        NotifyEvent.IMPORT_FAILED,
        NotifyEvent.DISK_LOW,
        NotifyEvent.STACK_START_FAILED,
        NotifyEvent.UPSTREAM_GAVE_UP,
        NotifyEvent.VERSION_DRIFT,
    ]
    assert notifier.sent == 10


def test_upstream_message_carries_the_phase_12_fields() -> None:
    """`local_track_id`, `forward_attempts`, `forward_error` — wie im API-Log."""
    notification = upstream_gave_up(
        local_track_ids=[17, 18], forward_attempts=7, forward_error="HTTP 500"
    )
    assert notification.fields["local_track_id"] == "17, 18"
    assert notification.fields["forward_attempts"] == 7
    assert notification.fields["forward_error"] == "HTTP 500"


def test_upstream_message_truncates_long_id_lists() -> None:
    notification = upstream_gave_up(
        local_track_ids=list(range(30)), forward_attempts=7, forward_error=None
    )
    assert "(+10 weitere)" in str(notification.fields["local_track_id"])
    assert notification.fields["forward_error"] == "-"


# --- Ereignis-Log -----------------------------------------------------------


def test_a_successful_send_is_logged_as_an_event() -> None:
    events: list[tuple[EventLevel, str, dict]] = []
    _notifier(_config(ntfy=True), events=events).send(stack_start_failed("x"))

    assert len(events) == 1
    level, message, extra = events[0]
    assert level is EventLevel.INFO
    assert message == "Benachrichtigung verschickt"
    assert extra["channels_sent"] == ["ntfy"]
    assert extra["notify_event"] == "stack_start_failed"


def test_a_partial_failure_is_logged_as_a_warning() -> None:
    events: list[tuple[EventLevel, str, dict]] = []
    _notifier(
        _config(ntfy=True, smtp=True),
        smtp=RecordingSmtp(fail="kein Server"),
        events=events,
    ).send(stack_start_failed("x"))

    level, _message, extra = events[0]
    assert level is EventLevel.WARNING
    assert extra["channels_failed"] == ["smtp"]
    assert extra["channels_sent"] == ["ntfy"]


# --- Testnachricht ----------------------------------------------------------


def test_the_test_message_can_target_one_channel() -> None:
    ntfy = RecordingNtfy()
    smtp = RecordingSmtp()
    notifier = _notifier(_config(ntfy=True, smtp=True), ntfy=ntfy, smtp=smtp)

    outcome = notifier.test("ntfy")

    assert outcome.sent == ("ntfy",)
    assert smtp.messages == []
    assert ntfy.last.headers["Title"] == "musicmeta-offline: Testnachricht"


def test_the_test_message_reaches_every_configured_channel() -> None:
    notifier = _notifier(_config(ntfy=True, smtp=True))
    assert set(notifier.test().sent) == {"ntfy", "smtp"}


def test_testing_an_unconfigured_channel_reports_nothing() -> None:
    """„Nicht eingerichtet" ist etwas anderes als „gescheitert" (M8-Anzeige)."""
    outcome = _notifier(_config(ntfy=True)).test("smtp")
    assert outcome.attempted is False
    assert outcome.results == ()


# --- Laufzeit-Verhalten -----------------------------------------------------


def test_channels_follow_the_current_configuration() -> None:
    """Ein in der Admin-UI nachgetragener Kanal wirkt ohne Neustart."""
    current = Config()
    notifier = Notifier(
        lambda: current, client=httpx.Client(transport=httpx.MockTransport(RecordingNtfy()))
    )
    assert notifier.channels() == []

    current = _config(ntfy=True)
    notifier._config = lambda: current  # type: ignore[method-assign,assignment]
    assert [channel.name for channel in notifier.channels()] == ["ntfy"]


def test_an_unreadable_configuration_disables_notifications() -> None:
    """Eine kaputte config.yaml darf den Betrieb nicht anhalten (Muster des Proxys)."""

    def broken() -> Config:
        raise RuntimeError("config.yaml unlesbar")

    notifier = Notifier(broken, client=httpx.Client())
    assert notifier.channels() == []
    assert notifier.send(stack_start_failed("x")).attempted is False


def test_background_send_does_not_block_the_caller() -> None:
    ntfy = RecordingNtfy()
    notifier = _notifier(_config(ntfy=True), ntfy=ntfy)

    thread = notifier.send_background(stack_start_failed("db"))
    notifier.drain(timeout_s=5.0)

    assert thread.is_alive() is False
    assert len(ntfy.requests) == 1
    assert notifier.sent == 1


def test_channel_result_defaults_are_explicit() -> None:
    """Ein Ergebnis ohne Fehlertext ist ein Erfolg — nicht „unbekannt"."""
    assert ChannelResult("ntfy", ok=True).error is None


def test_ntfy_channel_uses_the_configured_url() -> None:
    channel = NtfyChannel("https://example.org/topic", client=httpx.Client(), timeout_s=1.0)
    assert channel.url == "https://example.org/topic"
    assert channel.name == "ntfy"
