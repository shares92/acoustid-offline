"""Benachrichtigungen des Waechters — ntfy/Webhook und SMTP (ARCHITECTURE §6).

Der Betreiber sieht seine Instanz im Normalfall nie: sie schlaeft, importiert
nachts und schlaeft wieder ein. Genau deshalb braucht sie einen Weg, sich zu
melden, wenn etwas nicht stimmt — sonst faellt ein gescheiterter Import erst
auf, wenn jemand nach dem Datenstand fragt.

**Fuenf Ereignisse** (:class:`NotifyEvent`), alle aus der M2.5-Aufgabenliste:

===========================  ================================================
``import_failed``            Ein Job endete nicht mit Erfolg (Exit-Code ≠ 0)
``disk_low``                 Der Plattenplatz-Guard hat abgebrochen (§8.8)
``stack_start_failed``       Der Stack liess sich nicht wecken (§7)
``upstream_forward_gave_up`` Eine Einreichung hat die 7er-Grenze erreicht
                             (§8.9; das Ereignis selbst entsteht im
                             API-Prozess, hier kommt es ueber den Report des
                             ``queue-send``-Jobs an)
``version_drift``            Der Bestand gehoert zu einer anderen
                             PostgreSQL-Major (E14)
===========================  ================================================

**Zwei Kanaele, beide per Default aus.** Ein leerer Wert heisst „aus" — die
Regel des ganzen Konfigurationsschemas (:mod:`shared.config`). Ist kein Kanal
eingerichtet, passiert schlicht nichts; das ist der Auslieferungszustand und
kein Fehler.

* :class:`NtfyChannel` — ein ``POST`` auf ``notify.ntfy.url``. Der Rumpf ist
  der Meldungstext, Titel und Dringlichkeit stehen in Kopfzeilen: das ist das
  ntfy-Protokoll, und fuer einen beliebigen Webhook bleibt es ein POST mit
  lesbarem Text.
* :class:`SmtpChannel` — ``notify.smtp.*``; STARTTLS ueber Port 587 (der
  Vorgabewert), implizites TLS ueber 465, Anmeldung nur mit gesetztem
  Benutzer.

**Eine Benachrichtigung darf nichts anhalten.** :meth:`Notifier.send` wirft
nie: ein nicht erreichbarer Mailserver ist ein Grund fuer eine Warnung, nicht
fuer einen abgebrochenen Import. Was gesendet wurde und was nicht, steht im
Ergebnis (:class:`NotifyOutcome`) und im Ereignis-Log.

**Die Konfiguration wird bei jedem Versand frisch gelesen.** Wer in der
Admin-UI (M8) einen Kanal eintraegt, soll die naechste Meldung bekommen —
ohne Neustart. Dasselbe Muster wie im Proxy und im Idle-Stopp: der Dienst
haelt die Quelle, nicht den Wert.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.header import Header
from email.message import EmailMessage
from enum import StrEnum
from typing import Any, Final, Protocol

import httpx

from acoustid_watchdog.events import EventLevel
from shared.config import Config, NtfyConfig, SmtpConfig

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "IMPLICIT_TLS_PORT",
    "ChannelResult",
    "Notification",
    "Notifier",
    "NotifyChannel",
    "NotifyEvent",
    "NotifyOutcome",
    "NtfyChannel",
    "SmtpChannel",
]

_LOG = logging.getLogger(__name__)

#: Leseschranke eines Versands. Kurz gehalten: eine Benachrichtigung laeuft
#: im Anschluss an einen Job oder einen Weckvorgang, und ein haengender
#: Mailserver darf den Ablauf nicht um Minuten verlaengern.
DEFAULT_TIMEOUT_S: Final = 10.0

#: SMTP-Port mit **implizitem** TLS. Alles andere bekommt STARTTLS.
IMPLICIT_TLS_PORT: Final = 465


class NotifyEvent(StrEnum):
    """Anlass einer Benachrichtigung (Modul-Docstring).

    Der Wert ist zugleich der Feldname im Ereignis-Log und — bei
    ``upstream_forward_gave_up`` — derselbe String, den der API-Prozess
    schon in Phase 12 geloggt hat (``api/app/upstream.py``). Absicht: wer
    im Containerlog danach sucht, findet beide Enden derselben Sache.
    """

    IMPORT_FAILED = "import_failed"
    DISK_LOW = "disk_low"
    STACK_START_FAILED = "stack_start_failed"
    UPSTREAM_GAVE_UP = "upstream_forward_gave_up"
    VERSION_DRIFT = "version_drift"
    #: Die Testnachricht der Admin-UI (M8) — kein Betriebsereignis.
    TEST = "test"


@dataclass(frozen=True, slots=True)
class Notification:
    """Was verschickt werden soll.

    Attributes:
        event: Anlass; bestimmt die Dringlichkeit der ntfy-Kopfzeile.
        title: Eine Zeile — Betreff der Mail, ``Title`` bei ntfy.
        message: Der Klartext. Deutsch, wie alles, was der Betreiber sieht.
        level: Level des Ereignisses im Log.
        fields: Zusatzangaben; sie werden an den Meldungstext angehaengt
            (``schluessel: wert`` je Zeile) und stehen im Ereignis-Log.
    """

    event: NotifyEvent
    title: str
    message: str
    level: EventLevel = EventLevel.ERROR
    fields: Mapping[str, Any] = field(default_factory=dict)

    def body(self) -> str:
        """Meldungstext samt Zusatzangaben — der Rumpf beider Kanaele."""
        if not self.fields:
            return self.message
        lines = [f"{name}: {value}" for name, value in self.fields.items()]
        return "\n".join([self.message, "", *lines])


@dataclass(frozen=True, slots=True)
class ChannelResult:
    """Ergebnis **eines** Kanals."""

    channel: str
    ok: bool
    #: Fehlertext, wenn der Versand scheiterte.
    error: str | None = None
    #: Wohin genau (ntfy: die URL, SMTP: die Empfaenger) — fuer die
    #: Rueckmeldung der Testnachricht in der Admin-UI.
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    """Ergebnis eines Versands ueber alle eingerichteten Kanaele."""

    results: tuple[ChannelResult, ...] = ()

    @property
    def attempted(self) -> bool:
        """War ueberhaupt ein Kanal eingerichtet?"""
        return bool(self.results)

    @property
    def sent(self) -> tuple[str, ...]:
        return tuple(result.channel for result in self.results if result.ok)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(result.channel for result in self.results if not result.ok)

    @property
    def ok(self) -> bool:
        """Alle eingerichteten Kanaele haben zugestellt."""
        return self.attempted and not self.failed


class NotifyChannel(Protocol):
    """Ein Zustellweg — absichtlich winzig, wie :class:`~acoustid_watchdog.
    lifecycle.JobSource`."""

    #: Kurzname fuer Log, Ergebnis und die Auswahl der Testnachricht.
    name: str

    def send(self, notification: Notification) -> str:
        """Stellt zu.

        Returns:
            Klartext, wohin zugestellt wurde (fuer :attr:`ChannelResult.detail`).

        Raises:
            Exception: Jeder Zustellfehler; der :class:`Notifier` faengt ihn.
        """
        ...


# --- ntfy / Webhook ---------------------------------------------------------


#: Dringlichkeit der ntfy-Kopfzeile je Level. ``max`` bleibt bewusst
#: unbenutzt: es umgeht auf Mobilgeraeten die Ruhezeiten, und keines
#: unserer Ereignisse rechtfertigt, jemanden nachts zu wecken — der Stack
#: schlaeft dann ja auch.
_NTFY_PRIORITY: Final[Mapping[EventLevel, str]] = {
    EventLevel.DEBUG: "min",
    EventLevel.INFO: "default",
    EventLevel.WARNING: "high",
    EventLevel.ERROR: "high",
    EventLevel.CRITICAL: "urgent",
}

#: Symbol je Anlass (ntfy zeigt es vor dem Titel).
_NTFY_TAGS: Final[Mapping[NotifyEvent, str]] = {
    NotifyEvent.IMPORT_FAILED: "x",
    NotifyEvent.DISK_LOW: "floppy_disk",
    NotifyEvent.STACK_START_FAILED: "rotating_light",
    NotifyEvent.UPSTREAM_GAVE_UP: "warning",
    NotifyEvent.VERSION_DRIFT: "warning",
    NotifyEvent.TEST: "white_check_mark",
}


def _header_value(text: str) -> str:
    """Kopfzeilen-Wert, der auch mit Umlauten heil ankommt.

    HTTP-Kopfzeilen sind latin-1; ein Titel mit Umlaut braechte den Versand
    sonst mit einem ``UnicodeEncodeError`` ab — ausgerechnet dann, wenn
    etwas zu melden ist. Nicht-ASCII wird deshalb nach RFC 2047 kodiert;
    ntfy dekodiert das, und ein beliebiger Webhook sieht wenigstens
    lesbares ASCII.
    """
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return Header(text, "utf-8").encode()
    return text


class NtfyChannel:
    """``POST`` auf ``notify.ntfy.url`` — ntfy-Protokoll, Webhook-tauglich."""

    name = "ntfy"

    def __init__(self, url: str, *, client: httpx.Client, timeout_s: float) -> None:
        self.url = url
        self._client = client
        self._timeout_s = timeout_s

    def send(self, notification: Notification) -> str:
        """Schickt Titel, Dringlichkeit und Text.

        Raises:
            httpx.HTTPError: Verbindung, Zeitueberschreitung oder ein
                Fehlerstatus der Gegenstelle.
        """
        response = self._client.post(
            self.url,
            content=notification.body().encode("utf-8"),
            headers={
                "Title": _header_value(notification.title),
                "Priority": _NTFY_PRIORITY[notification.level],
                "Tags": _NTFY_TAGS[notification.event],
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        return self.url


# --- SMTP -------------------------------------------------------------------


def _recipients(raw: str) -> list[str]:
    """``notify.smtp.to`` als Liste — Komma **oder** Semikolon trennen.

    Beides ist in Mailprogrammen ueblich, und ein Betreiber, der das eine
    tippt, waehrend wir das andere erwarten, bekaeme eine Adresse mit
    Semikolon darin — also gar keine Mail.
    """
    parts = raw.replace(";", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


class SmtpChannel:
    """Mailversand ueber ``notify.smtp.*``."""

    name = "smtp"

    def __init__(self, config: SmtpConfig, *, timeout_s: float) -> None:
        self.config = config
        self._timeout_s = timeout_s

    def send(self, notification: Notification) -> str:
        """Baut die Mail und stellt sie zu.

        Raises:
            OSError: Verbindung oder Anmeldung gescheitert
                (``smtplib``-Fehler stammen von :class:`OSError` ab).
        """
        to = _recipients(self.config.to)
        message = EmailMessage()
        message["Subject"] = notification.title
        message["From"] = self.config.from_addr
        message["To"] = ", ".join(to)
        message.set_content(notification.body())

        with self._connect() as server:
            user = self.config.user
            if user:
                server.login(user, self.config.password.get_secret_value())
            server.send_message(message, from_addr=self.config.from_addr, to_addrs=to)
        return ", ".join(to)

    def _connect(self) -> smtplib.SMTP:
        """Verbindung mit TLS — implizit auf 465, sonst STARTTLS.

        Ohne Verschluesselung geht es nicht: die Anmeldedaten stuenden sonst
        im Klartext auf der Leitung. Ein Server ohne STARTTLS ist damit
        nicht bedienbar — das ist Absicht und faellt beim Testversand auf,
        nicht erst beim ersten Fehler im Betrieb.
        """
        host, port = self.config.host, self.config.port
        context = ssl.create_default_context()
        if port == IMPLICIT_TLS_PORT:
            return smtplib.SMTP_SSL(host, port, timeout=self._timeout_s, context=context)
        server = smtplib.SMTP(host, port, timeout=self._timeout_s)
        try:
            server.starttls(context=context)
        except BaseException:
            server.close()
            raise
        return server


# --- Der Versender ----------------------------------------------------------


#: Quelle der Notify-Ereignisse im ``event_log`` — eigene Quelle, damit die
#: Logansicht (M8) danach filtern kann.
EVENT_SOURCE: Final = "notify"


class Notifier:
    """Schickt eine :class:`Notification` ueber alle eingerichteten Kanaele."""

    def __init__(
        self,
        config: Callable[[], Config],
        *,
        log_event: Callable[..., None] | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        smtp_channel: Callable[[SmtpConfig], NotifyChannel] | None = None,
    ) -> None:
        """
        Args:
            config: Zugriff auf die **aktuelle** Laufzeit-Konfiguration; sie
                wird bei jedem Versand frisch gelesen (Modul-Docstring).
            log_event: ``(level, message, extra)`` — Anschluss ans
                Ereignis-Log. Ohne Angabe bleibt es beim Containerlog.
            client: Vorhandener ``httpx.Client`` (Tests). Wird dann nicht
                von :meth:`close` geschlossen.
            timeout_s: Leseschranke beider Kanaele.
            smtp_channel: Fabrik des Mail-Kanals; Tests setzen hier eine
                Attrappe ein, statt einen echten Server zu brauchen.
        """
        self._config = config
        self._log_event = log_event
        self._timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_s)
        self._smtp_channel = smtp_channel or (
            lambda smtp: SmtpChannel(smtp, timeout_s=self._timeout_s)
        )
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()
        #: Wie viele Benachrichtigungen dieser Prozess verschickt hat
        #: (Kennzahl fuer `/metrics`, Diagnose in Tests).
        self.sent = 0
        #: Wie viele Zustellversuche gescheitert sind.
        self.failures = 0

    def close(self) -> None:
        """Wartet auf unterwegs befindliche Meldungen und schliesst den Pool."""
        self.drain()
        if self._owns_client:
            self._client.close()

    # --- Kanaele ------------------------------------------------------------

    def channels(self, config: Config | None = None) -> list[NotifyChannel]:
        """Die gerade eingerichteten Kanaele — leere Liste heisst „aus".

        Args:
            config: Konfiguration; ohne Angabe die aktuelle.
        """
        current = config if config is not None else self._current_config()
        notify = current.notify
        channels: list[NotifyChannel] = []
        if notify.ntfy.enabled:
            channels.append(self._ntfy_channel(notify.ntfy))
        if notify.smtp.enabled:
            channels.append(self._smtp_channel(notify.smtp))
        return channels

    def _ntfy_channel(self, ntfy: NtfyConfig) -> NotifyChannel:
        return NtfyChannel(ntfy.url, client=self._client, timeout_s=self._timeout_s)

    def _current_config(self) -> Config:
        """Die laufende Konfiguration — oder die Defaults aus §6.

        Eine unlesbare ``config.yaml`` darf den Betrieb nicht anhalten;
        dann gilt der Vorgabewert (alle Kanaele aus). Dieselbe Haltung wie
        im Proxy und im Idle-Stopp.
        """
        try:
            return self._config()
        except Exception:
            _LOG.exception("Laufzeit-Konfiguration nicht lesbar, Benachrichtigung entfaellt")
            return Config()

    # --- Versand ------------------------------------------------------------

    def send(self, notification: Notification) -> NotifyOutcome:
        """Verschickt ueber alle eingerichteten Kanaele — und wirft **nie**.

        Ein Zustellfehler ist eine Warnung, kein Abbruch: die Meldung
        gehoert zu einem Vorgang, der ohnehin schon schiefgegangen ist, und
        ein nicht erreichbarer Mailserver darf ihn nicht zusaetzlich
        entgleisen lassen.
        """
        channels = self.channels()
        if not channels:
            _LOG.debug(
                "Kein Benachrichtigungskanal eingerichtet",
                extra={"notify_event": notification.event.value},
            )
            return NotifyOutcome()

        results = tuple(self._deliver(channel, notification) for channel in channels)
        outcome = NotifyOutcome(results)
        self.sent += len(outcome.sent)
        self.failures += len(outcome.failed)
        self._announce(notification, outcome)
        return outcome

    def send_background(self, notification: Notification) -> threading.Thread:
        """Wie :meth:`send`, aber ohne den Aufrufer aufzuhalten.

        **Der Weg fuer alles, was in der Ereignisschleife passiert.** Beide
        Kanaele sind synchron (``httpx.Client``, ``smtplib``); ein
        haengender Mailserver wuerde den Waechter sonst fuer die Dauer des
        Timeouts blockieren — und zwar genau in dem Moment, in dem er
        gerade einen Fehler meldet.

        Returns:
            Den laufenden Thread. Tests warten mit :meth:`drain`; im
            Betrieb sieht ihn niemand wieder.
        """
        thread = threading.Thread(
            target=self.send,
            args=(notification,),
            name=f"notify-{notification.event.value}",
            daemon=True,
        )
        with self._threads_lock:
            # Fertige Threads wegraeumen, damit die Liste nicht mitwaechst.
            self._threads = [running for running in self._threads if running.is_alive()]
            self._threads.append(thread)
        thread.start()
        return thread

    def drain(self, timeout_s: float | None = None) -> None:
        """Wartet auf alle laufenden Hintergrund-Versande.

        Fuer Tests (Determinismus) und fuer das Herunterfahren: eine
        Meldung, die gerade unterwegs ist, soll noch ankommen.
        """
        with self._threads_lock:
            pending = list(self._threads)
        for thread in pending:
            thread.join(timeout_s if timeout_s is not None else self._timeout_s * 2)

    def test(self, channel_name: str | None = None) -> NotifyOutcome:
        """Testnachricht — der Knopf „Testnachricht senden" (§6, M8).

        Args:
            channel_name: Nur diesen Kanal (``"ntfy"`` / ``"smtp"``); ohne
                Angabe alle eingerichteten.

        Returns:
            Das Ergebnis je Kanal. Ein nicht eingerichteter Kanal liefert
            **kein** Ergebnis — die Oberflaeche kann „nicht eingerichtet"
            damit von „eingerichtet, aber gescheitert" unterscheiden.
        """
        notification = Notification(
            event=NotifyEvent.TEST,
            title="musicmeta-offline: Testnachricht",
            message=(
                "Diese Nachricht bestaetigt, dass der Kanal erreichbar ist. "
                "Sie wurde von Hand ausgeloest."
            ),
            level=EventLevel.INFO,
        )
        channels = [
            channel
            for channel in self.channels()
            if channel_name is None or channel.name == channel_name
        ]
        if not channels:
            return NotifyOutcome()
        outcome = NotifyOutcome(tuple(self._deliver(c, notification) for c in channels))
        self.sent += len(outcome.sent)
        self.failures += len(outcome.failed)
        self._announce(notification, outcome)
        return outcome

    def _deliver(self, channel: NotifyChannel, notification: Notification) -> ChannelResult:
        try:
            detail = channel.send(notification)
        except Exception as error:
            _LOG.warning(
                "Benachrichtigung konnte nicht zugestellt werden",
                extra={
                    "notify_channel": channel.name,
                    "notify_event": notification.event.value,
                    "error": f"{type(error).__name__}: {error}",
                },
            )
            return ChannelResult(channel.name, ok=False, error=f"{type(error).__name__}: {error}")
        _LOG.info(
            "Benachrichtigung verschickt",
            extra={"notify_channel": channel.name, "notify_event": notification.event.value},
        )
        return ChannelResult(channel.name, ok=True, detail=detail)

    def _announce(self, notification: Notification, outcome: NotifyOutcome) -> None:
        """Schreibt das Ergebnis ins Ereignis-Log (§5 „event_log").

        Nur wenn ueberhaupt ein Kanal eingerichtet war: eine Instanz ohne
        Benachrichtigungen wuerde den Ringpuffer sonst mit Meldungen
        darueber fuellen, dass es nichts zu melden gab.
        """
        if self._log_event is None or not outcome.attempted:
            return
        extra: dict[str, Any] = {
            "notify_event": notification.event.value,
            "channels_sent": list(outcome.sent),
            "title": notification.title,
        }
        if outcome.failed:
            extra["channels_failed"] = list(outcome.failed)
            extra["errors"] = [r.error for r in outcome.results if not r.ok]
            self._log_event(
                EventLevel.WARNING,
                "Benachrichtigung nicht auf allen Kanaelen zugestellt",
                extra,
            )
            return
        self._log_event(EventLevel.INFO, "Benachrichtigung verschickt", extra)


# --- Fertige Meldungen ------------------------------------------------------
#
# Die Texte stehen hier und nicht bei den Ausloesern: so ist der Wortlaut an
# einer Stelle nachzulesen (und zu uebersetzen), und ein Ausloeser kann keine
# Meldung ohne Anlass bauen.


def import_failed(kind: str, *, result: str, error: str | None, run_id: int) -> Notification:
    """Ein Job endete nicht mit Erfolg."""
    return Notification(
        event=NotifyEvent.IMPORT_FAILED,
        title=f"musicmeta-offline: {kind} fehlgeschlagen",
        message=(
            f"Der Lauf #{run_id} ({kind}) ist nicht durchgelaufen. "
            "Der naechste Zyklus wiederholt ihn automatisch (Invariante §8.4); "
            "der Stand bleibt resumierbar."
        ),
        fields={"ergebnis": result, "fehler": error or "-", "lauf": run_id},
    )


def disk_low(path: str, *, free_gb: float, min_free_gb: float) -> Notification:
    """Der Plattenplatz-Guard hat abgebrochen (§8.8)."""
    return Notification(
        event=NotifyEvent.DISK_LOW,
        title="musicmeta-offline: Plattenplatz knapp",
        message=(
            f"Unter {path} sind nur noch {free_gb:.1f} GiB frei, gefordert sind "
            f"{min_free_gb:.1f} GiB (disk.min_free_gb). Der Lauf wurde abgebrochen, "
            "bevor er die Platte fuellen konnte."
        ),
        fields={"pfad": path, "frei_gib": round(free_gb, 1), "gefordert_gib": min_free_gb},
    )


def stack_start_failed(detail: str) -> Notification:
    """Der Stack liess sich nicht wecken (§7 „Fehlerverhalten")."""
    return Notification(
        event=NotifyEvent.STACK_START_FAILED,
        title="musicmeta-offline: Stack-Start fehlgeschlagen",
        message=(
            "Die Prozesse liessen sich nicht starten. Der Stack bleibt im "
            "Fehlerzustand stehen, bis ein Weckversuch ihn aufloest — Anfragen "
            "werden bis dahin mit HTTP 503 beantwortet."
        ),
        fields={"grund": detail},
    )


def upstream_gave_up(
    *, local_track_ids: Sequence[int], forward_attempts: int, forward_error: str | None
) -> Notification:
    """Einreichungen haben die 7er-Grenze erreicht (§8.9).

    Traegt die Felder des Phase-12-Ereignisses (``local_track_id``,
    ``forward_attempts``, ``forward_error``), damit Logzeile und Meldung
    dieselbe Sprache sprechen.
    """
    ids = ", ".join(str(value) for value in local_track_ids[:20])
    if len(local_track_ids) > 20:
        ids += f" (+{len(local_track_ids) - 20} weitere)"
    return Notification(
        event=NotifyEvent.UPSTREAM_GAVE_UP,
        title="musicmeta-offline: Upstream-Weiterleitung aufgegeben",
        message=(
            f"{len(local_track_ids)} Einreichung(en) haben die Fehlergrenze erreicht "
            "und werden nicht mehr von selbst versucht. Der manuelle Wiederholungs"
            "versuch steht in der Admin-UI (§8.9)."
        ),
        fields={
            "local_track_id": ids,
            "forward_attempts": forward_attempts,
            "forward_error": forward_error or "-",
        },
    )


def version_drift(*, expected: int, found: Sequence[int], detail: str) -> Notification:
    """Der Bestand gehoert zu einer anderen PostgreSQL-Major (E14)."""
    return Notification(
        event=NotifyEvent.VERSION_DRIFT,
        title="musicmeta-offline: Versions-Drift der Datenbank",
        message=(
            "Im Datenverzeichnis liegt ein Bestand einer anderen PostgreSQL-Major. "
            "Der Waechter verweigert den Start der Datenbank — ohne Migration waere "
            "der Bestand nicht bedienbar (docs/migration-v1-v2.md)."
        ),
        fields={
            "erwartet": expected,
            "gefunden": ", ".join(str(value) for value in found),
            "detail": detail,
        },
    )
