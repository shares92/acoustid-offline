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

from typing import Protocol, runtime_checkable

__all__ = [
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


@runtime_checkable
class ProcessGroupController(Protocol):
    """Startet und stoppt die Dienste des Stacks — mehr Wissen hat er nicht.

    Der ganze Ausschnitt, den der
    :class:`~acoustid_watchdog.wake.WakeCoordinator` von seiner Steuerung
    kennt. Wer ihn erfuellt, kann den Stack wecken und schlafen legen:
    heute :class:`~acoustid_watchdog.wake.StackController` ueber die
    Docker-Engine-API, spaeter der Supervisor-Gegenpart.

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

    def all_running(self) -> bool:
        """Laeuft alles, was zum Stack gehoert?

        Raises:
            ProcessControlError: wie bei :meth:`start`.
        """
        ...
