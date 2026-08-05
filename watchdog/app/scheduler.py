"""Der Zeitplan des Waechters (M2.5) — was wann faellig ist.

Der dritte Dauerlaeufer neben Idle-Stopp und Zustandsabgleich
(:mod:`acoustid_watchdog.lifecycle`), und der einzige, der die Instanz von
selbst **aufweckt**: ARCHITECTURE §3 („Dass auch ein faelliger Job weckt,
kommt mit dem Scheduler in M2.5").

Zwei Termine, beide aus §6 und beide in **lokaler** Zeit:

=========================  ==================================================
``acoustid.update.time``   Taeglicher Delta-Import (Default ``04:00``)
``backup.time``            Sicherung (Default ``04:45``) — nur wenn
                           ``backup.dir`` eingerichtet ist
=========================  ==================================================

``discogs.update.check_time`` steht schon im Schema, hat aber noch keinen
Job (M3) und wird deshalb hier nicht eingeplant.

**Faelligkeit heisst „seit dem heutigen Termin lief noch keiner".** Nicht
„es ist gerade 04:00": ein Takt von 30 Sekunden trifft eine Minute nie
sicher, und ein Container, der um 04:00 gerade neu startete, verloere den
Tag. Gefragt wird deshalb die Historie (:func:`~acoustid_watchdog.runs.
latest_run_since`) — sie ueberlebt einen Neustart, und ein von Hand
angestossener Lauf zaehlt mit.

**Verpasste Termine werden nachgeholt**, solange der Tag laeuft: startet
der Container um 06:00, laeuft der 04:00-Import sofort danach. Ein
verspaeteter Datenabgleich ist besser als keiner, und der Zyklus legt den
Stack anschliessend wieder schlafen.

**Ein fehlgeschlagener Lauf wird nicht sofort wiederholt** (Invariante
§8.4: „beim naechsten Zyklus"). Das ist Absicht: die haeufigsten Ursachen —
kein Netz, volle Platte, Lueckenbefund — sind am selben Tag meist dieselben,
und ein Wiederholungslauf im Minutentakt hielte das Array wach, ohne etwas
zu reparieren. Die Historie zeigt den Fehlschlag, die Benachrichtigung
meldet ihn, und der naechste Termin versucht es erneut — auf einem Stand,
der dank ``import_state`` genau dort fortsetzt, wo der gescheiterte Lauf
aufhoerte.

**Ein Abbruch zaehlt wie ein Fehlschlag.** Fuer die Faelligkeit ist der
Ausgang eines Laufs ohne Bedeutung — gefragt wird nur, **ob** seit dem
Termin einer lief. Fuer den haeufigsten Abbruchgrund ist das genau
richtig: der Plattenplatz-Guard (§8.8) bricht ab, weil kein Platz da ist,
und der kommt nicht von selbst zurueck. Sofort erneut zu starten
reparierte nichts.

**Ein neuer Termin ist eine neue Gelegenheit** — auch am selben Tag. Wer
die Ursache beseitigt hat (Platz geschaffen, Guard gelockert) und nicht
bis morgen warten will, setzt die Uhrzeit neu. Entscheidend ist dabei,
dass der neue Termin **nach** dem letzten Lauf liegt; ein Termin davor
ist derselbe verbrauchte Termin und feuert nicht noch einmal. Genau daran
scheiterte der erste Anlauf des E2E-Zyklus-Tests.

**Umstellung auf Sommerzeit.** Der Termin wird in lokaler Zeit gebildet;
in der Nacht der Umstellung kann er dadurch ausfallen oder doppelt
faellig scheinen. Beides ist harmlos: ein ausgefallener Lauf wird am
naechsten Tag nachgeholt, ein doppelter von der Historie verhindert.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.runs import RunKind, latest_run_since
from shared.config import Config

if TYPE_CHECKING:  # nur fuer die Typannotation
    from acoustid_watchdog.jobs import JobManager
    from acoustid_watchdog.store import Database

__all__ = [
    "DEFAULT_TICK_S",
    "SCHEDULE",
    "ScheduledJob",
    "Scheduler",
    "next_due_at",
    "utc_boundary",
]

_LOG = logging.getLogger(__name__)

#: Abstand zweier Faelligkeitspruefungen. Eine halbe Minute: die kleinste
#: Einheit eines Termins ist die Minute, und die Pruefung kostet eine
#: SQLite-Abfrage. Bewusst **kein** §6-Schluessel (Muster aus DECISIONS
#: 2026-08-01, Punkt 2).
DEFAULT_TICK_S: Final = 30.0


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Ein Termin: welcher Job, wann, und ob er ueberhaupt eingerichtet ist."""

    kind: RunKind
    #: Liest ``HH:MM`` aus der **aktuellen** Konfiguration.
    time_of_day: Callable[[Config], str]
    #: Ist der Job eingerichtet? (``backup.dir`` leer = Backup aus, §6.)
    enabled: Callable[[Config], bool] = lambda _config: True

    def scheduled_at(self, config: Config, now: datetime) -> datetime:
        """Der heutige Termin in lokaler Zeit."""
        return next_due_at(now, self.time_of_day(config))


