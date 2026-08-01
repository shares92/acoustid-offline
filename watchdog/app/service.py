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

Seit Phase 15 kommen die drei Dinge dazu, mit denen der Waechter den Stack
tatsaechlich steuert — und nur diese drei sprechen ueberhaupt nach aussen:

* **Docker-Steuerung** (:mod:`acoustid_watchdog.docker`) — der Unix-Socket,
  ueber den Stack-Container starten und stoppen (Invariante §8.1).
* **Weck-Koordination** (:mod:`acoustid_watchdog.wake`) — haelt Anfragen,
  bis der Stack bereit ist; genau ein Weckvorgang, egal wie viele warten.
* **Reverse-Proxy** (:mod:`acoustid_watchdog.proxy`) — der Weg von
  ``/v2/*`` zum API-Dienst.

:meth:`WatchdogService.open` ist zugleich der **Erststart-Pfad**: Datenbank
anlegen und migrieren, ``config.yaml`` mit den Defaults aus §6 erzeugen,
Admin-Passwort generieren und ins Containerlog schreiben. Alle drei
Schritte sind idempotent — ein Neustart wiederholt keinen davon.

Ab Phase 19 kommt der Scheduler dazu. Der Schnitt ist bewusst derselbe wie
im API-Dienst: alles Langlebige entsteht einmal beim Start und wird beim
Herunterfahren wieder freigegeben.
"""

from __future__ import annotations

import logging
from functools import partial
from types import TracebackType
from typing import Any, Self

from acoustid_watchdog import __version__
from acoustid_watchdog.admin import ensure_admin_user
from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.docker import DockerClient
from acoustid_watchdog.events import EventLevel, log_event
from acoustid_watchdog.proxy import ReverseProxy
from acoustid_watchdog.reload import ReloadMarker
from acoustid_watchdog.state import StackStateTracker
from acoustid_watchdog.store import Database
from acoustid_watchdog.wake import (
    API_BASE_URL,
    ReadinessProbe,
    StackController,
    WakeCoordinator,
)
from shared.config import Config
from shared.env import EnvSettings

__all__ = ["EVENT_SOURCE", "WAKE_EVENT_SOURCE", "WatchdogService"]

_LOG = logging.getLogger(__name__)

#: Quelle aller Ereignisse, die der Waechter-Kern selbst schreibt. Spaetere
#: Teile setzen eigene Quellen (``scheduler``, ``backup``), damit die
#: Logansicht danach filtern kann (Phase 27).
EVENT_SOURCE = "watchdog"

#: Quelle der Weck-Ereignisse (Phase 15).
WAKE_EVENT_SOURCE = "wake"


class WatchdogService:
    """Haelt die langlebigen Ressourcen eines Waechter-Prozesses."""

    def __init__(
        self,
        settings: EnvSettings,
        db: Database,
        config_store: ConfigStore,
        state: StackStateTracker | None = None,
        *,
        docker: DockerClient | None = None,
        probe: ReadinessProbe | None = None,
        proxy: ReverseProxy | None = None,
    ) -> None:
        """
        Args:
            settings: Bootstrap-Werte des Prozesses.
            db: Zustandsdatenbank (Cache-Pool).
            config_store: Laufzeit-Konfiguration.
            state: Stack-Zustand; ohne Angabe ``schlafend``.
            docker: Docker-Steuerung. Ohne Angabe entsteht ein Client auf
                den fest verdrahteten Socket-Pfad; Tests geben eine eigene
                Fassung mit.
            probe: Bereitschaftsfrage an den API-Healthcheck.
            proxy: Reverse-Proxy auf den API-Dienst.
        """
        self.settings = settings
        self.db = db
        self.config_store = config_store
        self.state = state if state is not None else StackStateTracker.sleeping()

        self.docker = docker if docker is not None else DockerClient()
        self.probe = probe if probe is not None else ReadinessProbe()
        self.proxy = proxy if proxy is not None else ReverseProxy(API_BASE_URL)
        self.stack = StackController(self.docker)
        self.wake = WakeCoordinator(
            self.stack,
            self.probe,
            self.state,
            # Weck-Ereignisse bekommen eine eigene Quelle, damit die
            # Logansicht (Phase 27) danach filtern kann.
            log_event=partial(self.log_event, source=WAKE_EVENT_SOURCE),
        )

    @classmethod
    def from_env(cls, env: EnvSettings | None = None) -> Self:
        """Baut den Dienst aus den ``AOFF_``-Variablen.

        Nichts wird dabei geoeffnet oder angelegt — das macht :meth:`open`,
        damit ein Importfehler nicht schon beim Modulimport Dateien anfasst.
        Auch die HTTP-Pools von Proxy, Probe und Docker-Client machen erst
        beim ersten Aufruf eine Verbindung auf.
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
        # Der Stack-Zustand liegt nur im Speicher (DECISIONS 2026-08-01,
        # Punkt 6) und wird deshalb bei jedem Start neu aus Docker erhoben —
        # der Betreiber kann den Stack zwischenzeitlich von Hand gestartet
        # haben. Schlaegt das fehl, laeuft der Waechter trotzdem weiter.
        stack_state = self.wake.refresh()

        self.log_event(
            EventLevel.INFO,
            "Waechter gestartet",
            {
                "version": __version__,
                "schema_version": self.db.schema_version,
                "stack_state": stack_state.value,
            },
        )
        return self

    def close(self) -> None:
        """Gibt Zustandsdatenbank, Docker-Client und Probe frei.

        Der Proxy haelt einen **asynchronen** Pool und wird deshalb in
        :meth:`aclose` geschlossen — das ist der Weg, den der Lifespan der
        Anwendung nimmt.
        """
        self.db.close()
        self.docker.close()
        self.probe.close()

    async def aclose(self) -> None:
        """Schliesst zusaetzlich den Proxy-Pool (Lifespan-Ende)."""
        await self.proxy.aclose()
        self.close()

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
