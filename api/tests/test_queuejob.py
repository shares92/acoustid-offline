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
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

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
from shared.env import EnvSettings
from shared.fpindex import FpIndexClient


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap-Umgebung **ohne** Dienste — der Job soll trotzdem antworten.

    Das Passwort ist bewusst erfunden: diese Tests kommen nie bis zu einer
    Verbindung, brauchen aber einen baubaren DSN (:meth:`shared.env.
    EnvSettings.db_dsn` wirft sonst).
    """
    monkeypatch.setenv("MMO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MMO_DB_PASSWORD", "geheim")
    return tmp_path


@pytest.fixture
def live_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection) -> Path:
    """Bootstrap-Umgebung **mit** echter Datenbank — fuer den ganzen CLI-Lauf.

    Der Job liest seine Zugaenge wie im Betrieb aus den ``MMO_``-Variablen
    (:func:`acoustid_api.queuejob.main`). Sie werden hier **vollstaendig
    und ausdruecklich** gesetzt, statt sich auf die Prozessumgebung zu
    verlassen — zwei Fallen sonst:

    * Ein erfundenes ``MMO_DB_PASSWORD`` (wie in :func:`env`) laesst die
      Anmeldung scheitern, und der Pool laeuft in seinen Timeout.
    * ``MMO_DB_PASSWORD_FILE`` **folgt** dem Datenverzeichnis
      (``shared.env._derive_defaults``): ein umgebogenes ``MMO_DATA_DIR``
      zeigt sonst auf eine Passwortdatei, die es dort nicht gibt.

    Die Datenbank ist die frisch angelegte des ``db``-Fixtures.
    """
    original = EnvSettings.from_env()
    monkeypatch.setenv("MMO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MMO_DB_PASSWORD_FILE", str(original.db_password_file))
    monkeypatch.setenv("MMO_DB_HOST", original.db_host)
    monkeypatch.setenv("MMO_DB_PORT", str(original.db_port))
    monkeypatch.setenv("MMO_DB_USER", original.db_user)
    monkeypatch.setenv("MMO_DB_NAME", db.info.dbname)
    monkeypatch.setenv("MMO_INDEX_URL", original.index_url)
    monkeypatch.setenv("MMO_INDEX_NAME", original.index_name)
    password = original.db_password.get_secret_value()
    if password:
        monkeypatch.setenv("MMO_DB_PASSWORD", password)
    else:
        # Das Passwort steht in der Datei — dann darf die Variable nicht
        # als leerer Rest herumliegen.
        monkeypatch.delenv("MMO_DB_PASSWORD", raising=False)
    return tmp_path


@pytest.mark.integration
def test_without_upstream_the_job_still_catches_up(live_env: Path) -> None:
    """Modus ``local`` ist eine Betreiber-Entscheidung, kein Fehler.

    Der Lauf steht deswegen nicht rot in der Historie — und der
    **Nachlauf** passiert trotzdem: waehrend des Delta-Imports
    zurueckgestellte Einreichungen (Betreiber-Entscheid 2026-08-05) waeren
    sonst gespeichert, aber im Index unauffindbar.
    """
    report_path = live_env / "jobs" / "queue-send.json"

    code = main(["--report", str(report_path)])

    assert code == EXIT_OK
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["schema"] == REPORT_SCHEMA
    assert document["result"] == "disabled"
    assert document["exit_code"] == 0
    assert document["attempted"] == 0
    assert document["indexed"] == 0  # es lag nichts an
    assert document["catch_up_rounds"] == 1


@pytest.mark.integration
def test_the_report_lands_on_stdout_by_default(
    live_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Log auf stderr, Report auf stdout — die beiden Stroeme bleiben getrennt."""
    assert main([]) == EXIT_OK

    document = json.loads(capsys.readouterr().out)
    assert document["result"] == "disabled"


@pytest.fixture
def live_index(live_env: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Wie :func:`live_env`, zusaetzlich mit **eigenem** Suchindex.

    Eigener Name je Lauf und danach abgeraeumt: der Server kann beliebig
    viele halten, und kein Test darf den Index eines anderen anfassen
    (dasselbe Muster wie in ``importer/tests/test_indexfeed_integration.py``).
    """
    monkeypatch.setenv("MMO_INDEX_NAME", f"queuejob{uuid4().hex[:8]}")
    client = FpIndexClient.from_env(EnvSettings.from_env())
    client.ensure_index()
    try:
        yield live_env
    finally:
        client.delete_index(missing_ok=True)
        client.close()


@pytest.mark.integration
@pytest.mark.db
@pytest.mark.index
def test_deferred_submissions_are_caught_up(live_index: Path, db: psycopg.Connection) -> None:
    """Der eigentliche Zweck des Nachlaufs (§8.12, K4).

    Eine Einreichung im Status ``new`` — genau das, was waehrend eines
    Delta-Imports liegen bleibt — wird hier nachgetragen, und zwar
    unabhaengig vom Submit-Modus.
    """
    live_env = live_index
    db.execute(
        "INSERT INTO local_submission (local_track_id, local_track_gid, fingerprint, length)"
        " VALUES (23, gen_random_uuid(), %s, 137)",
        (list(range(1000, 1300)),),
    )
    report_path = live_env / "queue.json"

    assert main(["--report", str(report_path)]) == EXIT_OK

    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["indexed"] == 1
    # Und die Zeile traegt jetzt ihren Indexierungs-Zeitpunkt.
    row = db.execute(
        "SELECT status, indexed_at FROM local_submission WHERE local_track_id = 23"
    ).fetchone()
    assert row is not None and row[0] == "indexed" and row[1] is not None


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
    # `local_track_id` ist **integer** (Sequenz bis 2147483647, §5.2); der
    # reservierte Bereich [2^31, 2^32-1] gilt fuer die **Dokument-ID** im
    # Suchindex — sie entsteht erst durch den Offset `LOCAL_DOC_ID_BASE`
    # (§5.3). Der Wert 2^31 gehoert also nicht in diese Spalte.
    db.execute(
        "INSERT INTO local_submission"
        " (local_track_id, local_track_gid, fingerprint, length, status,"
        "  forward_attempts, forward_error)"
        " VALUES (17, gen_random_uuid(), ARRAY[1], 10, 'forward_failed', %s, %s),"
        "        (18, gen_random_uuid(), ARRAY[2], 11, 'forward_failed', 2, 'nur zwei'),"
        "        (19, gen_random_uuid(), ARRAY[3], 12, 'indexed', 0, NULL)",
        (MAX_FORWARD_ATTEMPTS, "HTTP 500"),
    )

    rows = _gave_up_rows(db)

    assert list(rows) == [17]
    assert rows[17] == (MAX_FORWARD_ATTEMPTS, "HTTP 500")


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
