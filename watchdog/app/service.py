"""Laufzeitumgebung des Waechters: Zustandsdatenbank, Konfiguration, Zustand.

Das Gegenstueck zu :class:`acoustid_api.service.ApiService` — mit einem
entscheidenden Unterschied: der Waechter haelt **keine** Verbindung zum
Array. Kein Postgres-Pool, kein Index-Client, kein MusicBrainz. Was er
braucht, liegt auf dem Cache-Pool und ist damit auch bei schlafendem Stack
da (Invariante §8.2):

* **Zustandsdatenbank** (:mod:`acoustid_watchdog.store`) — SQLite mit
  API-Keys, Admin-Login, Lauf-Historie und Ereignis-Log.
* **Laufzeit-Konfiguration** (:mod:`acoustid_watchdog.config_store`) — die
  ``config.yaml``, deren einziger Schreiber der Waechter ist, samt
  Reload-Signal Richtung API.
* **Stack-Zustand** (:mod:`acoustid_watchdog.state`) — was die Container
  gerade tun, soweit der Waechter es weiss.

:meth:`WatchdogService.open` ist zugleich der **Erststart-Pfad**: Datenbank
anlegen und migrieren, ``config.yaml`` mit den Defaults aus §6 erzeugen,
Admin-Passwort generieren und ins Containerlog schreiben. Alle drei
Schritte sind idempotent — ein Neustart wiederholt keinen davon.

Ab Phase 15 kommen hier Docker-Steuerung und Proxy-Client dazu, ab Phase 19
der Scheduler. Der Schnitt ist bewusst derselbe wie im API-Dienst: alles
Langlebige entsteht einmal beim Start und wird beim Herunterfahren wieder
freigegeben.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Self

from acoustid_watchdog import __version__
from acoustid_watchdog.admin import ensure_admin_user
from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.events import EventLevel, log_event
from acoustid_watchdog.reload import ReloadMarker
from acoustid_watchdog.state import StackStateTracker
from acoustid_watchdog.store import Database
from shared.config import Config
from shared.env import EnvSettings

__all__ = ["EVENT_SOURCE", "WatchdogService"]

_LOG = logging.getLogger(__name__)

#: Quelle aller Ereignisse, die der Waechter-Kern selbst schreibt. Spaetere
#: Teile setzen eigene Quellen (``scheduler``, ``proxy``, ``backup``), damit
#: die Logansicht danach filtern kann (Phase 27).
EVENT_SOURCE = "watchdog"


class WatchdogService:
    """Haelt die langlebigen Ressourcen eines Waechter-Prozesses."""

    def __init__(
        self,
        settings: EnvSettings,
        db: Database,
        config_store: ConfigStore,
        state: StackStateTracker | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.config_store = config_store
        self.state = state if state is not None else StackStateTracker.sleeping()

    @classmethod
    def from_env(cls, env: EnvSettings | None = None) -> Self:
        """Baut den Dienst aus den ``AOFF_``-Variablen.

        Nichts wird dabei geoeffnet oder angelegt — das macht :meth:`open`,
        damit ein Importfehler nicht schon beim Modulimport Dateien anfasst.
        """
        settings = env or EnvSettings.from_env()
        return cls(
            settings,
            Database.for_data_dir(settings.data_dir),
            ConfigStore.from_path(settings.config_path),
        )

    # --- Lebenszyklus -------------------------------------------------------

    def open(self) -> Self:
        """Erststart und Normalstart — beides derselbe, idempotente Weg."""
        self.db.open()
        self.config_store.load()
        first_password = ensure_admin_user(self.db)

        _LOG.info(
            "Waechter konfiguriert",
            extra={
                "version": __version__,
                "data_dir": str(self.settings.data_dir),
                "config_path": str(self.settings.config_path),
                "db_path": str(self.db.path),
                "schema_version": self.db.schema_version,
                "port": self.settings.port,
                "first_start": first_password is not None,
            },
        )

        if first_password is not None:
            # Der Klartext steht ausschliesslich im Containerlog
            # (:mod:`acoustid_watchdog.admin`). Das Ereignis-Log ist
            # persistent und liegt hinter genau dieser Anmeldung — dort
            # gehoert nur der Vermerk hin, nicht das Passwort.
            self.log_event(
                EventLevel.WARNING,
                "Erststart: Admin-Passwort erzeugt und ins Containerlog geschrieben",
            )
        self.log_event(
            EventLevel.INFO,
            "Waechter gestartet",
            {"version": __version__, "schema_version": self.db.schema_version},
        )
        return self

    def close(self) -> None:
        """Gibt die Zustandsdatenbank frei."""
        self.db.close()

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Zugriffe -----------------------------------------------------------

    @property
    def config(self) -> Config:
        """Aktuelle Laufzeit-Konfiguration."""
        return self.config_store.config

    def update_config(self, config: Config, *, reason: str = "config_saved") -> ReloadMarker:
        """Schreibt die Konfiguration, signalisiert den Reload, protokolliert.

        Der Weg, den `/admin/config` in Phase 25 nimmt. Er liegt hier und
        nicht im :class:`~acoustid_watchdog.config_store.ConfigStore`, weil
        nur der Dienst beides kennt: die Datei und das Ereignis-Log.
        """
        marker = self.config_store.save(config, reason=reason)
        self.log_event(
            EventLevel.INFO,
            "Konfiguration geaendert",
            {"reason": reason, "reload_generation": marker.generation},
        )
        return marker

    def log_event(
        self,
        level: EventLevel,
        message: str,
        extra: dict[str, Any] | None = None,
        *,
        source: str = EVENT_SOURCE,
    ) -> None:
        """Schreibt ein Ereignis ins ``event_log`` und ins Containerlog."""
        log_event(self.db, level, source, message, extra)
