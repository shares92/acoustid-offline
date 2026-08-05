"""Reload-Signal des Waechters Richtung API-Dienst (ARCHITECTURE §5, §6).

ARCHITECTURE §5 „config.yaml (Waechter, Cache)": *„Vom Waechter
gelesen/geschrieben; der API-Layer erhaelt die relevante Teilmenge beim
Start bzw. per Reload-Signal vom Waechter."* Phase 14 baut die **Sendeseite**
dieses Signals; die Empfangsseite (der API-Dienst liest neu ein) gehoert zu
Phase 15, wo der Waechter den Stack erstmals wirklich steuert. Bis dahin
uebernimmt der Containerneustart beim Wecken diese Rolle.

**Warum eine Markierungsdatei.** Der Waechter hat in Phase 14 noch keinen
Kanal zum API-Container: kein Proxy (Phase 15), keine Docker-Steuerung
(Phase 15), keinen Healthcheck-Endpunkt (Entscheid 2026-08-01: Bau in
Phase 15). Was es schon gibt, ist genau ein gemeinsames Medium — das
Datenverzeichnis des Waechters, das der Stack read-only unter ``/watchdog``
mountet und aus dem er die ``config.yaml`` liest (siehe
``docker-compose.yml``). Eine kleine Marke **neben** der Konfiguration
laeuft ueber denselben Mount, braucht keine offenen Ports und funktioniert
auch dann, wenn der Empfaenger gerade schlaeft: er sieht beim Aufwachen die
neue Nummer.

Die Marke traegt einen **monoton wachsenden Zaehler**, keinen Zeitstempel
als Wahrheit: Uhren springen (Sommerzeit, NTP), Zaehler nicht. Wer die
Nummer zuletzt gesehen hat, weiss ohne Dateiinhalt-Vergleich, ob sich etwas
geaendert hat.

Geschrieben wird atomar (Temp-Datei + ``os.replace``), wie bei der
``config.yaml`` selbst: ein Leser sieht entweder die alte oder die neue
Marke, nie eine halbe. Die Datei enthaelt **keine** Secrets und bekommt
deshalb normale Leserechte — der API-Container muss sie lesen koennen.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

from acoustid_watchdog.store import utc_now

__all__ = ["RELOAD_SUFFIX", "ReloadMarker", "ReloadSignal"]

_LOG = logging.getLogger(__name__)

#: Die Marke heisst wie die Konfiguration plus dieser Endung
#: (``config.yaml`` -> ``config.yaml.reload``). So liegt sie garantiert im
#: selben Verzeichnis und damit im selben Mount wie die Datei, um die es
#: geht — auch wenn ``MMO_CONFIG_PATH`` woandershin zeigt.
RELOAD_SUFFIX: Final = ".reload"

#: Die Marke ist oeffentlich lesbar; der API-Container laeuft nicht
#: zwingend unter derselben UID wie der Waechter.
_FILE_MODE: Final = 0o644


@dataclass(frozen=True, slots=True)
class ReloadMarker:
    """Inhalt der Markierungsdatei."""

    #: Monoton wachsend, beginnt bei 1. Der eigentliche Vergleichswert.
    generation: int
    #: Wann das Signal gesetzt wurde (ISO-8601, UTC) — nur zur Diagnose.
    ts: str
    #: Warum, in Stichworten (z. B. ``"config_saved"``) — nur zur Diagnose.
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"generation": self.generation, "ts": self.ts, "reason": self.reason}


class ReloadSignal:
    """Sendeseite des Reload-Signals: eine Markierungsdatei.

    Der Zustand liegt vollstaendig in der Datei — der Waechter darf
    neustarten, ohne dass der Zaehler zurueckspringt und ein Empfaenger das
    Signal verpasst.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @classmethod
    def for_config(cls, config_path: str | os.PathLike[str]) -> Self:
        """Die Marke zur angegebenen ``config.yaml``."""
        path = Path(config_path)
        return cls(path.with_name(path.name + RELOAD_SUFFIX))

    def read(self) -> ReloadMarker | None:
        """Aktuelle Marke, oder ``None``, wenn noch nie signalisiert wurde.

        Eine unlesbare oder unvollstaendige Datei gilt ebenfalls als „noch
        nichts": das Signal ist ein Hinweis, kein Datenspeicher, und ein
        kaputter Hinweis darf keinen Start verhindern. Der naechste
        :meth:`emit` faengt wieder bei 1 an — der Empfaenger sieht dann eine
        Aenderung und laedt neu, was der sichere Ausgang ist.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            _LOG.warning(
                "Reload-Marke nicht lesbar",
                extra={"reload_path": str(self.path), "error": str(exc)},
            )
            return None
        try:
            data = json.loads(raw)
            return ReloadMarker(
                generation=int(data["generation"]),
                ts=str(data["ts"]),
                reason=str(data["reason"]),
            )
        except (ValueError, TypeError, KeyError) as exc:
            _LOG.warning(
                "Reload-Marke unbrauchbar, wird beim naechsten Signal ersetzt",
                extra={"reload_path": str(self.path), "error": str(exc)},
            )
            return None

    def emit(self, reason: str) -> ReloadMarker:
        """Zaehlt die Marke hoch und schreibt sie atomar.

        Args:
            reason: Kurzer, maschinenlesbarer Grund (z. B.
                ``"config_saved"``) — steht nur zur Diagnose in der Datei.
        """
        current = self.read()
        marker = ReloadMarker(
            generation=(current.generation + 1) if current else 1,
            ts=utc_now(),
            reason=reason,
        )
        self._write(marker)
        _LOG.info(
            "Reload-Signal gesetzt",
            extra={
                "reload_path": str(self.path),
                "reload_generation": marker.generation,
                "reload_reason": reason,
            },
        )
        return marker

    def _write(self, marker: ReloadMarker) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(marker.as_dict(), ensure_ascii=False) + "\n"

        handle_fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f"{self.path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.chmod(_FILE_MODE)
            tmp_path.replace(self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
