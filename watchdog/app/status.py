"""Antwort von ``GET /status`` (ARCHITECTURE §7 „Eigene Endpoints").

Der Vertrag lautet: *„Waechter-Endpoint, **weckt nie**: Stack-Zustand
(schlafend/startend/bereit/Fehler), Datenstand (letzte Delta-Sequenz),
letzter Update-Lauf, Version."*

**„Weckt nie" ist die eigentliche Anforderung** (Invariante §8.2), und sie
ist hier baulich erfuellt, nicht nur zugesagt: dieses Modul bekommt genau
zwei Dinge zu sehen — die Zustandsdatenbank des Waechters und seine
Laufzeit-Konfiguration. Beide liegen auf dem Cache-Pool. Es gibt in diesem
Aufrufpfad keine Verbindung zur AcoustID-Postgres, keine zum Suchindex,
keinen HTTP-Aufruf an den API-Dienst und keinen Docker-Aufruf; auch der
Datenstand kommt aus der Mitschrift in ``update_run`` und nicht aus
``import_state`` auf dem Array (:mod:`acoustid_watchdog.runs`).

Der Endpunkt ist bewusst **offen** — keine Anmeldung, kein API-Key. Er ist
die Bereitschaftsanzeige der Instanz und verraet nichts, was nicht ohnehin
jede Lookup-Antwort zeigt: keine Schluessel, keine Pfade, keine
Konfigurationswerte mit Secret-Charakter.

Antwortform (JSON)::

    {
      "status": "ok",
      "version": "0.0.1",
      "stack":  {"state": "sleeping", "state_display": "schlafend",
                 "since": "...", "detail": null},
      "data":   {"last_sequence": "2026-07-22", "updated_at": "...",
                 "run_id": 41},
      "last_update_run": {…} | null,
      "components": {"postgresql_major": 18,
                     "acoustid_index_commit": "6bc929a…" | null}
    }

``data`` ist ``null``, solange nie ein Delta eingespielt wurde (frische
Instanz vor dem Bootstrap) — ein leeres Objekt waere von „Stand unbekannt"
nicht zu unterscheiden.

**Erweiterungen sind ausschliesslich additiv** (M0-Analyse §2.1): das Feld
``stack`` behaelt seinen Namen und seine Form, auch wenn im Ein-Container-
Betrieb kein Stack mehr im Wortsinn existiert. Es ist ein bestehender
Vertrag — der Container-Healthcheck und jeder Skriptaufruf des Betreibers
haengen daran, und eine Umbenennung braechte niemandem etwas.

``components`` (neu in M2, v2 §12) beantwortet die Frage, die man sonst nur
per ``docker image inspect`` beantworten kann: **welche Fremdkomponenten
stecken in genau diesem laufenden Artefakt?** Das ist keine Kosmetik — der
Versions-Drift-Guard (E14) verweigert den Start auf einem Datenverzeichnis
der falschen Major, und wer das debuggt, muss die eingebackene Major sehen
koennen, ohne an das Image heranzukommen. Die Werte kommen aus der
Bootstrap-Umgebung, die das Dockerfile setzt; ausserhalb des Images sind
sie leer und werden zu ``null``.
"""

from __future__ import annotations

from typing import Any

from acoustid_watchdog import __version__
from acoustid_watchdog.runs import RunKind, latest_data_sequence, latest_run
from acoustid_watchdog.state import StackStateTracker
from acoustid_watchdog.store import Database
from shared.env import EnvSettings

__all__ = ["build_status"]


def build_status(db: Database, state: StackStateTracker, settings: EnvSettings) -> dict[str, Any]:
    """Baut die Statusantwort ausschliesslich aus Waechter-Daten.

    Args:
        db: Zustandsdatenbank des Waechters (Cache-Pool).
        state: Aktueller Stack-Zustand.
        settings: Bootstrap-Werte; liefern die eingebackenen Versionen.
            Sie stehen schon im Speicher — die Zusage „weckt nie" bleibt
            damit baulich erfuellt, es kommt kein Zugriff hinzu.
    """
    last_run = latest_run(db, RunKind.UPDATE)
    data_run = latest_data_sequence(db)
    return {
        "status": "ok",
        "version": __version__,
        "stack": state.status.as_dict(),
        "data": _data_state(data_run),
        "last_update_run": last_run.as_dict() if last_run else None,
        "components": _components(settings),
    }


def _components(settings: EnvSettings) -> dict[str, Any]:
    """Die eingebackenen Fremdkomponenten dieses Artefakts (v2 §12)."""
    return {
        "postgresql_major": settings.pg_major,
        # Leer heisst „kein eingebackenes Binary" (Lauf ausserhalb des
        # Images) — und das ist etwas anderes als ein leerer Commit-Name.
        "acoustid_index_commit": settings.index_commit or None,
    }


def _data_state(data_run: Any) -> dict[str, Any] | None:
    """Datenstand: letzte eingespielte Delta-Sequenz und wann sie kam."""
    if data_run is None:
        return None
    return {
        "last_sequence": data_run.last_sequence,
        # Ende des Laufs, der diesen Stand gebracht hat. `finished_at` ist
        # bei einem abgeschlossenen Lauf immer gesetzt; der Rueckfall auf
        # `started_at` deckt nur den theoretischen Fall einer von Hand
        # nachgetragenen Zeile ab.
        "updated_at": data_run.finished_at or data_run.started_at,
        "run_id": data_run.id,
    }
