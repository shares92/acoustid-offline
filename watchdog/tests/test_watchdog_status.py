"""``GET /status`` — Vertrag und die Zusage „weckt nie" (§7, §8.2, Phase 14)."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from acoustid_watchdog import __version__
from acoustid_watchdog.runs import RunKind, RunResult, finish_run, start_run
from acoustid_watchdog.service import WatchdogService
from shared.models import StackState


def test_status_answers_with_the_promised_parts(client: TestClient) -> None:
    """ARCHITECTURE §7: Stack-Zustand, Datenstand, letzter Update-Lauf, Version.

    Seit M2 zusaetzlich ``components`` (v2 §12) — **additiv**: die vier
    Felder von Phase 14 behalten Namen und Form.
    """
    response = client.get("/status")
    assert response.status_code == 200

    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert set(payload) == {
        "status",
        "version",
        "stack",
        "data",
        "last_update_run",
        "components",
    }


def test_the_stack_field_keeps_its_name(client: TestClient) -> None:
    """E16: `stack` bleibt, obwohl es keinen Stack mehr gibt.

    Der Ein-Container-Umbau haette hier eine Umbenennung nahegelegt — aber
    das Feld ist ein bestehender Vertrag: der Container-Healthcheck und
    jedes Betreiber-Skript lesen es. Eine Umbenennung braechte niemandem
    etwas und braeche alles, was danach greift.
    """
    payload = client.get("/status").json()

    assert "stack" in payload
    assert set(payload["stack"]) == {"state", "state_display", "since", "detail"}


def test_status_reports_the_baked_in_component_versions(
    client: TestClient, service: WatchdogService
) -> None:
    """v2 §12: die eingebackenen Versionen sind in `/status` sichtbar."""
    payload = client.get("/status").json()

    assert payload["components"]["postgresql_major"] == service.settings.pg_major


def test_a_missing_index_commit_is_null_not_empty(client: TestClient) -> None:
    """Ausserhalb des Images gibt es kein eingebackenes Binary.

    „Kein Wert" und „leerer Commit-Name" sind verschiedene Aussagen; nur
    `null` ist ehrlich.
    """
    payload = client.get("/status").json()

    assert payload["components"]["acoustid_index_commit"] is None


def test_fresh_instance_reports_sleeping_and_no_data(client: TestClient) -> None:
    payload = client.get("/status").json()
    assert payload["stack"]["state"] == StackState.SLEEPING.value
    assert payload["stack"]["state_display"] == "schlafend"
    assert payload["stack"]["since"]
    assert payload["stack"]["detail"] is None
    # Nie ein Delta eingespielt: „Stand unbekannt", nicht „Stand leer".
    assert payload["data"] is None
    assert payload["last_update_run"] is None


def test_status_reflects_the_current_stack_state(
    client: TestClient, service: WatchdogService
) -> None:
    # Der Fehlerzustand entsteht nur aus einem gescheiterten Start
    # (Uebergangstabelle in :mod:`acoustid_watchdog.state`).
    service.state.to(StackState.STARTING)
    service.state.to(StackState.ERROR, detail="db-Container startet nicht")
    payload = client.get("/status").json()
    assert payload["stack"]["state"] == "error"
    assert payload["stack"]["state_display"] == "fehler"
    assert payload["stack"]["detail"] == "db-Container startet nicht"


def test_status_shows_the_last_update_run(client: TestClient, service: WatchdogService) -> None:
    run_id = start_run(service.db, RunKind.ACOUSTID_DELTA)
    finish_run(
        service.db,
        run_id,
        RunResult.SUCCESS,
        files_imported=4,
        rows_imported=98_000,
        last_sequence="2026-07-22",
    )

    payload = client.get("/status").json()
    last = payload["last_update_run"]
    assert last["id"] == run_id
    assert last["result"] == "success"
    assert last["files_imported"] == 4
    assert last["rows_imported"] == 98_000
    assert last["running"] is False

    assert payload["data"] == {
        "last_sequence": "2026-07-22",
        "updated_at": last["finished_at"],
        "run_id": run_id,
    }


def test_status_shows_a_running_import(client: TestClient, service: WatchdogService) -> None:
    run_id = start_run(service.db, RunKind.ACOUSTID_DELTA)
    last = client.get("/status").json()["last_update_run"]
    assert last["id"] == run_id
    assert last["running"] is True
    assert last["result"] is None
    assert last["finished_at"] is None


def test_status_keeps_the_data_state_when_a_later_run_fails(
    client: TestClient, service: WatchdogService
) -> None:
    """Ein gescheiterter Lauf aendert den Datenstand nicht (Invariante §8.3/§8.4)."""
    good = start_run(service.db, RunKind.ACOUSTID_DELTA)
    finish_run(service.db, good, RunResult.SUCCESS, last_sequence="2026-07-22")
    bad = start_run(service.db, RunKind.ACOUSTID_DELTA)
    finish_run(service.db, bad, RunResult.FAILED, error="Plattenplatz zu knapp")

    payload = client.get("/status").json()
    assert payload["data"]["last_sequence"] == "2026-07-22"
    assert payload["last_update_run"]["id"] == bad
    assert payload["last_update_run"]["error"] == "Plattenplatz zu knapp"


def test_backup_runs_do_not_show_up_as_update_run(
    client: TestClient, service: WatchdogService
) -> None:
    update = start_run(service.db, RunKind.ACOUSTID_DELTA)
    finish_run(service.db, update, RunResult.SUCCESS, last_sequence="2026-07-22")
    backup = start_run(service.db, RunKind.BACKUP)
    finish_run(service.db, backup, RunResult.SUCCESS)

    payload = client.get("/status").json()
    assert payload["last_update_run"]["id"] == update


def test_status_needs_no_authentication(client: TestClient) -> None:
    """Die Bereitschaftsanzeige der Instanz ist bewusst offen (§7)."""
    assert client.get("/status").status_code == 200


def test_status_is_a_read_only_endpoint(client: TestClient) -> None:
    assert client.post("/status").status_code == 405


# --- Die eigentliche Zusage: nichts wird geweckt ----------------------------


def test_status_opens_no_network_connection(
    client: TestClient, service: WatchdogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariante §8.2 — baulich geprueft, nicht nur zugesagt.

    Der Weg von `/status` darf weder die AcoustID-Postgres noch den
    Suchindex noch den API-Dienst noch den Docker-Socket beruehren: jeder
    dieser Wege liefe ueber einen Socket, und der Stack wuerde davon
    aufwachen. Also wird das Anlegen von Sockets fuer die Dauer der
    Anfrage schlicht verboten.
    """
    run_id = start_run(service.db, RunKind.ACOUSTID_DELTA)
    finish_run(service.db, run_id, RunResult.SUCCESS, last_sequence="2026-07-22")

    def verboten(*args: object, **kwargs: object) -> None:
        raise AssertionError("/status hat eine Netzwerkverbindung geoeffnet")

    monkeypatch.setattr(socket, "socket", verboten)
    monkeypatch.setattr(socket, "create_connection", verboten)

    payload = client.get("/status").json()
    assert payload["data"]["last_sequence"] == "2026-07-22"


def test_status_reports_a_broken_state_database_as_an_error(
    client: TestClient, service: WatchdogService
) -> None:
    """Lieber ein ehrlicher Fehler als ein geschoentes „alles gut"."""
    service.db.close()
    response = client.get("/status")
    assert response.status_code == 500
    assert response.json()["status"] == "error"
