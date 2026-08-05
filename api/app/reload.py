"""Empfangsseite des Reload-Signals (ARCHITECTURE §5, Phase 14/15).

Der Waechter ist der einzige Schreiber der ``config.yaml``; api und
importer mounten sie read-only unter ``/watchdog`` (``docker-compose.yml``).
Nach jedem Schreiben legt er daneben eine Markierungsdatei
``config.yaml.reload`` ab — JSON mit einem **monoton wachsenden Zaehler**
``generation`` (Sendeseite: ``watchdog/app/reload.py``, Entscheid
2026-08-01, Punkt 5).

Dieses Modul ist die Gegenseite: es merkt sich die zuletzt gesehene
Generation, schaut regelmaessig nach und laedt die ``config.yaml`` neu,
wenn die Nummer gestiegen ist. Kein Datei-Watcher, kein Signal, kein
offener Port — die Marke laeuft ueber denselben read-only-Mount, den es
ohnehin gibt, und funktioniert auch, wenn der Empfaenger beim Setzen des
Signals gerade geschlafen hat: er sieht beim Aufwachen die neue Nummer.

**Was ein Reload wirklich aendert.** Konservativ genau das, was der Dienst
zur Anfragezeit aus der Konfiguration liest:

============================  =============================================
``submit.mode``               sofort — inklusive Neubau des
``submit.upstream_app_key``   Upstream-Weiterleiters (er entsteht sonst nur
                              beim Start und waere im Modus-Wechsel
                              ``local`` -> ``local+upstream`` gar nicht da)
``mb.keep_submitted_mbid``    sofort (wird je Anfrage gelesen)
``index.query_hashes``        **nicht** — eine Aenderung verlangt einen
                              Index-Neuaufbau (§6); der laufende Wert
                              bleibt, damit Suche und Bestand
                              zusammenpassen
``mb.dsn``                    **nicht** — der Pool und sein Schema-
                              Selfcheck entstehen beim Start; ein
                              Austausch im laufenden Betrieb waere ein
                              zweiter Verbindungslebenszyklus ohne Abnehmer
============================  =============================================

Die beiden „nicht"-Faelle werden nicht etwa still ignoriert: der laufende
Wert wird in die uebernommene Konfiguration **zurueckgeschrieben** und die
Abweichung als Warnung protokolliert. So beschreibt ``service.config``
immer, was der Prozess tatsaechlich tut — eine halb uebernommene
Konfiguration waere die schlechtere Luege. Alles Uebrige aus der
``config.yaml`` (auth, wake, idle, update, cache, ratelimit, metrics,
notify, backup) liest die API gar nicht; es gehoert dem Waechter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final

from acoustid_api.upstream import UpstreamForwarder
from shared.config import Config, ConfigError, load_config

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from acoustid_api.service import ApiService

__all__ = [
    "DEFAULT_INTERVAL_S",
    "RELOAD_SUFFIX",
    "ConfigReloader",
    "read_generation",
]

_LOG = logging.getLogger(__name__)

#: Dateiendung der Marke, gebildet aus dem Namen der Konfiguration
#: (``config.yaml`` -> ``config.yaml.reload``). Der Wert ist Vertrag
#: zwischen zwei getrennten Images und steht deshalb hier noch einmal —
#: der Waechter ist kein Import des API-Dienstes.
RELOAD_SUFFIX: Final = ".reload"

#: Abstand zweier Blicke auf die Marke. Ein ``stat`` je zehn Sekunden auf
#: eine Datei im Cache-Pool kostet nichts; schneller muss eine
#: Konfigurationsaenderung nicht ankommen (die Admin-UI sagt dem Betreiber
#: ohnehin, dass sie „beim naechsten Zyklus" greift).
DEFAULT_INTERVAL_S: Final = 10.0


def read_generation(marker_path: Path) -> int | None:
    """Liest die Generation aus der Marke.

    Returns:
        Die Nummer, oder ``None``, wenn es die Marke nicht gibt bzw. sie
        unbrauchbar ist. Eine kaputte Marke ist kein Fehler: das Signal ist
        ein Hinweis, kein Datenspeicher (Sendeseite ebenso).
    """
    try:
        raw = marker_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _LOG.warning(
            "Reload-Marke nicht lesbar",
            extra={"reload_path": str(marker_path), "error": str(exc)},
        )
        return None
    try:
        return int(json.loads(raw)["generation"])
    except (ValueError, TypeError, KeyError) as exc:
        _LOG.warning(
            "Reload-Marke unbrauchbar",
            extra={"reload_path": str(marker_path), "error": str(exc)},
        )
        return None


class ConfigReloader:
    """Beobachtet die Marke und uebernimmt geaenderte Konfiguration."""

    def __init__(
        self,
        service: ApiService,
        config_path: str | Path,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """
        Args:
            service: Laufzeitumgebung, deren ``config`` ersetzt wird.
            config_path: Pfad der ``config.yaml`` (``MMO_CONFIG_PATH``).
            interval_s: Abstand zweier Blicke auf die Marke.
        """
        self.service = service
        self.config_path = Path(config_path)
        self.marker_path = self.config_path.with_name(self.config_path.name + RELOAD_SUFFIX)
        self.interval_s = interval_s
        #: Zuletzt gesehene Generation; ``None`` = noch nie signalisiert.
        self.generation: int | None = None
        #: Wie oft neu geladen wurde (Diagnose und Tests).
        self.reloads = 0

    # --- Einzelschritt ------------------------------------------------------

    def prime(self) -> int | None:
        """Merkt sich den Stand beim Start, ohne neu zu laden.

        Die Konfiguration ist beim Start gerade frisch gelesen worden
        (``ApiService.from_env``); es gibt nichts zu uebernehmen. Ohne
        diesen Schritt wuerde der erste Blick jede vorhandene Marke fuer
        neu halten und einen ueberfluessigen Reload ausloesen.
        """
        self.generation = read_generation(self.marker_path)
        _LOG.info(
            "Reload-Signal wird beobachtet",
            extra={"reload_path": str(self.marker_path), "reload_generation": self.generation},
        )
        return self.generation

    def check(self) -> bool:
        """Ein Blick auf die Marke; laedt bei neuer Generation neu.

        Returns:
            ``True``, wenn diese Runde eine neue Konfiguration uebernommen
            hat.
        """
        generation = read_generation(self.marker_path)
        if generation is None or generation == self.generation:
            return False
        # Auch eine *kleinere* Nummer gilt als Aenderung: die Marke kann
        # geloescht und neu begonnen worden sein (dann faengt der Waechter
        # wieder bei 1 an). Neu laden ist in dem Fall der sichere Ausgang.
        previous = self.generation
        self.generation = generation
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            # Die alte Konfiguration bleibt in Kraft — sie ist gueltig, die
            # neue nicht. Der Betreiber sieht den Grund im Log und in der
            # Admin-UI (die schreibt nur validierte Dateien; eine kaputte
            # kann nur von Hand entstehen).
            _LOG.error(
                "Neue Konfiguration ist ungueltig, alte bleibt in Kraft",
                extra={"config_path": str(self.config_path), "error": str(exc)},
            )
            return False

        self._apply(config)
        self.reloads += 1
        _LOG.info(
            "Konfiguration neu geladen",
            extra={
                "config_path": str(self.config_path),
                "reload_generation": generation,
                "reload_generation_previous": previous,
                "submit_mode": self.service.config.submit.mode.value,
            },
        )
        return True

    # --- Schleife -----------------------------------------------------------

    async def run(self) -> None:
        """Fragt die Marke bis zum Abbruch periodisch ab.

        Laeuft als Hintergrundaufgabe im Lifespan des Dienstes
        (:mod:`acoustid_api.main`). Der Dateizugriff ist kurz und lokal;
        ein eigener Thread lohnt dafuer nicht.
        """
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                self.check()
            except Exception:
                # Eine Hintergrundschleife darf an nichts sterben — sonst
                # kaeme jede spaetere Aenderung nie mehr an.
                _LOG.exception("Reload-Pruefung fehlgeschlagen")

    # --- Uebernahme ---------------------------------------------------------

    def _apply(self, config: Config) -> None:
        """Uebernimmt die neue Konfiguration, so weit sie wirken darf."""
        running = self.service.config
        config = self._keep_startup_values(config, running)

        upstream_changed = (
            config.submit.mode is not running.submit.mode
            or config.submit.upstream_app_key.get_secret_value()
            != running.submit.upstream_app_key.get_secret_value()
        )
        self.service.config = config
        if upstream_changed:
            self._rebuild_upstream(config)

    def _keep_startup_values(self, config: Config, running: Config) -> Config:
        """Setzt die Werte zurueck, die nur ein Neustart aendern kann."""
        overrides: dict[str, object] = {}
        if config.index.query_hashes != running.index.query_hashes:
            _LOG.warning(
                "index.query_hashes wirkt erst nach Index-Neuaufbau und Neustart",
                extra={
                    "query_hashes_running": running.index.query_hashes,
                    "query_hashes_file": config.index.query_hashes,
                },
            )
            overrides["index"] = running.index
        if config.mb.dsn.get_secret_value() != running.mb.dsn.get_secret_value():
            _LOG.warning(
                "mb.dsn wirkt erst nach einem Neustart des API-Dienstes",
                extra={"mb_configured_file": config.mb.configured},
            )
            overrides["mb"] = config.mb.model_copy(update={"dsn": running.mb.dsn})
        return config.model_copy(update=overrides) if overrides else config

    def _rebuild_upstream(self, config: Config) -> None:
        """Baut den Upstream-Weiterleiter neu — der einzige Neubau hier.

        Er ist an ``submit.mode`` und ``submit.upstream_app_key`` gebunden
        und existiert ausserhalb von ``local+upstream`` gar nicht
        (:meth:`acoustid_api.upstream.UpstreamForwarder.from_config`). Ohne
        Neubau bliebe ein Umschalten auf ``local+upstream`` wirkungslos.
        Der alte Weiterleiter wird geschlossen; die Drossel des neuen
        beginnt bei null, was nach einem Moduswechsel richtig ist.
        """
        previous = self.service.upstream
        self.service.upstream = UpstreamForwarder.from_config(config)
        if previous is not None:
            previous.close()
        _LOG.info(
            "Upstream-Weiterleiter neu gebaut",
            extra={"submit_mode": config.submit.mode.value},
        )
