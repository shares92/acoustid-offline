"""Die ``config.yaml`` aus Sicht des Waechters (ARCHITECTURE §5, §6).

Der Waechter ist der **einzige Schreiber** der Laufzeit-Konfiguration; API
und Importer sehen dieselbe Datei auf dem ``/config``-Mount (seit M1b ein
Container; lesbar fuer sie ueber Gruppe ``musicmeta``, Modus 0640).
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
from shared.config import Config, LegacyKey, load_config, read_legacy_keys, save_config

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
        legacy = read_legacy_keys(self.path)
        config = load_config(self.path, create_if_missing=True)
        if legacy:
            self._rewrite_legacy_file(config, legacy)
        _LOG.info(
            "Laufzeit-Konfiguration geladen",
            extra={
                "config_path": str(self.path),
                # Nur Modi und Schalter, nie Werte mit Secret-Charakter
                # (ARCHITECTURE §6).
                "auth_mode": config.auth.mode.value,
                "submit_mode": config.acoustid.submit.mode.value,
                "cache_enabled": config.cache.enabled,
                "mb_configured": config.mb.configured,
            },
        )
        return config

    def _rewrite_legacy_file(self, config: Config, legacy: list[LegacyKey]) -> None:
        """Einmaliger Umschreiber auf das M2-Schema (E9).

        Das Uebergangslesen allein genuegt nicht: es haelt eine Datei am
        Leben, die bei jedem Start warnt und deren Schluessel nicht mehr zu
        dem passen, was die Admin-UI anzeigt. Schlimmer noch, sie wuerde
        beim naechsten Speichern **stillschweigend** in die neue Form
        kippen — dann waere unklar, ob der Betreiber die Werte gesehen hat.
        Also einmal, sichtbar und beim Start.

        Der Waechter ist dafuer die richtige Stelle: er ist der einzige
        Schreiber der Datei (Modul-Docstring). Ein Fehlschlag ist bewusst
        **kein** Startabbruch — das Uebergangslesen traegt weiter, und ein
        schreibgeschuetztes `/config` darf die Instanz nicht lahmlegen.
        """
        try:
            save_config(config, self.path)
        except OSError as exc:
            _LOG.warning(
                "Konfiguration konnte nicht auf das neue Schema umgeschrieben werden — "
                "die alten Schluessel werden weiter gelesen",
                extra={"config_path": str(self.path), "error": str(exc)},
            )
            return
        _LOG.info(
            "Konfiguration auf das neue Schluesselschema umgeschrieben",
            extra={
                "config_path": str(self.path),
                "config_keys_migrated": [entry.old for entry in legacy],
            },
        )

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
