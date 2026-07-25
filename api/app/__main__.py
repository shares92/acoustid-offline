"""Start des API-Dienstes: ``python -m acoustid_api``.

Der Dienst laeuft im Compose-Netz **ohne** veroeffentlichten Port; davor
sitzt der Waechter als Proxy (ARCHITECTURE §3). Der Port hier ist deshalb
fest der containerinterne 8080 und bewusst nicht der ``AOFF_PORT`` des
Waechters — der gehoert dem Proxy.

Zugaenge und Log-Level kommen wie ueberall aus den ``AOFF_``-Variablen
(``shared.env``), die Laufzeit-Konfiguration aus der ``config.yaml``.
"""

from __future__ import annotations

from typing import Final

import uvicorn

from shared.env import EnvSettings

#: Containerinterner Port des API-Dienstes.
PORT: Final = 8080


def main() -> None:
    """Startet uvicorn mit der Anwendungsfabrik."""
    settings = EnvSettings.from_env()
    uvicorn.run(
        "acoustid_api.main:build_app",
        factory=True,
        # An alle Schnittstellen des Containers — veroeffentlicht wird
        # trotzdem keine: der Compose-Dienst hat kein `ports:`.
        host="0.0.0.0",
        port=PORT,
        log_level=settings.log_level.lower(),
        access_log=False,  # der Waechter protokolliert die Zugriffe
    )


if __name__ == "__main__":
    main()
