"""Start des API-Dienstes: ``python -m acoustid_api``.

Der Dienst laeuft **ohne** veroeffentlichten Port; davor sitzt der Waechter
als Proxy (ARCHITECTURE §3, HANDOFF v2 §3).

**Adresse und Port sind seit M1b bindend** und kommen aus den
Bootstrap-Werten: im Ein-Container-Betrieb teilen sich Waechter und API
einen Netzwerk-Namensraum. Ein fester Port 8080 waere der des Waechters —
die Kollision faellt erst beim ersten Wecken auf (`EADDRINUSE`, Risiko R9
der M0-Analyse), also lange nach dem Start. Und ``0.0.0.0`` waere in dieser
Aufstellung eine echte Oeffnung nach aussen: die API kennt weder Auth noch
Rate-Limit, beides setzt der Waechter durch (§7 „Durchsetzungsort Auth &
Rate-Limit"). Deshalb ``127.0.0.1:<MMO_API_PORT>``.

Zugaenge und Log-Level kommen wie ueberall aus den ``MMO_``-Variablen
(``shared.env``), die Laufzeit-Konfiguration aus der ``config.yaml``.
"""

from __future__ import annotations

from typing import Final

import uvicorn

from shared.env import EnvSettings

#: Adresse, an die der Dienst bindet. Fest: eine erreichbare API waere ein
#: Weg an Auth und Rate-Limit vorbei (Modul-Docstring).
HOST: Final = "127.0.0.1"


def main() -> None:
    """Startet uvicorn mit der Anwendungsfabrik."""
    settings = EnvSettings.from_env()
    uvicorn.run(
        "acoustid_api.main:build_app",
        factory=True,
        host=HOST,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=False,  # der Waechter protokolliert die Zugriffe
    )


if __name__ == "__main__":
    main()
