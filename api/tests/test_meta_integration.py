"""``meta`` gegen echtes Postgres mit MB-Mini-Schema (Phase 10).

Marker `integration` (also nur Postgres — der acoustid-index wird hier nicht
gebraucht: alle Lookups laufen ueber ``trackid``, und der Weg beruehrt die
Suche nicht). Steuerung ueber `--integration` bzw.
`ACOUSTID_INTEGRATION_TESTS`, siehe conftest.py im Repo-Wurzelverzeichnis.

Lokal::

    docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db
    MMO_DB_HOST=127.0.0.1 uv run pytest api/tests/test_meta_integration.py \\
        --integration=require

Der Nachweis, um den es geht: unser SQL passt zum echten MB-Schema, die
Choreografie liefert die Antwortstruktur des Originals, und ein Ausfall des
Spiegels kostet Metadaten — keine Antwort (Invariante §8.7). Das
Mini-Schema samt Testbestand steht in `mb_fixture.py`; echte MusicBrainz-
Dumps kommen hier bewusst nicht vor.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import mb_fixture
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from acoustid_api.main import create_app
from acoustid_api.service import ApiService
from shared.config import Config
from shared.env import EnvSettings
from shared.fpindex import FpIndexClient
from shared.mb import MbClient
from shared.mb import queries as mb_queries

pytestmark = [pytest.mark.integration]

DAY = date(2026, 7, 22)
STAMP = "2026-07-22T12:00:00+00:00"

#: AcoustID, die in allen Tests nachgeschlagen wird.
ACOUSTID = UUID("f0f0f0f0-0000-4000-8000-000000000001")

#: DSN auf einen Port, an dem niemand lauscht (Ausfall-Test).
DEAD_DSN = "host=127.0.0.1 port=1 dbname=musicbrainz_db connect_timeout=1"


# --- Bestand ---------------------------------------------------------------


@pytest.fixture
def mb_schema(db: psycopg.Connection) -> psycopg.Connection:
    """MB-Mini-Schema in derselben Wegwerf-Datenbank."""
    mb_fixture.create_schema(db)
    mb_fixture.seed(db)
    return db


def seed_track(
    db: psycopg.Connection,
    *,
    track_id: int = 1,
    gid: UUID = ACOUSTID,
    mbids: tuple[tuple[str, int], ...] = ((mb_fixture.MBID_ONE, 7),),
    disabled: bool = False,
) -> None:
    """Eine AcoustID mit ihren Recording-MBIDs anlegen (unser Bestand)."""
    db.execute(
        "INSERT INTO track (id, gid, created, src_day) VALUES (%s, %s, %s, %s)",
        (track_id, gid, STAMP, DAY),
    )
    for index, (mbid, sources) in enumerate(mbids, start=track_id * 100):
        db.execute(
            "INSERT INTO track_mbid "
            "(id, track_id, mbid, submission_count, disabled, created, src_day) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (index, track_id, mbid, sources, disabled, STAMP, DAY),
        )


def seed_user_meta(db: psycopg.Connection, *, track_id: int = 1) -> None:
    """Eingereichte Textmetadaten fuer den ``usermeta``-Rueckfall."""
    db.execute(
        "INSERT INTO meta (id, track, artist, album, album_artist, track_no, disc_no, year, "
        "created, src_day) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            77,
            "Eingereicht",
            "Eingereichte Band",
            "Eingereichtes Album",
            "Album-Band",
            3,
            1,
            2003,
            STAMP,
            DAY,
        ),
    )
    db.execute(
        "INSERT INTO track_meta (id, track_id, meta_id, submission_count, created, src_day) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (1, track_id, 77, 5, STAMP, DAY),
    )


@pytest.fixture
def make_client(scratch_env: EnvSettings) -> Iterator[Any]:
    """Fabrik fuer eine App mit frei waehlbarem MusicBrainz-Zugang."""
    opened: list[Any] = []

    def build(
        dsn: str | None = None,
        *,
        row_limit: int = mb_queries.DEFAULT_ROW_LIMIT,
        config: Config | None = None,
    ) -> TestClient:
        pool = ConnectionPool(
            scratch_env.db_dsn().get_secret_value(),
            min_size=1,
            max_size=2,
            kwargs={"autocommit": True},
            open=False,
        )
        mb = None
        if dsn is not None:
            mb = MbClient(dsn, row_limit=row_limit, pool_timeout_s=0.5)
        # Der Index wird ueber `trackid` nie angefasst; die Adresse ist Zierde.
        index = FpIndexClient("http://127.0.0.1:1", "pytest")
        service = ApiService(pool, index, config or Config(), mb)
        service.open()
        opened.append(service)
        test_client = TestClient(create_app(service))
        test_client.__enter__()
        opened.append(test_client)
        return test_client

    try:
        yield build
    finally:
        for item in reversed(opened):
            if isinstance(item, TestClient):
                item.__exit__(None, None, None)
            else:
                item.close()


@pytest.fixture
def client(scratch_env: EnvSettings, mb_schema: psycopg.Connection, make_client: Any) -> TestClient:
    """App mit erreichbarem MB-Mini-Schema."""
    return make_client(scratch_env.db_dsn().get_secret_value())


def lookup(client: TestClient, meta: str, **extra: str) -> dict[str, Any]:
    """Ein Lookup ueber ``trackid`` — ohne Suchindex."""
    response = client.get(
        "/v2/lookup", params={"client": "testkey", "trackid": str(ACOUSTID), "meta": meta, **extra}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok", payload
    return payload["results"][0]


# --- Selfcheck & Gesundheit -------------------------------------------------


def test_the_mini_schema_passes_the_selfcheck(
    scratch_env: EnvSettings, mb_schema: psycopg.Connection
) -> None:
    with MbClient(scratch_env.db_dsn().get_secret_value()) as mb:
        status = mb.startup_check()
    assert status.reachable is True
    assert status.schema_ok is True
    assert status.missing == ()
    assert status.release_event_view is True
    assert status.stale is None
    assert status.health is not None
    assert status.health.schema_sequence == mb_fixture.SCHEMA_SEQUENCE
    assert status.health.replication_sequence == 4242


def test_a_missing_column_is_found_and_answered_degraded(
    scratch_env: EnvSettings, mb_schema: psycopg.Connection, make_client: Any
) -> None:
    """Schema-Guard: fehlt eine Spalte, gibt es Lookups ohne Metadaten."""
    mb_schema.execute("ALTER TABLE musicbrainz.recording DROP COLUMN length")
    dsn = scratch_env.db_dsn().get_secret_value()

    with MbClient(dsn) as mb:
        status = mb.startup_check()
    assert status.schema_ok is False
    assert "recording.length" in status.missing

    seed_track(mb_schema)
    result = lookup(make_client(dsn), "recordings")
    assert result["recordings"] == [{"id": mb_fixture.MBID_ONE}]


def test_the_view_fallback_delivers_the_same_events(
    scratch_env: EnvSettings, mb_schema: psycopg.Connection, make_client: Any
) -> None:
    """Ohne ``release_event`` uebernimmt die Vereinigung der Basistabellen."""
    mb_schema.execute("DROP VIEW musicbrainz.release_event")
    dsn = scratch_env.db_dsn().get_secret_value()

    with MbClient(dsn) as mb:
        assert mb.startup_check().release_event_view is False

    seed_track(mb_schema)
    result = lookup(make_client(dsn), "recordings releases")
    releases = {item["id"]: item for item in result["recordings"][0]["releases"]}
    assert releases[mb_fixture.RELEASE_ONE_GID]["releaseevents"] == [
        {"country": "DE", "date": {"year": 1999, "month": 7}}
    ]
    assert releases[mb_fixture.RELEASE_TWO_GID]["releaseevents"] == [{"date": {"year": 2004}}]


# --- Die Abfragen selbst ----------------------------------------------------


def test_recordings_are_read_with_length_in_milliseconds(
    mb_schema: psycopg.Connection,
) -> None:
    found = mb_queries.recordings_by_mbids(mb_schema, [mb_fixture.MBID_ONE])
    row = found[mb_fixture.MBID_ONE]
    assert row.name == "Erstes Stueck"
    assert row.length_ms == mb_fixture.LENGTH_ONE_MS
    assert row.artist_credit_name == "Beispielband feat. Gaststimme"


def test_artist_credits_come_back_in_display_order(mb_schema: psycopg.Connection) -> None:
    credits = mb_queries.artist_credits(mb_schema, [10, 11])
    assert [item.name for item in credits[10]] == ["Beispielband", "Gaststimme"]
    assert credits[10][0].join_phrase == " feat. "
    assert credits[10][1].join_phrase == ""
    assert len(credits[11]) == 1


def test_release_counts_sum_all_mediums(mb_schema: psycopg.Connection) -> None:
    counts = mb_queries.release_counts(mb_schema, [500, 501])
    assert counts[500].medium_count == 2
    assert counts[500].track_count == 22
    assert counts[501].medium_count == 1


def test_secondary_types_follow_child_order(mb_schema: psycopg.Connection) -> None:
    """Eingefuegt wurde in verkehrter Reihenfolge."""
    assert mb_queries.release_group_secondary_types(mb_schema, [700]) == {
        700: ["Compilation", "Live"]
    }


def test_the_redirect_table_resolves_a_merged_recording(mb_schema: psycopg.Connection) -> None:
    assert mb_queries.resolve_recording_redirects(mb_schema, [mb_fixture.MBID_MERGED]) == {
        mb_fixture.MBID_MERGED: mb_fixture.MBID_ONE
    }


def test_the_existence_check_finds_only_known_mbids(mb_schema: psycopg.Connection) -> None:
    found = mb_queries.existing_recording_mbids(mb_schema, [mb_fixture.MBID_ONE, str(uuid4())])
    assert found == {mb_fixture.MBID_ONE}


def test_the_release_rows_are_capped_and_flagged(mb_schema: psycopg.Connection) -> None:
    """Aufnahme 1 steckt auf drei Tracks (zwei Medien, zwei Releases)."""
    full = mb_queries.recording_release_rows(mb_schema, [mb_fixture.MBID_ONE])
    assert len(full.rows) == 3
    assert full.truncated is False

    capped = mb_queries.recording_release_rows(mb_schema, [mb_fixture.MBID_ONE], limit_rows=1)
    assert len(capped.rows) == 1
    assert capped.truncated is True


# --- Antwort ueber HTTP -----------------------------------------------------


def test_meta_recordings_delivers_title_artists_and_duration(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema)
    result = lookup(client, "recordings")

    assert result["id"] == str(ACOUSTID)
    assert result["recordings"] == [
        {
            "id": mb_fixture.MBID_ONE,
            "title": "Erstes Stueck",
            # 209 999 ms — abgeschnitten, nicht gerundet.
            "duration": 209.0,
            "artists": [
                {
                    "id": "dddd0001-0000-0000-0000-000000000000",
                    "name": "Beispielband",
                    "joinphrase": " feat. ",
                },
                {"id": "dddd0002-0000-0000-0000-000000000000", "name": "Gaststimme"},
            ],
        }
    ]


def test_sources_come_from_our_own_track_mbid_table(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema, mbids=((mb_fixture.MBID_ONE, 42),))
    result = lookup(client, "recordings sources")
    assert result["recordings"][0]["sources"] == 42


def test_a_disabled_mbid_is_not_delivered(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema, disabled=True)
    result = lookup(client, "recordings")
    assert "recordings" not in result


def test_a_recording_without_releases_keeps_its_metadata(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    """Fallstrick 1: die Referenz liefert hier gar nichts."""
    seed_track(mb_schema, mbids=((mb_fixture.MBID_NO_RELEASE, 1),))
    result = lookup(client, "recordings releases")

    recording = result["recordings"][0]
    assert recording["title"] == "Ohne Album"
    assert recording["releases"] == []


def test_the_picard_combination_produces_the_full_tree(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema)
    result = lookup(client, "recordings releasegroups releases tracks compress sources")

    recording = result["recordings"][0]
    assert recording["sources"] == 7
    group = recording["releasegroups"][0]
    assert group["id"] == mb_fixture.RELEASE_GROUP_GID
    assert group["type"] == "Album"
    assert group["secondarytypes"] == ["Compilation", "Live"]

    releases = {item["id"]: item for item in group["releases"]}
    first = releases[mb_fixture.RELEASE_ONE_GID]
    assert first["medium_count"] == 2
    assert first["track_count"] == 22
    assert first["country"] == "DE"
    assert first["date"] == {"year": 1999, "month": 7}
    # `compress`: der Titel entspricht dem der Release-Gruppe.
    assert "title" not in first

    medium = next(item for item in first["mediums"] if item["position"] == 1)
    assert medium["track_count"] == 12
    assert medium["format"] == "CD"
    assert medium["tracks"][0]["id"] == mb_fixture.TRACK_ONE_GID
    assert medium["tracks"][0]["position"] == 4
    # `compress`: Trackname gleich dem Titel der Aufnahme.
    assert "title" not in medium["tracks"][0]

    bonus = next(item for item in first["mediums"] if item["position"] == 2)
    assert bonus["title"] == "Bonus-CD"


def test_the_releases_branch_hangs_directly_under_the_result(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema)
    result = lookup(client, "releases")
    assert "recordings" not in result
    assert {item["id"] for item in result["releases"]} == {
        mb_fixture.RELEASE_ONE_GID,
        mb_fixture.RELEASE_TWO_GID,
    }


def test_recordingids_deliver_only_the_mbid(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema)
    assert lookup(client, "recordingids")["recordings"] == [{"id": mb_fixture.MBID_ONE}]


def test_a_merged_mbid_is_resolved_to_the_canonical_one(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    """Unser Bestand kennt die alte MBID; MusicBrainz hat sie zusammengefuehrt."""
    seed_track(mb_schema, mbids=((mb_fixture.MBID_MERGED, 2),))
    recording = lookup(client, "recordings")["recordings"][0]

    assert recording["id"] == mb_fixture.MBID_ONE
    assert recording["title"] == "Erstes Stueck"


def test_the_submitted_mbid_can_be_kept(
    scratch_env: EnvSettings, mb_schema: psycopg.Connection, make_client: Any
) -> None:
    seed_track(mb_schema, mbids=((mb_fixture.MBID_MERGED, 2),))
    config = Config.model_validate({"mb": {"keep_submitted_mbid": True}})
    test_client = make_client(scratch_env.db_dsn().get_secret_value(), config=config)

    recording = lookup(test_client, "recordings")["recordings"][0]
    assert recording["id"] == mb_fixture.MBID_MERGED
    assert recording["title"] == "Erstes Stueck"


def test_an_unknown_mbid_yields_a_bare_recording(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    unknown = str(uuid4())
    seed_track(mb_schema, mbids=((unknown, 1),))
    assert lookup(client, "recordings")["recordings"] == [{"id": unknown}]


def test_usermeta_fills_in_when_musicbrainz_knows_nothing(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema, mbids=((str(uuid4()), 1),))
    seed_user_meta(mb_schema)

    recordings = lookup(client, "recordings usermeta")["recordings"]
    assert recordings[1]["title"] == "Eingereicht"
    assert recordings[1]["artists"] == ["Eingereichte Band"]


def test_m2_delivers_a_flat_track_list(mb_schema: psycopg.Connection, client: TestClient) -> None:
    seed_track(mb_schema)
    recording = lookup(client, "m2")["recordings"][0]

    assert recording["duration"] == 209.0
    positions = sorted(item["position"] for item in recording["tracks"])
    assert positions == [1, 4, 5]
    # Track 9002 hat keine Laenge — im Original ein TypeError.
    without_length = next(item for item in recording["tracks"] if item["position"] == 1)
    assert "duration" not in without_length


def test_the_xml_format_renders_the_nested_structure(
    mb_schema: psycopg.Connection, client: TestClient
) -> None:
    seed_track(mb_schema)
    response = client.get(
        "/v2/lookup",
        params={
            "client": "testkey",
            "trackid": str(ACOUSTID),
            "meta": "recordings releases tracks",
            "format": "xml",
        },
    )
    assert response.status_code == 200
    body = response.text
    assert "<recordings><recording>" in body
    assert "<releases><release>" in body
    assert "<mediums><medium>" in body
    assert "<tracks><track>" in body


# --- Degradierter Betrieb ---------------------------------------------------


def test_an_unreachable_mirror_still_answers_with_uuids_and_mbids(
    mb_schema: psycopg.Connection, make_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Invariante §8.7 — HTTP 200, MBIDs und ``sources`` bleiben."""
    seed_track(mb_schema)
    caplog.set_level(logging.WARNING, logger="acoustid_api.meta")

    result = lookup(make_client(DEAD_DSN), "recordings sources")

    assert result["id"] == str(ACOUSTID)
    assert result["recordings"] == [{"id": mb_fixture.MBID_ONE, "sources": 7}]
    assert any("ohne MusicBrainz-Metadaten" in record.message for record in caplog.records)


def test_without_a_configured_mirror_the_lookup_still_works(
    mb_schema: psycopg.Connection, make_client: Any
) -> None:
    seed_track(mb_schema)
    result = lookup(make_client(None), "recordings")
    assert result["recordings"] == [{"id": mb_fixture.MBID_ONE}]


def test_a_lookup_without_meta_never_touches_musicbrainz(
    mb_schema: psycopg.Connection, make_client: Any
) -> None:
    seed_track(mb_schema)
    response = make_client(DEAD_DSN).get(
        "/v2/lookup", params={"client": "testkey", "trackid": str(ACOUSTID)}
    )
    assert response.json() == {
        "status": "ok",
        "results": [{"id": str(ACOUSTID), "score": 1.0}],
    }


def test_the_row_limit_truncates_and_is_logged(
    scratch_env: EnvSettings,
    mb_schema: psycopg.Connection,
    make_client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seed_track(mb_schema)
    caplog.set_level(logging.WARNING, logger="shared.mb.metadata")

    test_client = make_client(scratch_env.db_dsn().get_secret_value(), row_limit=1)
    result = lookup(test_client, "recordings releases")

    assert len(result["recordings"][0]["releases"]) == 1
    assert any("gekappt" in record.message for record in caplog.records)
