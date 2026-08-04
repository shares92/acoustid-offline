"""Start des Waechters: ``python -m acoustid_watchdog``.

Der Waechter ist der einzige Dienst mit einem veroeffentlichten Port: er
traegt API-Proxy, `/status`, `/metrics` und die Admin-UI unter `/admin` —
alles auf **einem** Port (ARCHITECTURE §6 „Feste Werte"). Anders als beim
API-Dienst ist der Port deshalb konfigurierbar: ``AOFF_PORT``, Default 8080.

Er laeuft als Dauerlaeufer auf dem SSD-Cache-Pool; der Stack darunter darf
schlafen (ARCHITECTURE §3).
"""

from __future__ import annotations

import uvicorn

from acoustid_watchdog.main import SERVICE_NAME, build_app
from shared import setup_logging
from shared.env import EnvSettings

__all__ = ["main"]


def main() -> None:
    """Startet uvicorn mit der Anwendungsfabrik."""
    settings = EnvSettings.from_env()
    # Auch hier schon, damit die Startmeldungen von uvicorn nicht die
    # einzigen Zeilen vor dem Lifespan-Start sind; `create_app` richtet das
    # Logging fuer den Prozess erneut ein (idempotent).
    setup_logging(SERVICE_NAME, settings.log_level)
    uvicorn.run(
        build_app,
        factory=True,
        # An alle Schnittstellen des Containers — der Waechter ist der
        # einzige Dienst, der von aussen erreichbar sein SOLL (§3);
        # veroeffentlicht wird der Port von Compose. Die drei anderen
        # Prozesse binden dagegen ausdruecklich das Loopback.
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Uvicorns Zugriffslog waere die einzige Nicht-JSON-Zeile im Log.
        # Die Zugriffe protokolliert der Proxy selbst, sobald es ihn gibt
        # (Phase 15).
        access_log=False,
    )


if __name__ == "__main__":
    main()
