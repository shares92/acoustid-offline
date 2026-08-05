"""Rotation der Waechter-Logdatei ``/config/logs/watchdog.log`` (M2.5).

**Warum ueberhaupt eine eigene Rotation.** Alle anderen Prozesse loggen
ueber supervisord, und das rotiert sie selbst
(``stdout_logfile_maxbytes=10MB``, ``stdout_logfile_backups=3``). Der
Waechter ist die Ausnahme: sein Log geht **doppelt** hinaus (E16) — auf
die Container-Ausgabe und in eine Datei —, und zwar ueber eine Pipeline::

    /bin/sh -c 'exec … -m acoustid_watchdog 2>&1 | tee -a /config/logs/watchdog.log'

Fuer supervisord ist das Ziel damit ``/dev/fd/1`` mit
``stdout_logfile_maxbytes=0``; die Datei schreibt ``tee``, und um die
kuemmert sich niemand. Auf einem dauerhaft laufenden Wächter waechst sie
unbegrenzt — auf dem SSD-Cache-Pool (v2 §3).

**Warum kopieren und kuerzen statt umbenennen.** ``tee`` haelt den
Dateideskriptor offen. Nach einem ``rename`` schriebe es unbeirrt in die
umbenannte Datei weiter, und die neue bliebe fuer immer leer — der
klassische Fehler, und einer, der erst nach Wochen auffaellt. Ein
``truncate`` auf derselben Inode wirkt dagegen sofort: ``tee -a``
schreibt im Anhaenge-Modus, also nach dem Kuerzen wieder ab Offset 0.

Der bekannte Preis dieses Verfahrens ist ein winziges Fenster zwischen
Kopie und Kuerzen, in dem eine Logzeile verloren gehen kann. Das ist
hier vertretbar: dieselbe Zeile steht zugleich auf der Container-Ausgabe,
und die persistenten Ereignisse liegen ohnehin im ``event_log``
(:mod:`acoustid_watchdog.events`).

Die Grenzen sind bewusst dieselben wie in ``supervisor/supervisord.conf``:
10 MB je Datei, drei Generationen. So sieht ein Betreiber in
``/config/logs`` ueberall dasselbe Muster.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Final

from starlette.concurrency import run_in_threadpool

__all__ = [
    "DEFAULT_BACKUPS",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_MAX_BYTES",
    "LOG_DIRNAME",
    "WATCHDOG_LOG_FILENAME",
    "LogRotator",
]

_LOG = logging.getLogger(__name__)

#: Unterverzeichnis der Logs im Datenverzeichnis (``/config/logs``) —
#: derselbe Ort, den ``supervisord.conf`` als ``childlogdir`` benutzt.
LOG_DIRNAME: Final = "logs"

#: Die Datei, die ``tee`` schreibt (supervisord.conf, ``[program:watchdog]``).
WATCHDOG_LOG_FILENAME: Final = "watchdog.log"

#: Ab dieser Groesse wird rotiert — wie ``stdout_logfile_maxbytes=10MB``.
DEFAULT_MAX_BYTES: Final = 10 * 1024 * 1024

#: So viele Generationen bleiben — wie ``stdout_logfile_backups=3``.
DEFAULT_BACKUPS: Final = 3

#: Abstand zweier Groessenpruefungen. Fuenf Minuten: die Datei waechst im
#: Normalbetrieb um Kilobyte je Stunde, und die Pruefung kostet ein
#: ``stat``. Bewusst **kein** §6-Schluessel (Muster aus DECISIONS
#: 2026-08-01, Punkt 2).
DEFAULT_INTERVAL_S: Final = 300.0


class LogRotator:
    """Haelt ``watchdog.log`` klein — kopieren und kuerzen (Modul-Docstring)."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """
        Args:
            path: Die Logdatei.
            max_bytes: Groesse, ab der rotiert wird; ``0`` schaltet ab.
            backups: Wie viele Generationen aufbewahrt werden.
            interval_s: Abstand zweier Pruefungen.
        """
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.interval_s = interval_s
        #: Wie oft rotiert wurde (Diagnose, Tests).
        self.rotations = 0

    @classmethod
    def for_data_dir(cls, data_dir: Path, **options: object) -> LogRotator:
        """Der Rotator am Vorgabeort (``<data_dir>/logs/watchdog.log``)."""
        return cls(Path(data_dir) / LOG_DIRNAME / WATCHDOG_LOG_FILENAME, **options)  # type: ignore[arg-type]

    # --- Einzelschritt ------------------------------------------------------

    def due(self) -> bool:
        """Ist die Datei ueber der Grenze?

        Eine fehlende Datei ist kein Fehler: ausserhalb des Containers gibt
        es sie nicht (dort schreibt kein ``tee``), und vor der ersten
        Logzeile auch nicht.
        """
        if self.max_bytes <= 0:
            return False
        try:
            return self.path.stat().st_size > self.max_bytes
        except OSError:
            return False

    def rotate(self) -> bool:
        """Rotiert einmal — kopieren, kuerzen, Generationen verschieben.

        Returns:
            ``True``, wenn wirklich rotiert wurde.
        """
        if not self.due():
            return False
        try:
            self._shift_generations()
            shutil.copy2(self.path, self._generation(1))
            # **Kuerzen statt loeschen**: `tee` haelt den Deskriptor offen,
            # und nur so schreibt es in dieselbe Datei weiter.
            with self.path.open("r+b") as handle:
                handle.truncate(0)
        except OSError as error:
            # Ein volles oder schreibgeschuetztes `/config` darf den
            # Waechter nicht anhalten — die Zeilen stehen ohnehin auch auf
            # der Container-Ausgabe.
            _LOG.warning(
                "Logdatei liess sich nicht rotieren",
                extra={"log_path": str(self.path), "error": str(error)},
            )
            return False
        self.rotations += 1
        _LOG.info(
            "Logdatei rotiert",
            extra={"log_path": str(self.path), "backups": self.backups},
        )
        return True

    def _generation(self, number: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{number}")

    def _shift_generations(self) -> None:
        """``.2`` wird ``.3``, ``.1`` wird ``.2``; die aelteste faellt weg."""
        self._generation(self.backups).unlink(missing_ok=True)
        for number in range(self.backups - 1, 0, -1):
            source = self._generation(number)
            if source.exists():
                source.replace(self._generation(number + 1))

    # --- Schleife -----------------------------------------------------------

    async def run(self) -> None:
        """Prueft bis zum Abbruch periodisch die Groesse."""
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await run_in_threadpool(self.rotate)
            except Exception:
                # Eine Hintergrundschleife darf an nichts sterben.
                _LOG.exception("Logrotation fehlgeschlagen")
