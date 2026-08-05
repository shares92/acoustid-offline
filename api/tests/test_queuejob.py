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

from acoustid_api.queuejob import (
    EXIT_OK,
    MAX_CATCH_UP_ROUNDS,
    REPORT_SCHEMA,
    _gave_up_rows,
    _newly_gave_up,
    main,
)
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
def test_given_up_groups_are_found_in_the_database(db: psycopg.Connection) -> None:
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

    rows = _gave_up_rows(db)

    assert list(rows) == [2147483648]
    assert rows[2147483648] == (MAX_FORWARD_ATTEMPTS, "HTTP 500")


@pytest.mark.integration
def test_an_empty_queue_has_nothing_given_up(db: psycopg.Connection) -> None:
    assert _gave_up_rows(db) == {}


# --- K5: gemeldet wird nur, was DIESER Lauf aufgibt --------------------------


def test_only_the_new_ones_are_reported() -> None:
    """Der Bestand allein feuerte jede Nacht dieselben IDs.

    Nach ein paar Wochen haette der Betreiber gelernt, die Meldung zu
    ignorieren — und genau dann kaeme die erste echte durch.
    """
    before = {17: (7, "HTTP 500")}
    after = {17: (7, "HTTP 500"), 18: (7, "HTTP 502"), 19: (7, None)}

    details = _newly_gave_up(before, after)

    assert details["gave_up_track_ids"] == [18, 19]
    assert details["gave_up_total"] == 3  # der Bestand steht daneben
    assert details["forward_attempts"] == 7
    assert details["forward_error"] == "HTTP 502"


def test_a_quiet_run_reports_nothing_given_up() -> None:
    """Derselbe Bestand vorher wie nachher — also keine Meldung."""
    bestand = {17: (7, "HTTP 500")}

    details = _newly_gave_up(bestand, bestand)

    assert details["gave_up_track_ids"] == []
    assert details["gave_up_total"] == 1
    assert details["forward_attempts"] == 0
    assert details["forward_error"] is None


def test_the_very_first_give_up_is_reported() -> None:
    details = _newly_gave_up({}, {17: (7, "HTTP 500")})
    assert details["gave_up_track_ids"] == [17]


# --- K4: der Nachlauf raeumt vollstaendig auf -------------------------------


def test_the_catch_up_loops_until_the_backlog_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein einzelner Aufruf liess bei >200 Eintraegen den Rest still liegen.

    Nach einem Restore (docs/backup-restore.md) sind das schnell Tausende:
    200 waeren sichtbar, der Rest unauffindbar — und der Lauf meldete
    trotzdem „ok".
    """
    from acoustid_api import queuejob

    batches = [200, 200, 137]
    calls: list[int] = []

    def fake_index_pending(connection: object, service: object) -> int:
        handled = batches[len(calls)]
        calls.append(handled)
        return handled

    monkeypatch.setattr(queuejob, "index_pending", fake_index_pending)

    indexed, rounds = queuejob._catch_up(object(), object())  # type: ignore[arg-type]

    assert indexed == 537
    assert rounds == 3


def test_the_catch_up_stops_when_nothing_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    from acoustid_api import queuejob

    monkeypatch.setattr(queuejob, "index_pending", lambda *_args: 0)

    assert queuejob._catch_up(object(), object()) == (0, 1)  # type: ignore[arg-type]


def test_the_catch_up_has_a_safety_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein Arbeitsvorrat, der nicht schrumpft, darf nicht endlos drehen."""
    from acoustid_api import queuejob
    from acoustid_api.submit import MAX_INDEX_BATCH

    monkeypatch.setattr(queuejob, "index_pending", lambda *_args: MAX_INDEX_BATCH)

    indexed, rounds = queuejob._catch_up(object(), object())  # type: ignore[arg-type]

    assert rounds == MAX_CATCH_UP_ROUNDS
    assert indexed == MAX_CATCH_UP_ROUNDS * MAX_INDEX_BATCH
