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

* **Prozess-Steuerung** (:mod:`acoustid_watchdog.process` und
  :mod:`acoustid_watchdog.stack`) — der Unix-Socket von supervisord, ueber
  den Postgres, Suchindex und API starten und stoppen (Invariante §8.1).
  Seit M1b; bis dahin war es der Docker-Socket.
* **Weck-Koordination** (:mod:`acoustid_watchdog.wake`) — haelt Anfragen,
  bis der Stack bereit ist; genau ein Weckvorgang, egal wie viele warten.
* **Reverse-Proxy** (:mod:`acoustid_watchdog.proxy`) — der Weg von
  ``/v2/*`` zum API-Dienst.

Seit Phase 18 haengen zwei Waechter am Eingang des Proxy-Pfads — beide
ebenfalls ohne jeden Kontakt zum Stack:

* **Key-Pruefung** (:mod:`acoustid_watchdog.auth`) — der ``apikey``-Modus,
  aus der Tabelle ``api_key`` der Zustandsdatenbank.
* **IP-Rate-Limit** (:mod:`acoustid_watchdog.ratelimit`) — ein gleitendes
  Minutenfenster im Speicher, aktiv in beiden Auth-Modi.

Seit Phase 17 liegt neben der Zustandsdatenbank die **Cache-Datei**
(:mod:`acoustid_watchdog.cache`) — bewusst eine eigene Ablage, damit die
Massenschreibvorgaenge des Lookup-Caches den Zustand nicht belasten
(DECISIONS 2026-08-01, Phase 14). Auch sie liegt auf dem Cache-Pool und ist
damit bei schlafendem Stack da; genau deshalb kann eine Anfrage aus ihr
beantwortet werden, ohne irgendetwas zu wecken.

Seit Phase 16 kommt der Rest des Lebenszyklus dazu
(:mod:`acoustid_watchdog.lifecycle`): die Uhr der letzten Anfrage, die
Auskunft ueber laufende Jobs und die beiden Dauerlaeufer — Idle-Stopp und
Zustandsabgleich. Gestartet und beendet werden die beiden im Lifespan der
Anwendung (:mod:`acoustid_watchdog.main`); hier entstehen sie nur, wie
alles Langlebige.

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
from acoustid_watchdog.auth import ApiKeyAuthenticator
from acoustid_watchdog.cache import LookupCache
from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.control import ProcessControlError, ProcessGroupController
from acoustid_watchdog.events import EventLevel, log_event
from acoustid_watchdog.lifecycle import (
    ActivityTracker,
    DatabaseJobs,
    IdleStopper,
    StatePoller,
)
from acoustid_watchdog.process import SupervisorClient
from acoustid_watchdog.proxy import ReverseProxy
from acoustid_watchdog.ratelimit import IpRateLimiter
from acoustid_watchdog.reload import ReloadMarker
from acoustid_watchdog.stack import (
    ServiceGroupController,
    check_postgres_version,
    default_gates,
)
from acoustid_watchdog.state import StackStateTracker, StackStatus
from acoustid_watchdog.store import Database
from acoustid_watchdog.wake import (
    ReadinessProbe,
    WakeCoordinator,
)
from shared.config import Config
from shared.env import EnvSettings
from shared.models import StackState

__all__ = [
    "CACHE_EVENT_SOURCE",
    "EVENT_SOURCE",
    "STACK_EVENT_SOURCE",
    "WAKE_EVENT_SOURCE",
    "WatchdogService",
]

_LOG = logging.getLogger(__name__)

#: Quelle aller Ereignisse, die der Waechter-Kern selbst schreibt. Spaetere
#: Teile setzen eigene Quellen (``scheduler``, ``backup``), damit die
#: Logansicht danach filtern kann (Phase 27).
EVENT_SOURCE = "watchdog"

#: Quelle der Weck-Ereignisse (Phase 15).
WAKE_EVENT_SOURCE = "wake"

#: Quelle der Zustandswechsel (Phase 16). Eigene Quelle, weil diese
#: Ereignisse die Zustandsmaschine erzaehlen — wer den Lebenszyklus
#: nachvollziehen will, filtert genau danach.
STACK_EVENT_SOURCE = "stack"

#: Quelle der Cache-Ereignisse (Phase 17). Geloggt wird nur die
#: **Invalidierung** — Treffer und Fehlschlaege sind Zaehler (Phase 22), kein
#: Ereignis: sie kaemen im Sekundentakt und wuerden den Ringpuffer von 5000
#: Eintraegen an einem Nachmittag leeren.
CACHE_EVENT_SOURCE = "cache"


class WatchdogService:
    """Haelt die langlebigen Ressourcen eines Waechter-Prozesses."""

    def __init__(
        self,
        settings: EnvSettings,
        db: Database,
        config_store: ConfigStore,
        state: StackStateTracker | None = None,
        *,
        supervisor: SupervisorClient | None = None,
        stack: ProcessGroupController | None = None,
        probe: ReadinessProbe | None = None,
        proxy: ReverseProxy | None = None,
        cache: LookupCache | None = None,
    ) -> None:
        """
        Args:
            settings: Bootstrap-Werte des Prozesses.
            db: Zustandsdatenbank (Cache-Pool).
            config_store: Laufzeit-Konfiguration.
            state: Stack-Zustand; ohne Angabe ``schlafend``.
            supervisor: Steuerweg zu supervisord. Ohne Angabe entsteht ein
                Client auf den fest verdrahteten Socket-Pfad; Tests geben
                eine eigene Fassung mit.
            stack: Fertige Prozessgruppen-Steuerung. Ohne Angabe entsteht
                die Betriebsfassung (:class:`ServiceGroupController` mit
                den Gates aus den Bootstrap-Werten).
            probe: Bereitschaftsfrage an den API-Healthcheck.
            proxy: Reverse-Proxy auf den API-Dienst.
            cache: Lookup-Cache; ohne Angabe die Vorgabedatei im
                Datenverzeichnis.
        """
        self.settings = settings
        self.db = db
        self.config_store = config_store
        self.cache = cache if cache is not None else LookupCache.for_data_dir(settings.data_dir)
        self.state = state if state is not None else StackStateTracker.sleeping()
        # Jeder Zustandswechsel wird zum Ereignis — auch der, den kein
        # Weckvorgang ausgeloest hat (Poller, Idle-Stopp). Der Anschluss
        # sitzt hier und nicht im Tracker, weil nur der Dienst die
        # Zustandsdatenbank kennt.
        self.state.on_transition = self._log_transition

        self.supervisor = supervisor if supervisor is not None else SupervisorClient()
        # Beide Adressen kommen aus den Bootstrap-Werten (``AOFF_API_*``)
        # und nicht mehr aus Modulkonstanten: im Ein-Container-Betrieb ist
        # der API-Dienst nicht mehr ``acoustid-api:8080``, sondern ein
        # Loopback-Port — das ist ein Umgebungswert, keine Codekonstante.
        self.probe = probe if probe is not None else ReadinessProbe(settings.api_health_url)
        self.proxy = proxy if proxy is not None else ReverseProxy(settings.api_base_url)
        # Die beiden Waechter am Eingang (Phase 18). Beide leben im
        # Speicher bzw. auf der Zustandsdatenbank und beruehren den Stack
        # nie — deshalb duerfen sie vor dem Cache stehen (Invariante §8.2).
        self.auth = ApiKeyAuthenticator(self.db)
        self.ratelimit = IpRateLimiter()

        # Der einzige Ort, an dem die konkrete Steuerung gewaehlt wird —
        # gehalten wird sie nur ueber ihr Protokoll. Der Ein-Container-Umbau
        # war damit genau diese eine Zeile (HANDOFF v2, M1b).
        self.stack: ProcessGroupController = (
            stack
            if stack is not None
            else ServiceGroupController(
                self.supervisor,
                gates=default_gates(settings),
                version_guard=self._guard_postgres_version,
            )
        )
        self.wake = WakeCoordinator(
            self.stack,
            self.probe,
            self.state,
            # Weck-Ereignisse bekommen eine eigene Quelle, damit die
            # Logansicht (Phase 27) danach filtern kann.
            log_event=partial(self.log_event, source=WAKE_EVENT_SOURCE),
        )

        # Lebenszyklus (Phase 16): Uhr, Job-Auskunft und die beiden
        # Dauerlaeufer. Die Aufgaben selbst startet der Lifespan.
        self.activity = ActivityTracker()
        self.jobs = DatabaseJobs(self.db)
        self.idle = IdleStopper(
            self.wake,
            self.state,
            self.activity,
            self.jobs,
            lambda: self.config,
        )
        self.poller = StatePoller(self.wake)

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
        # Der Cache scheitert nie nach aussen: eine unbrauchbare Datei wird
        # weggeworfen, ein weiterhin unbrauchbarer Cache schaltet sich still
        # ab (:mod:`acoustid_watchdog.cache`).
        self.cache.open()
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
                "cache_path": str(self.cache.path),
                "cache_entries": self.cache.entries,
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
        # Der Versions-Drift-Guard (E14) meldet sich beim Start **einmal**
        # laut — und danach bei jedem Weckversuch (`version_guard` der
        # Steuerung). Der Waechter laeuft trotzdem weiter: nur so sieht der
        # Betreiber den Befund ueberhaupt (`/status`, Admin-UI, Ereignis-Log).
        self._report_postgres_version_drift()

        # Der Stack-Zustand liegt nur im Speicher (DECISIONS 2026-08-01,
        # Punkt 6) und wird deshalb bei jedem Start neu aus der Steuerung
        # erhoben — der Betreiber kann den Stack zwischenzeitlich von Hand
        # gestartet haben. Schlaegt das fehl, laeuft der Waechter trotzdem
        # weiter.
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
        """Gibt Zustandsdatenbank, Cache, Supervisor-Client und Probe frei.

        Der Proxy haelt einen **asynchronen** Pool und wird deshalb in
        :meth:`aclose` geschlossen — das ist der Weg, den der Lifespan der
        Anwendung nimmt.
        """
        self.db.close()
        self.cache.close()
        self.supervisor.close()
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

    def invalidate_cache(self, reason: str) -> int:
        """Leert den Lookup-Cache vollstaendig (Invariante §8.6).

        **Der** Weg, auf dem der Cache geleert wird — es gibt keinen
        zweiten. Drei Abnehmer teilen ihn sich:

        ==================  ==================================================
        ``submission``      Der Proxy-Pfad nach einer erfolgreichen lokalen
                            Submission (:mod:`acoustid_watchdog.main`).
        ``delta_import``    Der Update-Zyklus nach einem erfolgreichen
                            Delta-Import (Phase 19).
        ``manual``          „Cache jetzt leeren" aus `/admin/config`
                            (Phase 25).
        ==================  ==================================================

        Geleert wird **unabhaengig von ``cache.enabled``**: sonst haette ein
        zwischenzeitlich abgeschalteter Cache nach dem Wiedereinschalten
        Antworten von vor dem Import.

        Args:
            reason: Kurzname des Ausloesers (siehe Tabelle) — er steht im
                Ereignis und macht im Log nachvollziehbar, warum der Cache
                leer ist.

        Returns:
            Zahl der entfernten Eintraege.
        """
        removed = self.cache.invalidate_all()
        if removed:
            # Nur bei tatsaechlich entfernten Eintraegen: eine Instanz mit
            # regem Submit-Verkehr wuerde den Ringpuffer sonst mit
            # Leermeldungen eines schon leeren Caches fuellen.
            self.log_event(
                EventLevel.INFO,
                "Lookup-Cache geleert",
                {"reason": reason, "removed": removed},
                source=CACHE_EVENT_SOURCE,
            )
        return removed

    # --- Versions-Drift der Datenbank (E14) ---------------------------------

    def _guard_postgres_version(self) -> None:
        """Verweigert den Start, wenn der Bestand zu einer anderen Major gehoert.

        Genau eine Major-Version steckt im Image (E14). Ein Postgres 18
        startet auf einem 17er-Datenverzeichnis gar nicht erst — die
        Fehlermeldung stuende aber nur im Prozesslog, und der Stack ginge
        wortlos in ``fehler``. Deshalb wird hier **vor** dem ersten
        ``startProcess`` geprueft: der Weckvorgang scheitert mit einem Satz,
        den der Betreiber lesen kann.

        Raises:
            ProcessControlError: Es liegt ein Bestand einer anderen Major
                vor.
        """
        drift = check_postgres_version(self.settings.db_data_root, self.settings.pg_major)
        if drift is None:
            return
        raise ProcessControlError(str(drift))

    def _report_postgres_version_drift(self) -> None:
        """Schreibt einen Drift-Befund beim Start ins Log und ins Ereignis-Log.

        Ohne diese Meldung merkte der Betreiber den Drift erst beim ersten
        Weckversuch — also moeglicherweise Stunden nach dem Update, und nur
        als 503. Eine Notification wird daraus in M2.5 (E14).
        """
        drift = check_postgres_version(self.settings.db_data_root, self.settings.pg_major)
        if drift is None:
            return
        _LOG.error(
            "Versions-Drift der Datenbank",
            extra={
                "expected_major": drift.expected,
                "found_majors": list(drift.found),
                "db_data_root": str(self.settings.db_data_root),
            },
        )
        self.log_event(
            EventLevel.ERROR,
            "Versions-Drift der Datenbank — Start verweigert",
            {
                "expected_major": drift.expected,
                "found_majors": list(drift.found),
                "detail": str(drift),
            },
            source=STACK_EVENT_SOURCE,
        )

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

    def _log_transition(self, previous: StackStatus, current: StackStatus) -> None:
        """Schreibt jeden Zustandswechsel ins Ereignis-Log (Phase 16).

        Die Mitschrift des Lebenszyklus: aus ihr laesst sich hinterher
        lesen, wann der Stack wach war und warum er es wurde — auch dann,
        wenn niemand zugesehen hat. Der Wortlaut ist der der Admin-UI
        (ARCHITECTURE §9), damit Logansicht und Statuskarte dieselbe
        Sprache sprechen.
        """
        level = EventLevel.ERROR if current.state is StackState.ERROR else EventLevel.INFO
        self.log_event(
            level,
            f"Stack-Zustand: {current.state.display_name}",
            {
                "state": current.state.value,
                "state_previous": previous.state.value,
                "detail": current.detail,
            },
            source=STACK_EVENT_SOURCE,
        )
