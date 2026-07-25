"""Strukturiertes Logging fuer alle drei Services.

Eine Zeile JSON je Ereignis auf **stderr** — maschinenlesbar fuer
`docker logs`, Unraid und spaetere Auswertung. Das Feldschema ist zugleich
die Grundlage des `event_log` aus Phase 14 (dieselben Kernfelder plus
`extra`); implementiert wird das event_log dort, nicht hier.

Beispielzeile, hier zur Lesbarkeit umgebrochen — im Log steht sie auf einer
Zeile:

|   {"ts": "2026-07-25T02:00:00.123Z", "level": "INFO",
|    "service": "acoustid-watchdog", "logger": "shared.config",
|    "msg": "Konfiguration geschrieben",
|    "extra": {"config_path": "/data/config.yaml"}}

Extra-Felder kommen wie ueblich per ``logger.info(msg, extra={...})`` und
landen gebuendelt unter ``extra`` — so kann kein Anwendungsfeld ein
Kernfeld ueberschreiben. Nicht JSON-taugliche Werte werden ueber ``str()``
serialisiert; ``SecretStr`` bleibt dadurch maskiert (`**********`).

**Falle:** Die Namen der LogRecord-Standardattribute sind fuer ``extra``
gesperrt — ``created``, ``name``, ``module``, ``filename``, ``lineno``,
``process``, ``message`` und Verwandte lassen das stdlib-``logging``
sofort mit ``KeyError`` abbrechen. Im Zweifel ein sprechendes Praefix
verwenden (``file_created`` statt ``created``).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

__all__ = [
    "HANDLER_NAME",
    "JsonLogFormatter",
    "setup_logging",
]

#: Name des von `setup_logging` installierten Handlers. Nur dieser wird bei
#: einem erneuten Aufruf ersetzt — fremde Handler (pytest, uvicorn) bleiben.
HANDLER_NAME = "acoustid-offline"

# Standardattribute eines LogRecord; alles andere ist ein Extra-Feld.
_RESERVED_RECORD_FIELDS = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _json_default(value: Any) -> str:
    """Fallback fuer nicht JSON-taugliche Werte.

    Bewusst ``str()``: Secrets (`pydantic.SecretStr`) maskieren sich damit
    selbst, Pfade und Zeitstempel werden lesbar.
    """
    return str(value)


class JsonLogFormatter(logging.Formatter):
    """Formatiert LogRecords als eine JSON-Zeile."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_FIELDS
        }
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=_json_default)


def setup_logging(
    service_name: str,
    level: int | str = "INFO",
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Richtet das strukturierte Logging ein und liefert den Service-Logger.

    Idempotent: ein zweiter Aufruf ersetzt nur den eigenen Handler, fremde
    Handler (z. B. von pytest oder uvicorn) bleiben unangetastet.

    Args:
        service_name: erscheint als `service` in jeder Zeile, z. B.
            `acoustid-watchdog`.
        level: Level-Name (`"INFO"`) oder -Zahl.
        stream: Ausgabeziel; Default `sys.stderr`.

    Raises:
        ValueError: unbekannter Level-Name.
    """
    resolved = _resolve_level(level)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(JsonLogFormatter(service_name))
    handler.setLevel(resolved)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == HANDLER_NAME:
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    root.setLevel(resolved)

    return logging.getLogger(service_name)


def _resolve_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(level.upper())
    if resolved is None:
        known = ", ".join(sorted(logging.getLevelNamesMapping()))
        raise ValueError(f"unbekannter Log-Level {level!r}; erlaubt sind: {known}")
    return resolved
