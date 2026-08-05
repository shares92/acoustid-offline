"""Warteschlangenlauf als One-Shot-Job (M2.5, E10).

Der Job ist die Kommandozeile um :func:`~acoustid_api.upstream.drain_queue`
— gebaut, weil der Waechter den Lauf zwar anstoessen, aber nicht selbst
ausfuehren darf: er haelt bewusst keine Verbindung zum Array (§8.2).

Geprueft wird hier der **Vertrag** (Exit-Code, Report, Modus-Verhalten),
nicht die Weiterleitung selbst — die steht seit Phase 12 in
``test_upstream.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from acoustid_api.queuejob import EXIT_OK, REPORT_SCHEMA, _gave_up_details, main
from acoustid_api.upstream import MAX_FORWARD_ATTEMPTS


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap-Umgebung ohne Dienste — der Job soll trotzdem antworten."""
    monkeypatch.setenv("MMO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MMO_DB_PASSWORD", "geheim")
    return tmp_path


@pytest.mark.integration
def test_without_upstream_the_job_still_catches_up(
    env: Path, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modus ``local`` ist eine Betreiber-Entscheidung, kein Fehler.

    Der Lauf steht deswegen nicht rot in der Historie — und der
    **Nachlauf** passiert trotzdem: waehrend des Delta-Imports
    zurueckgestellte Einreichungen (Betreiber-Entscheid 2026-08-05) waeren
    sonst gespeichert, aber im Index unauffindbar.
    """
    monkeypatch.setenv("MMO_DB_NAME", db.info.dbname)
    report_path = env / "jobs" / "queue-send.json"

    code = main(["--report", str(report_path)])

    assert code == EXIT_OK
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["schema"] == REPORT_SCHEMA
    assert document["result"] == "disabled"
    assert document["exit_code"] == 0
    assert document["attempted"] == 0
    assert document["indexed"] == 0  # es lag nichts an


@pytest.mark.integration
def test_the_report_lands_on_stdout_by_default(
    env: Path,
    db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Log auf stderr, Report auf stdout — die beiden Stroeme bleiben getrennt."""
    monkeypatch.setenv("MMO_DB_NAME", db.info.dbname)

    assert main([]) == EXIT_OK

    document = json.loads(capsys.readouterr().out)
    assert document["result"] == "disabled"


def test_an_unreachable_database_is_reported_not_raised(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auch ein gescheiterter Lauf hinterlaesst einen Report."""
    monkeypatch.setenv("MMO_DB_PORT", "1")  # dort lauscht nichts
    report_path = env / "queue.json"

    code = main(["--report", str(report_path)])

    assert code == 1
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["result"] == "failed"
    assert document["error"]["message"]


def test_a_broken_environment_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ohne Datenbank-Passwort ist der Job nicht startbar (Exit-Code 2)."""
    monkeypatch.setenv("MMO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MMO_DB_PASSWORD", raising=False)
    monkeypatch.setenv("MMO_DB_PASSWORD_FILE", str(tmp_path / "gibt-es-nicht"))
    report_path = tmp_path / "queue.json"

    assert main(["--report", str(report_path)]) == 2

    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["result"] == "usage_error"


# --- Die aufgegebenen Gruppen (§8.9) ----------------------------------------


@pytest.mark.integration
def test_given_up_groups_end_up_in_the_report(db: psycopg.Connection) -> None:
    """Der Waechter sieht das Ereignis aus Phase 12 nur ueber diesen Weg.

    Es entsteht im API-Prozess und steht dort im Log; die Benachrichtigung
    aus M2.5 braucht aber ``local_track_id``, ``forward_attempts`` und
    ``forward_error``.
    """
    db.execute(
        "INSERT INTO local_submission"
        " (local_track_id, local_track_gid, fingerprint, length, status,"
        "  forward_attempts, forward_error)"
        " VALUES (2147483648, gen_random_uuid(), ARRAY[1], 10, 'forward_failed', %s, %s),"
        "        (2147483649, gen_random_uuid(), ARRAY[2], 11, 'forward_failed', 2, 'nur zwei'),"
        "        (2147483650, gen_random_uuid(), ARRAY[3], 12, 'indexed', 0, NULL)",
        (MAX_FORWARD_ATTEMPTS, "HTTP 500"),
    )

    details = _gave_up_details(db)

    assert details["gave_up_track_ids"] == [2147483648]
    assert details["forward_attempts"] == MAX_FORWARD_ATTEMPTS
    assert details["forward_error"] == "HTTP 500"


@pytest.mark.integration
def test_an_empty_queue_reports_nothing_given_up(db: psycopg.Connection) -> None:
    details = _gave_up_details(db)
    assert details == {
        "gave_up_track_ids": [],
        "forward_attempts": 0,
        "forward_error": None,
    }
