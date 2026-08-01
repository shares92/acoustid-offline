"""Waechter von acoustid-offline (Import-Name ``acoustid_watchdog``).

Der Dauerlaeufer auf dem SSD-Cache-Pool — der einzige Dienst, der immer an
ist, und der einzige mit einem veroeffentlichten Port (ARCHITECTURE §3).

Stand Phase 14 steht das Grundgeruest: Zustandshaltung auf dem Cache-Pool
und ein Statusendpunkt, der nichts weckt.

* :mod:`acoustid_watchdog.main` — HTTP-Schicht (FastAPI): ``GET /status``.
* :mod:`acoustid_watchdog.service` — Zustandsdatenbank, Konfiguration,
  Stack-Zustand; zugleich der Erststart-Pfad.
* :mod:`acoustid_watchdog.store` — SQLite auf dem Cache-Pool: Schema,
  Migrationen, Transaktionen.
* :mod:`acoustid_watchdog.events` — ``event_log`` mit Ringpuffer-Grenze.
* :mod:`acoustid_watchdog.runs` — ``update_run``: Historie der Import- und
  Backup-Laeufe, Quelle des Datenstands.
* :mod:`acoustid_watchdog.admin` — ``admin_user``, argon2 und das beim
  Erststart erzeugte Passwort.
* :mod:`acoustid_watchdog.config_store` — die ``config.yaml``, deren
  einziger Schreiber der Waechter ist.
* :mod:`acoustid_watchdog.reload` — Reload-Signal Richtung API-Dienst.
* :mod:`acoustid_watchdog.state` — die fuenf Stack-Zustaende.
* :mod:`acoustid_watchdog.status` — Antwort von ``/status``.

Es fehlen noch: Proxy mit Weck-Logik und Docker-Steuerung (Phase 15),
Zustandsmaschine und Idle-Stopp (16), Lookup-Cache (17), Auth und
Rate-Limit (18), Scheduler (19), Benachrichtigungen (20), Backup (21),
Metrics (22) und die Admin-UI (23-27).
"""

__version__ = "0.0.1"