#: Die Termine des Betriebs. Reihenfolge ist Rangfolge: sind beide faellig,
#: laeuft erst der Import und danach — beim naechsten Takt — die Sicherung.
#: Das ist auch die Reihenfolge der Vorgabewerte (04:00 vor 04:45) und die
#: fachlich richtige: gesichert wird der neue Stand, nicht der alte.
SCHEDULE: Final[tuple[ScheduledJob, ...]] = (
    ScheduledJob(
        kind=RunKind.ACOUSTID_DELTA,
        time_of_day=lambda config: config.acoustid.update.time,
    ),
    ScheduledJob(
        kind=RunKind.BACKUP,
        time_of_day=lambda config: config.backup.time,
        enabled=lambda config: config.backup.enabled,
    ),
)


def next_due_at(now: datetime, time_of_day: str) -> datetime:
    """Der Termin ``HH:MM`` am Tag von ``now`` — in derselben Zeitzone."""
    hour, minute = (int(part) for part in time_of_day.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def utc_boundary(moment: datetime) -> str:
    """Ein lokaler Zeitpunkt als ISO-8601 in UTC — das Format der Historie.

    Die Zeitstempel in ``update_run`` stehen in UTC
    (:func:`acoustid_watchdog.store.utc_now`); ein Termin in lokaler Zeit
    muss dorthin uebersetzt werden, bevor man ihn vergleicht.
    """
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Scheduler:
    """Prueft im Takt, ob ein Termin faellig ist, und stoesst ihn an."""

    def __init__(
        self,
        db: Database,
        manager: JobManager,
        config: Callable[[], Config],
        *,
        schedule: tuple[ScheduledJob, ...] = SCHEDULE,
        interval_s: float = DEFAULT_TICK_S,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        """
        Args:
            db: Zustandsdatenbank — die Historie beantwortet die
                Faelligkeitsfrage.
            manager: Trigger-API; sie sorgt zugleich dafuer, dass nie zwei
                Jobs gleichzeitig laufen.
            config: Zugriff auf die **aktuelle** Laufzeit-Konfiguration;
                die Termine werden bei jeder Pruefung frisch gelesen, damit
                eine Aenderung in der Admin-UI ohne Neustart greift (Muster
                des :class:`~acoustid_watchdog.lifecycle.IdleStopper`).
            schedule: Die Termine; Tests geben eigene mit.
            interval_s: Abstand zweier Pruefungen.
            clock: Zeitquelle. ``datetime.now().astimezone()`` liefert die
                **lokale** Zeit mit Zeitzone — §6 nennt die Termine in
                lokaler Zeit, und die Historie steht in UTC.
        """
        self._db = db
        self._manager = manager
        self._config = config
        self._schedule = schedule
        self.interval_s = interval_s
        self._clock = clock
        #: Wie viele Termine dieser Prozess ausgeloest hat (Tests, Metrik).
        self.triggered = 0

    # --- Einzelschritt ------------------------------------------------------

    async def check(self) -> RunKind | None:
        """Eine Faelligkeitspruefung.

        Returns:
            Die Art des angestossenen Laufs, oder ``None``, wenn nichts
            faellig war (oder schon ein Job laeuft).
        """
        if self._manager.running:
            # Kein Vorwurf: ein Import darf laenger dauern als bis zum
            # naechsten Termin. Der naechste Takt fragt wieder.
            return None
        config = self._runtime_config()
        now = self._clock()
        for job in self._schedule:
            if not job.enabled(config):
                continue
            if not await self._is_due(job, config, now):
                continue
            if self._manager.trigger(job.kind, reason="scheduler"):
                self.triggered += 1
                _LOG.info(
                    "Termin faellig, Lauf angestossen",
                    extra={"job_kind": job.kind.value, "scheduled": job.time_of_day(config)},
                )
                return job.kind
        return None

    async def _is_due(self, job: ScheduledJob, config: Config, now: datetime) -> bool:
        """Ist der heutige Termin erreicht — und lief seither keiner?"""
        try:
            scheduled = job.scheduled_at(config, now)
        except ValueError:
            # Eine ungueltige Uhrzeit kaeme durch das Schema nicht durch
            # (`TimeOfDay`, shared.config) — hier ist sie nur denkbar, wenn
            # jemand die Datei von Hand kaputt macht. Dann faellt der
            # Termin aus, statt den Waechter anzuhalten.
            _LOG.exception("Termin nicht lesbar", extra={"job_kind": job.kind.value})
            return False
        if now < scheduled:
            return False
        since = utc_boundary(scheduled)
        previous = await run_in_threadpool(latest_run_since, self._db, job.kind, since)
        return previous is None

    def _runtime_config(self) -> Config:
        """Die laufende Konfiguration — oder die Defaults aus §6.

        Eine unlesbare ``config.yaml`` darf den Zeitplan nicht anhalten:
        dann gelten die dokumentierten Vorgabewerte (04:00, Backup aus).
        Dieselbe Haltung wie im Proxy und im Idle-Stopp.
        """
        try:
            return self._config()
        except Exception:
            _LOG.exception("Laufzeit-Konfiguration nicht lesbar, Vorgabewerte werden benutzt")
            return Config()

    # --- Schleife -----------------------------------------------------------

    async def run(self) -> None:
        """Prueft bis zum Abbruch periodisch auf faellige Termine.

        Laeuft als Hintergrundaufgabe im Lifespan; der Abbruch beim
        Herunterfahren ist ``CancelledError`` und beendet sie sofort.
        """
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.check()
            except Exception:
                # Eine Hintergrundschleife darf an nichts sterben — sonst
                # liefe die Instanz nie wieder von selbst.
                _LOG.exception("Faelligkeitspruefung fehlgeschlagen")
