"""Die ``config.yaml`` aus Sicht des Waechters (ARCHITECTURE §5, §6).

Der Waechter ist der **einzige Schreiber** der Laufzeit-Konfiguration; API
und Importer mounten dieselbe Datei read-only (siehe ``docker-compose.yml``).
Schema, Laden und atomares Schreiben liegen in :mod:`shared.config` — hier
kommt nur das dazu, was dieser Schreiber braucht:

* **Eine gemeinsame, im Speicher gehaltene Sicht.** Alle Routen sehen
  dieselbe :class:`~shared.config.Config`; die Datei wird nicht bei jeder
  Anfrage neu geparst.
* **Anlegen beim Erststart.** Fehlt die Datei, entsteht sie mit den
  Defaults aus §6 — sichtbar auf dem Cache-Volume, damit der Betreiber sie
  auch von Hand editieren kann.
* **Reload-Signal nach jedem Schreiben** (:mod:`acoustid_watchdog.reload`),
  damit der API-Dienst die geaenderte Teilmenge uebernimmt.
* **Serialisierte Schreibvorgaenge.** Zwei gleichzeitige Speichervorgaenge
  aus der Admin-UI duerfen sich nicht ueberholen — sonst gewinnt nicht die
  spaetere Aenderung, sondern die langsamere.

Bewusst **kein** Neu-Einlesen bei Dateiaenderung von aussen: wer die Datei
von Hand editiert, startet den Waechter neu. Ein Watcher waere ein
zweiter, konkurrierender Schreibpfad in dieselbe Datei — genau das, was das
Reload-Signal auf der anderen Seite vermeiden soll.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Self

from acoustid_watchdog.reload import ReloadMarker, ReloadSignal
from shared.config import Config, load_config, save_config

__all__ = ["ConfigStore"]

_LOG = logging.getLogger(__name__)


class ConfigStore:
    """Haelt die Laufzeit-Konfiguration und schreibt sie zurueck."""

    def __init__(
        self,
        path: str | Path,
        signal: ReloadSignal | None = None,
        config: Config | None = None,
    ) -> None:
        self.path = Path(path)
        self.signal = signal if signal is not None else ReloadSignal.for_config(self.path)
        self._config = config
        self._lock = threading.Lock()

    @classmethod
    def from_path(cls, path: str | Path) -> Self:
        """Store fuer diesen Pfad; die Datei wird noch nicht gelesen."""
        return cls(path)

    @property
    def config(self) -> Config:
        """Die aktuelle Konfiguration; laedt beim ersten Zugriff nach.

        Raises:
            shared.config.ConfigError: Datei unlesbar oder Werte ungueltig.
        """
        with self._lock:
            if self._config is None:
                self._config = self._load()
            return self._config

    def load(self) -> Config:
        """Liest die Datei (neu) ein und legt sie beim Erststart an.

        Raises:
            shared.config.ConfigError: Datei unlesbar oder Werte ungueltig.
        """
        with self._lock:
            self._config = self._load()
            return self._config

    def _load(self) -> Config:
        config = load_config(self.path, create_if_missing=True)
        _LOG.info(
            "Laufzeit-Konfiguration geladen",
            extra={
                "config_path": str(self.path),
                # Nur Modi und Schalter, nie Werte mit Secret-Charakter
                # (ARCHITECTURE §6).
                "auth_mode": config.auth.mode.value,
                "submit_mode": config.submit.mode.value,
                "cache_enabled": config.cache.enabled,
                "mb_configured": config.mb.configured,
            },
        )
        return config

    def save(self, config: Config, *, reason: str = "config_saved") -> ReloadMarker:
        """Schreibt die Konfiguration und signalisiert den Reload.

        Reihenfolge ist Absicht: erst die Datei, dann die Marke. Wer die
        Marke sieht, findet garantiert schon den neuen Inhalt vor; die
        umgekehrte Reihenfolge liesse einen Empfaenger die alte Datei fuer
        die neue halten.

        Args:
            config: Vollstaendige neue Konfiguration.
            reason: Grund fuer die Reload-Marke (Diagnose).

        Returns:
            Die gesetzte Marke.
        """
        with self._lock:
            save_config(config, self.path)
            self._config = config
            return self.signal.emit(reason)
