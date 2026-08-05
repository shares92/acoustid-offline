"""Weiterleitung eigener Einreichungen an api.acoustid.org (Phase 12).

Im Modus ``local+upstream`` (ARCHITECTURE §6) bleibt jede Einreichung
zuerst **lokal** — gespeichert und indexiert wie in Phase 11 — und wird
zusaetzlich an den Original-Dienst weitergereicht. Die Weiterleitung ist
damit ausdruecklich eine Zugabe und nie eine Bedingung: was hier schiefgeht,
darf die Submit-Antwort nicht anfassen (lokal gespeichert ist die Wahrheit).

**Wire-Format.** Genau der Original-Submit, den auch Picard schickt:
``application/x-www-form-urlencoded`` an ``/v2/submit``, ``client`` =
:attr:`~shared.config.SubmitConfig.upstream_app_key` (unser eigener
Application-Key), ``user`` = der beim Einreichen hinterlegte
``submitted_by``-Key des Clients — **unveraendert durchgereicht**, weil
acoustid.org keinen Mechanismus fuer „im Namen Dritter" kennt und die
Nutzungsbedingungen den User-Key an den Nutzer binden (Phase-1-Bericht,
„Application-Key & Nutzungsregeln"). Der Fingerprint wird aus dem
gespeicherten Vollvektor **zurueckgerechnet**
(:func:`~shared.fingerprint.encode_fingerprint`) — die Zeichenkette des
Clients steht nirgends, der Codec ist verlustfrei.

**Eine Anfrage je Einreichungsgruppe.** Eine Aufnahme mit drei MBIDs steht
als drei Zeilen in ``local_submission``, geht aber als **eine** Anfrage
hinaus: ``mbid.0`` dreimal belegt, alles andere einmal. Genau so haette der
Client sie eingereicht, und genau so entsteht upstream wieder eine
Einreichung je MBID. Gruppen werden **nicht** zu einer Anfrage gebuendelt:
sie koennen verschiedene ``user``-Keys tragen (davon gibt es je Anfrage nur
einen), und je Gruppe genau eine Anfrage haelt Erfolg und Fehlschlag
sauber der Gruppe zugeordnet, deren Status danach umgesetzt wird.

**Drossel und Backoff.** Die Nutzungsbedingungen erlauben hoechstens drei
Anfragen je Sekunde; :class:`Throttle` haelt den Mindestabstand
prozessweit ein (ein Schloss, monotone Uhr — mehrere Anfrage-Threads
teilen sich die Drossel). Ein ``Retry-After`` liefert der Dienst nicht,
also wartet das eigene Backoff exponentiell ab
:data:`BACKOFF_INITIAL_S` bis :data:`BACKOFF_MAX_S`.

**Statuspfade.**

.. code-block:: text

    indexed ──(200 ok)──────────> forwarded
        │                            ↑
        └──(Fehler)──> forward_failed┘   (forward_attempts + 1)

Weitergeleitet wird nur, was **indexiert** ist: der Status ist eine einzige
Spalte, und eine Einreichung, die den Suchindex noch nie gesehen hat, darf
diese Information nicht gegen ``forwarded`` eintauschen. Zeilen im Status
``new`` holt der naechste Submit nach (Phase 11), erst danach kommen sie in
diese Warteschlange.

**Wann weitergeleitet wird.** Zwei Wege, mehr nicht — einen
Hintergrund-Worker gibt es bewusst nicht (er kollidierte mit dem
Schlaf-Zyklus, DECISIONS „Phase-11-Submit-Details"):

1. :func:`forward_after_submit` — direkt in der Submit-Anfrage, nach
   Speichern und Indexieren. Invariante §8.9 spricht von Fehlversuchen, die
   „beim naechsten Update-Lauf erneut versucht" werden; der **erste**
   Versuch gehoert also hierher. Gedeckelt auf
   :data:`MAX_FORWARD_PER_REQUEST` Gruppen, damit ein grosses Picard-Paket
   die Antwort nicht minutenlang aufhaelt.
2. :func:`drain_queue` — der Warteschlangenlauf, den der Waechter ab
   Phase 19 im taeglichen Update-Zyklus aufruft. Er nimmt alles mit, was
   noch nicht weitergeleitet ist (auch Reste aus 1.) und alle Fehlversuche
   unterhalb der Grenze.

**Sieben Fehlversuche, dann Ruhe** (§8.9). ``forward_attempts`` zaehlt
**Laeufe**, nicht HTTP-Versuche; ab :data:`MAX_FORWARD_ATTEMPTS` faellt die
Gruppe aus dem Arbeitsvorrat, ein strukturiertes Ereignis
(:data:`UPSTREAM_GAVE_UP_EVENT`) geht ins Log — Abnehmer ist die
Benachrichtigung aus Phase 20 — und weiter geht es nur noch von Hand ueber
:func:`retry_forward` (Abnehmer: Trigger-API Phase 19 / Admin-UI Phase 26).

**Ist der Dienst weg, hoert die Runde auf.** Ein Transportfehler (kein Netz,
Zeitueberschreitung, 5xx, 429) bedeutet „upstream ist gerade nicht da" — die
restlichen Gruppen des Laufs werden dann gar nicht erst probiert und
behalten ihren Zaehler. Ein inhaltlicher Fehler (4xx) betrifft nur diese
eine Gruppe; der Lauf macht weiter.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self
from urllib.parse import urlencode

import httpx
import psycopg
from pydantic import SecretStr

from acoustid_api import __version__
from acoustid_api.store import (
    ForwardCandidate,
    load_forward_queue,
    mark_forward_failed,
    mark_forwarded,
    reset_forward_attempts,
)
from shared.config import Config
from shared.fingerprint import encode_fingerprint

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from acoustid_api.service import ApiService

__all__ = [
    "BACKOFF_INITIAL_S",
    "BACKOFF_MAX_S",
    "DEFAULT_DRAIN_LIMIT",
    "DRAIN_ATTEMPTS",
    "MAX_ERROR_CHARS",
    "MAX_FORWARD_ATTEMPTS",
    "MAX_FORWARD_PER_REQUEST",
    "MAX_REQUESTS_PER_SECOND",
    "MIN_REQUEST_INTERVAL_S",
    "REQUEST_ATTEMPTS",
    "UPSTREAM_FORWARDED_EVENT",
    "UPSTREAM_GAVE_UP_EVENT",
    "UPSTREAM_URL",
    "USER_AGENT",
    "ForwardReport",
    "Throttle",
    "UpstreamForwarder",
    "UpstreamResult",
    "drain_queue",
    "forward_after_submit",
    "forward_groups",
    "retry_forward",
]

_LOG = logging.getLogger(__name__)

#: Ziel der Weiterleitung. **Nur https** — der Original-Dienst erzwingt es
#: nicht (Phase-1-Bericht), aber hier gehen fremde User-Keys ueber die
#: Leitung. Tests setzen die Adresse ueber den Konstruktor
#: (:class:`UpstreamForwarder`), so wie der Downloader seine ``base_url``.
UPSTREAM_URL: Final = "https://api.acoustid.org/v2/submit"

#: „Do not make more than 3 requests per second" (webservice.md, zitiert im
#: Phase-1-Bericht). Hart eingehalten, nicht als Richtwert.
MAX_REQUESTS_PER_SECOND: Final = 3

#: Mindestabstand zweier Anfragen, der sich daraus ergibt.
MIN_REQUEST_INTERVAL_S: Final = 1.0 / MAX_REQUESTS_PER_SECOND

#: Erste Wartezeit nach einem Transportfehler; verdoppelt sich je Versuch.
BACKOFF_INITIAL_S: Final = 1.0

#: Deckel der Wartezeit (ARCHITECTURE §7).
BACKOFF_MAX_S: Final = 30.0

#: Fehlversuche je Einreichung, bis kein automatischer Versuch mehr folgt
#: (Invariante §8.9). Gezaehlt werden **Laeufe**, nicht HTTP-Versuche.
MAX_FORWARD_ATTEMPTS: Final = 7

#: HTTP-Versuche je Gruppe im Anfragepfad: genau einer. Ein Backoff von bis
#: zu 30 s waehrend einer offenen Submit-Anfrage waere fuer den Client eine
#: Zumutung — die Warteschlange holt es ohnehin nach.
REQUEST_ATTEMPTS: Final = 1

#: HTTP-Versuche je Gruppe im Warteschlangenlauf. Der laeuft im
#: Update-Zyklus und darf warten.
DRAIN_ATTEMPTS: Final = 5

#: Hoechstzahl Gruppen, die **eine** Submit-Anfrage weiterleitet. Bei drei
#: Anfragen je Sekunde sind das gut drei Sekunden; der Rest bleibt
#: ``indexed`` und geht im naechsten Warteschlangenlauf hinaus.
MAX_FORWARD_PER_REQUEST: Final = 10

#: Hoechstzahl Gruppen je Warteschlangenlauf. Bei drei Anfragen je Sekunde
#: sind 500 Gruppen knapp drei Minuten — genug fuer einen Tagesrueckstand,
#: ohne den Update-Lauf zu blockieren.
DEFAULT_DRAIN_LIMIT: Final = 500

#: So viele Zeichen einer Fehlermeldung landen in ``forward_error``. Eine
#: HTML-Fehlerseite soll die Zeile nicht aufblaehen.
MAX_ERROR_CHARS: Final = 500

#: Ereignisname im Log nach erfolgreicher Weiterleitung.
UPSTREAM_FORWARDED_EVENT: Final = "upstream_submission_forwarded"

#: Ereignisname im Log, sobald eine Einreichung aufgegeben wird (§8.9).
#: Abnehmer ist die Benachrichtigung „Upstream-Submit dauerhaft
#: fehlgeschlagen" aus Phase 20 — hier wird sie nur **geloggt**.
UPSTREAM_GAVE_UP_EVENT: Final = "upstream_forward_gave_up"

#: Kennung gegenueber api.acoustid.org — hoeflich und zuordenbar.
USER_AGENT: Final = (
    f"musicmeta-offline-api/{__version__} (+https://github.com/shares92/musicmeta-offline)"
)

_FORM_TYPE: Final = "application/x-www-form-urlencoded"

#: Statuscodes, die „nochmal versuchen" bedeuten (wie im Downloader).
_TRANSIENT_STATUS: Final = frozenset(
    {
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)

_DEFAULT_CONNECT_TIMEOUT_S: Final = 5.0
_DEFAULT_READ_TIMEOUT_S: Final = 15.0

#: Nur diese Statuswerte duerfen nach ``forwarded``/``forward_failed``
#: wechseln. ``new`` fehlt mit Absicht (siehe Modulkopf).
FORWARDABLE_STATUS: Final = ("indexed", "forward_failed")


class _TransientError(Exception):
    """Upstream ist gerade nicht ansprechbar — ein weiterer Versuch lohnt."""


class _PermanentError(Exception):
    """Diese Einreichung wird upstream so nicht angenommen."""


@dataclass(frozen=True, slots=True)
class UpstreamResult:
    """Ergebnis **einer** weitergeleiteten Gruppe.

    Attributes:
        ok: Upstream hat mit ``{"status": "ok", …}`` geantwortet.
        error: Fehlertext fuer ``forward_error`` (bereits gekuerzt und vom
            Application-Key bereinigt); ``None`` bei Erfolg.
        transient: Der Fehler spricht gegen den Dienst, nicht gegen die
            Einreichung — der Lauf hoert danach auf.
        attempts: Gebrauchte HTTP-Versuche.
        submission_ids: Die Submission-IDs **des Originals**. Sie werden
            bewusst nicht gespeichert (siehe :func:`forward_groups`).
    """

    ok: bool
    error: str | None = None
    transient: bool = False
    attempts: int = 1
    submission_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ForwardReport:
    """Zusammenfassung eines Weiterleitungslaufs (fuer Aufrufer und Log).

    Attributes:
        attempted: Gruppen, fuer die tatsaechlich eine Anfrage lief.
        forwarded: davon erfolgreich (``forwarded``).
        failed: davon gescheitert (``forward_failed``).
        gave_up: Gruppen, die mit diesem Lauf die Grenze aus §8.9 erreicht
            haben — genau diese Zahl Ereignisse steht im Log.
        skipped: Gruppen aus dem Arbeitsvorrat, die nach einem
            Transportfehler nicht mehr probiert wurden.
    """

    attempted: int = 0
    forwarded: int = 0
    failed: int = 0
    gave_up: int = 0
    skipped: int = 0

    @property
    def empty(self) -> bool:
        """Es gab nichts zu tun."""
        return not (self.attempted or self.skipped)


class Throttle:
    """Haelt einen Mindestabstand zwischen zwei Anfragen ein.

    Prozessweit und thread-sicher: die API bearbeitet Anfragen im
    Threadpool, und ein Warteschlangenlauf kann parallel dazu laufen —
    beide teilen sich **eine** Drossel, sonst waere die Grenze aus den
    Nutzungsbedingungen nur pro Thread eingehalten.

    Der Zeitpunkt wird unter dem Schloss reserviert, gewartet wird
    ausserhalb: so blockiert ein wartender Thread die anderen nicht beim
    Reservieren, und die Reihenfolge der Zeitfenster steht trotzdem fest.
    """

    def __init__(
        self,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._next_at: float | None = None

    def acquire(self) -> float:
        """Wartet, bis die naechste Anfrage hinaus darf.

        Returns:
            Die tatsaechlich gewartete Zeit in Sekunden (0.0, wenn der
            Abstand ohnehin schon eingehalten war) — die Tests pruefen
            damit die Drossel als Zahlenreihe.
        """
        with self._lock:
            now = self._monotonic()
            start = now if self._next_at is None else max(now, self._next_at)
            self._next_at = start + self.min_interval_s
        delay = start - now
        if delay > 0:
            self._sleep(delay)
        return max(delay, 0.0)


class UpstreamForwarder:
    """Spricht ``/v2/submit`` von api.acoustid.org.

    Kennt Datenbank und Statusmaschine **nicht** — er baut die Anfrage, haelt
    Drossel und Backoff ein und sagt, was dabei herauskam. Die Buchfuehrung
    macht :func:`forward_groups`.
    """

    def __init__(
        self,
        app_key: SecretStr | str,
        *,
        url: str = UPSTREAM_URL,
        client: httpx.Client | None = None,
        throttle: Throttle | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        backoff_initial_s: float = BACKOFF_INITIAL_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        read_timeout_s: float = _DEFAULT_READ_TIMEOUT_S,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
    ) -> None:
        """
        Args:
            app_key: Eigener Application-Key (``acoustid.submit.upstream_app_key``).
            url: Zieladresse; **muss** ``https://`` sein. In Tests zeigt sie
                auf einen ``httpx.MockTransport``.
            client: Vorhandener ``httpx.Client``. Wird dann **nicht** von
                :meth:`close` geschlossen.
            throttle: Gemeinsame Drossel; ohne Angabe eine eigene mit
                :data:`MIN_REQUEST_INTERVAL_S`.
            sleep: Wartefunktion des Backoffs; in Tests ersetzbar.
            monotonic: Uhr der Drossel; in Tests ersetzbar.

        Raises:
            ValueError: Kein Application-Key, oder die Adresse ist nicht
                ``https``.
        """
        secret = app_key.get_secret_value() if isinstance(app_key, SecretStr) else app_key
        if not secret:
            raise ValueError(
                "acoustid.submit.upstream_app_key fehlt — ohne eigenen Application-Key "
                "nimmt api.acoustid.org nichts an"
            )
        if not url.lower().startswith("https://"):
            raise ValueError(
                f"Upstream-Adresse muss https sein, bekommen: {url!r} — "
                "ueber die Leitung gehen fremde user-Keys (ARCHITECTURE §7)"
            )
        self._app_key = secret
        self.url = url
        self.throttle = throttle or Throttle(sleep=sleep, monotonic=monotonic)
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            follow_redirects=False,
        )

    @classmethod
    def from_config(cls, config: Config, **kwargs: Any) -> Self | None:
        """Baut den Weiterleiter, falls der Modus ihn verlangt.

        Returns:
            ``None`` in den Modi ``off`` und ``local`` — dann gibt es nichts
            weiterzuleiten und auch keinen HTTP-Pool.
        """
        if not config.acoustid.submit.upstream_enabled:
            return None
        return cls(config.acoustid.submit.upstream_app_key, **kwargs)

    # --- Lebenszyklus ------------------------------------------------------

    def close(self) -> None:
        """Schliesst den HTTP-Pool, falls dieser Weiterleiter ihn anlegte."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        """Ohne Application-Key — der taucht nirgends in Ausgaben auf."""
        return f"{type(self).__name__}(url={self.url!r})"

    # --- Weiterleiten ------------------------------------------------------

    def body_for(self, group: ForwardCandidate) -> str:
        """Der Anfragerumpf einer Gruppe (form-urlencoded).

        Oeffentlich, weil er die eigentliche Kompatibilitaetsaussage dieser
        Phase ist: die Tests pruefen ihn Feld fuer Feld.
        """
        fields: list[tuple[str, str]] = [
            ("format", "json"),
            ("client", self._app_key),
            ("clientversion", __version__),
            # Der user-Key des einreichenden Clients, unveraendert. Er ist
            # ein Geheimnis des Nutzers und wird deshalb nie geloggt.
            ("user", group.submitted_by or ""),
            ("duration.0", str(group.duration)),
            ("fingerprint.0", encode_fingerprint(group.hashes)),
        ]
        optional: list[tuple[str, object | None]] = [
            ("bitrate.0", group.bitrate),
            ("fileformat.0", group.fileformat),
            ("puid.0", group.puid),
            ("foreignid.0", group.foreignid),
            ("track.0", group.track),
            ("artist.0", group.artist),
            ("album.0", group.album),
            ("albumartist.0", group.album_artist),
            ("trackno.0", group.track_no),
            ("discno.0", group.disc_no),
            ("year.0", group.year),
        ]
        fields += [(name, str(value)) for name, value in optional if value is not None]
        # Mehrfaches `mbid.0` — genau so nimmt das Original eine Aufnahme mit
        # mehreren Zuordnungen entgegen und macht daraus je MBID eine Zeile.
        fields += [("mbid.0", mbid) for mbid in group.mbids]
        return urlencode(fields)

    def submit(
        self, group: ForwardCandidate, *, max_attempts: int = REQUEST_ATTEMPTS
    ) -> UpstreamResult:
        """Leitet **eine** Gruppe weiter (mit Drossel und Backoff).

        Wirft nie: jeder Fehlschlag kommt als :class:`UpstreamResult` zurueck,
        denn ein Upstream-Problem darf weder die Submit-Anfrage noch den
        Warteschlangenlauf abbrechen.
        """
        if not group.submitted_by:
            # Kann ueber den Endpunkt nicht entstehen (`user` ist Pflicht),
            # aber die Spalte ist NULL-bar. Ohne User-Key gibt es nichts zu
            # schicken — und raten waere eine Zweckentfremdung fremder Keys.
            return UpstreamResult(
                ok=False,
                error="kein user-Key hinterlegt (submitted_by ist leer)",
                transient=False,
                attempts=0,
            )

        body = self.body_for(group)
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                submission_ids = self._post(body)
            except _PermanentError as exc:
                return UpstreamResult(
                    ok=False, error=self._clean(str(exc)), transient=False, attempts=attempt
                )
            except _TransientError as exc:
                last_error = exc
                if attempt == max_attempts:
                    break
                delay = min(self.backoff_initial_s * 2 ** (attempt - 1), self.backoff_max_s)
                _LOG.warning(
                    "Upstream-Submit gescheitert — neuer Versuch",
                    extra={
                        "local_track_id": group.local_track_id,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "delay_s": delay,
                        "reason": self._clean(str(exc)),
                    },
                )
                self._sleep(delay)
                continue
            return UpstreamResult(ok=True, attempts=attempt, submission_ids=submission_ids)

        tries = "einem Versuch" if max_attempts == 1 else f"{max_attempts} Versuchen"
        return UpstreamResult(
            ok=False,
            error=self._clean(f"kein Erfolg nach {tries}: {last_error}"),
            transient=True,
            attempts=max_attempts,
        )

    def _post(self, body: str) -> tuple[int, ...]:
        """Ein HTTP-Versuch, Drossel inklusive.

        Raises:
            _TransientError: Netzfehler oder ein Status mit Aussicht auf
                Besserung.
            _PermanentError: Der Dienst lehnt diese Einreichung ab.
        """
        self.throttle.acquire()
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": _FORM_TYPE,
            "Accept": "application/json",
        }
        try:
            response = self._client.post(self.url, content=body.encode("utf-8"), headers=headers)
        except httpx.HTTPError as exc:
            raise _TransientError(f"Uebertragung gescheitert: {exc}") from exc

        status = response.status_code
        if status in _TRANSIENT_STATUS:
            raise _TransientError(f"HTTP {status}: {_first_line(response)}")
        if status != HTTPStatus.OK:
            raise _PermanentError(f"HTTP {status}: {_first_line(response)}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise _TransientError(f"Antwort ist kein JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise _TransientError(f"Antwort ist kein Objekt, sondern {type(payload).__name__}")
        if payload.get("status") != "ok":
            error = payload.get("error")
            detail = error.get("message") if isinstance(error, dict) else payload.get("status")
            code = error.get("code") if isinstance(error, dict) else None
            raise _PermanentError(f"Fehler {code}: {detail}")

        submissions = payload.get("submissions")
        if not isinstance(submissions, list):
            return ()
        return tuple(
            entry["id"]
            for entry in submissions
            if isinstance(entry, dict) and isinstance(entry.get("id"), int)
        )

    def _clean(self, text: str) -> str:
        """Application-Key entfernen und kuerzen.

        Der Key steht im Anfragerumpf; eine Fehlermeldung, die den Rumpf
        zitiert, wuerde ihn sonst ins Log und in ``forward_error`` tragen
        (ARCHITECTURE §6: Secrets bleiben maskiert).
        """
        cleaned = text.replace(self._app_key, "***")
        if len(cleaned) > MAX_ERROR_CHARS:
            cleaned = cleaned[: MAX_ERROR_CHARS - 1] + "…"
        return cleaned


def _first_line(response: httpx.Response) -> str:
    """Die erste Zeile einer Fehlerantwort — genug fuers Protokoll."""
    try:
        text = response.text
    except UnicodeDecodeError, httpx.HTTPError:  # pragma: no cover - Notnagel
        return "<unlesbar>"
    return text.strip().splitlines()[0][:200] if text.strip() else "<leer>"


# --- Buchfuehrung -----------------------------------------------------------


def forward_groups(
    connection: psycopg.Connection,
    forwarder: UpstreamForwarder,
    groups: Sequence[ForwardCandidate],
    *,
    max_attempts: int,
) -> ForwardReport:
    """Leitet Gruppen weiter und schreibt ihren Status fort.

    Reihenfolge je Gruppe: erst die Anfrage, dann der Statuswechsel — dieselbe
    Regel wie bei der Indexierung (nie „erledigt" vermerken, was noch nicht
    heraus ist).

    **Die Submission-IDs des Originals werden nicht gespeichert.** Sie sind
    nur gegen dessen ``/v2/submission_status`` etwas wert, und dieser Dienst
    fragt dort nichts nach; ``/v2/submission_status`` in Phase 13 beantwortet
    ausschliesslich **lokale** IDs. Sie mit den lokalen IDs in eine Spalte zu
    legen, waere ein Datenmodellfehler, eine eigene Spalte eine Migration ohne
    Abnehmer (§5.2 nennt fuer diese Phase genau vier Spalten). Nachvollziehbar
    bleiben sie ueber das Ereignis :data:`UPSTREAM_FORWARDED_EVENT` im Log.
    """
    attempted = forwarded = failed = gave_up = 0
    for position, group in enumerate(groups):
        result = forwarder.submit(group, max_attempts=max_attempts)
        attempted += 1
        if result.ok:
            mark_forwarded(connection, group.local_track_id)
            forwarded += 1
            _LOG.info(
                "Einreichung an api.acoustid.org weitergeleitet",
                extra={
                    "event": UPSTREAM_FORWARDED_EVENT,
                    "local_track_id": group.local_track_id,
                    "mbids": len(group.mbids),
                    "http_attempts": result.attempts,
                    # IDs des Originals — bewusst nur hier, nicht in der DB.
                    "upstream_submission_ids": list(result.submission_ids),
                },
            )
            continue

        attempts = mark_forward_failed(connection, group.local_track_id, result.error or "")
        failed += 1
        if attempts >= MAX_FORWARD_ATTEMPTS:
            gave_up += 1
            _LOG.error(
                "Upstream-Weiterleitung endgueltig aufgegeben — manueller Versuch noetig",
                extra={
                    "event": UPSTREAM_GAVE_UP_EVENT,
                    "local_track_id": group.local_track_id,
                    "forward_attempts": attempts,
                    "max_forward_attempts": MAX_FORWARD_ATTEMPTS,
                    "forward_error": result.error,
                },
            )
        else:
            _LOG.warning(
                "Upstream-Weiterleitung gescheitert — bleibt in der Warteschlange",
                extra={
                    "local_track_id": group.local_track_id,
                    "forward_attempts": attempts,
                    "forward_error": result.error,
                },
            )
        if result.transient:
            # Der Dienst ist gerade nicht da: die restlichen Gruppen jetzt zu
            # probieren, kostet nur Zeit und Fehlversuche.
            skipped = len(groups) - position - 1
            if skipped:
                _LOG.warning(
                    "Upstream nicht erreichbar — Rest der Warteschlange bleibt liegen",
                    extra={"skipped": skipped, "local_track_id": group.local_track_id},
                )
            return ForwardReport(attempted, forwarded, failed, gave_up, skipped)

    return ForwardReport(attempted, forwarded, failed, gave_up, 0)


def forward_after_submit(
    connection: psycopg.Connection,
    service: ApiService,
    local_track_ids: Sequence[int],
    *,
    limit: int = MAX_FORWARD_PER_REQUEST,
) -> ForwardReport:
    """Der erste Versuch, direkt in der Submit-Anfrage.

    Weitergeleitet wird **nur**, was diese Anfrage gespeichert hat — ein
    Rueckstand aus frueheren Anfragen gehoert in den Warteschlangenlauf und
    nicht in die Antwortzeit eines fremden Clients.

    Wirft nie: die Submit-Antwort haengt nicht an der Weiterleitung
    (docs/api-submit.md, „Bewusste Abweichungen"). Auch ein unerwarteter
    Fehler landet nur im Log.
    """
    forwarder = _forwarder_of(service)
    if forwarder is None or not local_track_ids:
        return ForwardReport()
    try:
        groups = load_forward_queue(
            connection,
            limit=limit,
            max_attempts=MAX_FORWARD_ATTEMPTS,
            only=list(local_track_ids),
        )
        return forward_groups(connection, forwarder, groups, max_attempts=REQUEST_ATTEMPTS)
    except Exception:
        _LOG.exception(
            "Upstream-Weiterleitung abgebrochen — die Einreichung ist lokal gespeichert",
            extra={"local_track_ids": list(local_track_ids)[:20]},
        )
        return ForwardReport()


def drain_queue(
    connection: psycopg.Connection,
    service: ApiService,
    *,
    limit: int = DEFAULT_DRAIN_LIMIT,
    max_attempts: int = DRAIN_ATTEMPTS,
) -> ForwardReport:
    """Arbeitet die Upstream-Warteschlange ab (Invariante §8.9).

    Einzeln aufrufbar und ohne Nebenwirkungen auf laufende Anfragen — der
    Waechter ruft sie ab Phase 19 im taeglichen Update-Zyklus, die Admin-UI
    ab Phase 26 auf Knopfdruck.

    Arbeitsvorrat: alles, was indexiert und noch nicht weitergeleitet ist,
    plus alle Fehlversuche unterhalb von :data:`MAX_FORWARD_ATTEMPTS` —
    aelteste zuerst.

    Returns:
        :class:`ForwardReport`; im Modus ``off``/``local`` ein leerer.
    """
    forwarder = _forwarder_of(service)
    if forwarder is None:
        return ForwardReport()
    groups = load_forward_queue(connection, limit=limit, max_attempts=MAX_FORWARD_ATTEMPTS)
    report = forward_groups(connection, forwarder, groups, max_attempts=max_attempts)
    if not report.empty:
        _LOG.info(
            "Upstream-Warteschlange abgearbeitet",
            extra={
                "attempted": report.attempted,
                "forwarded": report.forwarded,
                "failed": report.failed,
                "gave_up": report.gave_up,
                "skipped": report.skipped,
            },
        )
    return report


def retry_forward(
    connection: psycopg.Connection,
    service: ApiService,
    *,
    local_track_ids: Sequence[int] | None = None,
    limit: int = DEFAULT_DRAIN_LIMIT,
    max_attempts: int = DRAIN_ATTEMPTS,
) -> ForwardReport:
    """Manueller Wiederholungsversuch — der Hook aus §8.9.

    Setzt den Fehlerzaehler gezielt zurueck und versucht **genau diese**
    Einreichungen erneut; ohne ihn bliebe eine aufgegebene Gruppe fuer immer
    liegen. Aufrufer sind die Trigger-API des Waechters (Phase 19) und die
    Admin-UI (Phase 26).

    Args:
        local_track_ids: Die Gruppen, um die es geht. Ohne Angabe alle, die
            die Grenze erreicht haben — der Knopf „Upstream-Queue senden".

    Returns:
        :class:`ForwardReport` des anschliessenden Versuchs.
    """
    forwarder = _forwarder_of(service)
    if forwarder is None:
        return ForwardReport()
    only = list(local_track_ids) if local_track_ids is not None else None
    reset = reset_forward_attempts(
        connection,
        local_track_ids=only,
        min_attempts=1 if only is not None else MAX_FORWARD_ATTEMPTS,
    )
    if reset:
        _LOG.info(
            "Fehlerzaehler der Upstream-Warteschlange zurueckgesetzt",
            extra={"local_track_ids": reset[:20], "count": len(reset)},
        )
    selection = only if only is not None else reset
    if not selection:
        return ForwardReport()
    groups = load_forward_queue(
        connection, limit=limit, max_attempts=MAX_FORWARD_ATTEMPTS, only=selection
    )
    return forward_groups(connection, forwarder, groups, max_attempts=max_attempts)


def _forwarder_of(service: ApiService) -> UpstreamForwarder | None:
    """Der Weiterleiter des Dienstes — oder ``None``, wenn nicht zustaendig.

    Zwei Bedingungen, weil beide einzeln schiefgehen koennen: der Modus muss
    ``local+upstream`` sein (er kann sich ueber die Admin-UI aendern), und der
    Dienst muss einen Weiterleiter haben (im Modus ``off``/``local`` legt er
    keinen an).
    """
    if not service.config.acoustid.submit.upstream_enabled:
        return None
    forwarder = getattr(service, "upstream", None)
    if forwarder is None:
        _LOG.warning(
            "acoustid.submit.mode ist 'local+upstream', aber es gibt keinen Weiterleiter — "
            "Einreichungen bleiben in der Warteschlange"
        )
    return forwarder
