"""Die Naht zwischen Weck-Logik und Prozess-Steuerung (Phase M1a).

Ein winziges Modul mit genau zwei Dingen — dem **Vertrag**, den der
:class:`~acoustid_watchdog.wake.WakeCoordinator` an seine Steuerung stellt,
und der **Fehlerbasis**, an der er ihn scheitern sieht. Beides steht
bewusst hier und nicht bei einer der beiden Seiten:

* Bei der Weck-Logik (:mod:`acoustid_watchdog.wake`) koennte die
  Fehlerbasis nicht stehen — die Steuerungsmodule muessen sie importieren,
  und ``wake`` importiert seinerseits die Steuerung.
* Bei der Steuerung koennte der Vertrag nicht stehen: es soll ja gerade
  mehr als eine geben.

**Warum ueberhaupt.** Heute steuert der Waechter Docker-Container
(:mod:`acoustid_watchdog.docker`), ab dem Ein-Container-Umbau steuert er
Prozesse eines Supervisors (HANDOFF v2 §5, DECISIONS 2026-08-04 E1). Der
Koordinator braucht von beidem dasselbe und nicht mehr: „starte alles",
„stoppe alles", „laeuft alles?". Steht dieser Ausschnitt als Protokoll
fest, ist der Umbau ein Adapter-Tausch an einer Stelle
(``WatchdogService``) statt ein Eingriff in die Weck-Logik.

Vorbild ist :class:`~acoustid_watchdog.lifecycle.JobSource`: strukturell
getypt (``Protocol``), absichtlich winzig, ohne Bezug auf die Technik
dahinter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "GroupStatus",
    "ProcessControlError",
    "ProcessGroupController",
]


class ProcessControlError(Exception):
    """Die Prozess-Steuerung konnte einen Auftrag nicht ausfuehren.

    Die gemeinsame Basis aller Fehler, die aus einer Steuerung nach oben
    kommen duerfen — heute :class:`~acoustid_watchdog.docker.DockerError`
    und seine Unterklassen. Der Koordinator faengt ausschliesslich diese
    Basis; er soll nicht wissen, ob gerade ein Docker-Socket oder ein
    Supervisor-Socket geschwiegen hat.

    Was er mit ihr tut, haengt vom Auftrag ab und bleibt unveraendert:
    beim Start und beim Stopp geht der Stack in den Fehlerzustand, beim
    blossen Nachfragen (:meth:`~acoustid_watchdog.wake.WakeCoordinator.observe`)
    bleibt die Anzeige stehen — ein nicht erreichbarer Steuerungsweg ist
    kein Fehler *des Stacks*.
    """


@dataclass(frozen=True, slots=True)
class GroupStatus:
    """Was die Steuerung ueber die Gruppe sagt — eine Frage, drei Antworten.

    Der Ersatz fuer das ``all_running() -> bool`` der Naht-Phase, und der
    Grund fuer den Tausch steht in der M0-Analyse (§2.1, R8): unter Docker
    war „laeuft nicht" **eindeutig gutartig** — ein Container, den niemand
    gestartet hat, schlief. Unter einem Prozess-Supervisor ist das nicht
    mehr so. Ein Prozess kann gestoppt sein (``STOPPED`` — genau das will
    der Idle-Stopp) oder von selbst weggefallen sein (``EXITED``, ``FATAL``,
    ``BACKOFF``). Beides sieht in einem Bool gleich aus, und ein Absturz
    duerfte sich nie als Schlaf maskieren.

    Deshalb liefert die Steuerung beide Halbwahrheiten getrennt: ob alles
    laeuft, und was **unerwartet** weg ist.
    """

    #: Laeuft alles, was zur Gruppe gehoert?
    running: bool
    #: Steht wirklich alles, was gestoppt werden **kann**? Nur dann schlaeft
    #: der Stack.
    #:
    #: Bewusst kein ``not running``: dazwischen liegt der ganze Bereich, in
    #: dem etwas laeuft und etwas nicht — ein halb gestarteter Stack, ein von
    #: Hand gestoppter Einzelprozess, ein Autorestart in ``STARTING``. Als
    #: „schlafend" gelesen waere das die gefaehrlichste Fehlanzeige des
    #: Projekts: eine laufende Postgres haelt das Array wach, waehrend die
    #: Anzeige Ruhe meldet (R8). Die residenten Prozesse (E12) zaehlen hier
    #: nicht mit — sie laufen ja gerade absichtlich weiter.
    sleeping: bool = False
    #: Namen der Prozesse, die von selbst weg sind — leer ist der Normalfall.
    #: Ein gestoppter (schlafender) Prozess steht hier **nicht**.
    crashed: tuple[str, ...] = ()
    #: Prozessname -> Zustandsname der Steuerung (``RUNNING``, ``FATAL``, …).
    #: Reine Diagnose fuers Log; `/status` erweitert darum erst M2 (additiv).
    states: tuple[tuple[str, str], ...] = ()

    @property
    def healthy(self) -> bool:
        """Laeuft alles und ist nichts abgestuerzt?"""
        return self.running and not self.crashed

    @property
    def partial(self) -> bool:
        """Weder ganz wach noch ganz schlafend — und nichts abgestuerzt.

        Der Zustand, den es unter Docker in dieser Form nicht gab und den
        die Anzeige deshalb nie kannte.
        """
        return not self.running and not self.sleeping and not self.crashed


@runtime_checkable
class ProcessGroupController(Protocol):
    """Startet und stoppt die Dienste des Stacks — mehr Wissen hat er nicht.

    Der ganze Ausschnitt, den der
    :class:`~acoustid_watchdog.wake.WakeCoordinator` von seiner Steuerung
    kennt. Wer ihn erfuellt, kann den Stack wecken und schlafen legen:
    seit M1b :class:`~acoustid_watchdog.stack.ServiceGroupController` ueber
    supervisord; in der Naht-Phase war es die Docker-Fassung.

    ``runtime_checkable``, damit ein Test die Zusage nachweisen kann
    (``isinstance``); geprueft wird dabei nur, dass die drei Methoden da
    sind — ihre Signaturen haelt die Typpruefung fest.
    """

    def start(self) -> list[str]:
        """Startet alles, was zum Stack gehoert.

        Returns:
            Namen dessen, was **dieser** Aufruf wirklich gestartet hat —
            was schon lief, steht nicht darin (Idempotenz).

        Raises:
            ProcessControlError: Der Steuerungsweg antwortet nicht, oder
                etwas Angefragtes gibt es nicht.
        """
        ...

    def stop(self) -> list[str]:
        """Stoppt alles, was zum Stack gehoert — in umgekehrter Reihenfolge.

        Returns:
            Namen dessen, was dieser Aufruf wirklich gestoppt hat.

        Raises:
            ProcessControlError: wie bei :meth:`start`.
        """
        ...

    def inspect(self) -> GroupStatus:
        """Erhebt den Zustand der ganzen Gruppe in **einem** Aufruf.

        Returns:
            :class:`GroupStatus` — laeuft alles, und was ist unerwartet
            weggefallen.

        Raises:
            ProcessControlError: wie bei :meth:`start`. Wichtig: das heisst
                „ich weiss es nicht", nicht „der Stack ist kaputt" — der
                Aufrufer laesst seine Anzeige dann stehen.
        """
        ...
