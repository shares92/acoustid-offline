"""Choreografie der MB-Abfragen (Phase 10) — ohne Datenbank.

Geprueft wird die **Reihenfolge und Anzahl** der Aufrufe aus dem
Phase-1-Bericht sowie die drei bewussten Abweichungen von der Referenz
(Basiszeile ohne Veroeffentlichung, ``.get()`` statt Direktzugriff,
Ganzzahldivision). Dafuer tritt an die Stelle von
:mod:`shared.mb.queries` eine Attrappe, die vorbereitete Zeilen liefert und
jeden Aufruf mitschreibt; das echte SQL pruefen die Integrationstests gegen
das MB-Mini-Schema.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.mb import metadata
from shared.mb.metadata import duration_seconds, lookup_metadata
from shared.mb.queries import (
    ArtistRow,
    RecordingRow,
    ReleaseCounts,
    ReleaseEventRow,
    ReleaseGroupRow,
    ReleaseRow,
    ReleaseRows,
)

MBID_A = "11111111-1111-1111-1111-111111111111"
MBID_B = "22222222-2222-2222-2222-222222222222"
MBID_OLD = "99999999-9999-9999-9999-999999999999"


def recording(gid: str, *, length_ms: int | None = 209_999, credit: int = 10) -> RecordingRow:
    return RecordingRow(
        recording_id=abs(hash(gid)) % 1000,
        gid=gid,
        name=f"Titel {gid[:2]}",
        length_ms=length_ms,
        artist_credit=credit,
        artist_credit_name="Kuenstler",
    )


def release_row(gid: str, *, release_id: int = 500, group_id: int = 700) -> ReleaseRow:
    return ReleaseRow(
        recording_gid=gid,
        track_gid=f"track-{release_id}",
        track_position=3,
        track_name="Trackname",
        track_artist_credit=11,
        track_length_ms=185_500,
        medium_position=1,
        medium_track_count=12,
        medium_name=None,
        medium_format="CD",
        release_id=release_id,
        release_gid=f"release-{release_id}",
        release_name="Albumname",
        release_artist_credit=12,
        release_group_id=group_id,
    )


class FakeQueries:
    """Steht an der Stelle von :mod:`shared.mb.queries`."""

    def __init__(self, **data: Any) -> None:
        self.recordings: dict[str, RecordingRow] = data.get("recordings", {})
        self.existing: set[str] = data.get("existing", set())
        self.redirects: dict[str, str] = data.get("redirects", {})
        self.release_rows: list[ReleaseRow] = data.get("release_rows", [])
        self.truncated: bool = data.get("truncated", False)
        self.counts: dict[int, ReleaseCounts] = data.get("counts", {})
        self.events: dict[int, list[ReleaseEventRow]] = data.get("events", {})
        self.groups: dict[int, ReleaseGroupRow] = data.get("groups", {})
        self.secondary: dict[int, list[str]] = data.get("secondary", {})
        self.credits: dict[int, list[ArtistRow]] = data.get("credits", {})
        self.calls: list[str] = []
        self.credit_ids: list[int] = []
        self.event_view: bool | None = None
        self.row_limit: int | None = None

    def recordings_by_mbids(self, _connection: Any, mbids: Any) -> dict[str, RecordingRow]:
        self.calls.append("recordings_by_mbids")
        return {gid: self.recordings[gid] for gid in mbids if gid in self.recordings}

    def existing_recording_mbids(self, _connection: Any, mbids: Any) -> set[str]:
        self.calls.append("existing_recording_mbids")
        return {gid for gid in mbids if gid in self.existing}

    def resolve_recording_redirects(self, _connection: Any, mbids: Any) -> dict[str, str]:
        self.calls.append("resolve_recording_redirects")
        return {gid: self.redirects[gid] for gid in mbids if gid in self.redirects}

    def recording_release_rows(
        self, _connection: Any, mbids: Any, *, limit_rows: int
    ) -> ReleaseRows:
        self.calls.append("recording_release_rows")
        self.row_limit = limit_rows
        return ReleaseRows(
            rows=[row for row in self.release_rows if row.recording_gid in set(mbids)],
            truncated=self.truncated,
        )

    def release_counts(self, _connection: Any, ids: Any) -> dict[int, ReleaseCounts]:
        self.calls.append("release_counts")
        return {key: value for key, value in self.counts.items() if key in set(ids)}

    def release_events(
        self, _connection: Any, ids: Any, *, use_view: bool
    ) -> dict[int, list[ReleaseEventRow]]:
        self.calls.append("release_events")
        self.event_view = use_view
        return {key: value for key, value in self.events.items() if key in set(ids)}

    def release_groups(self, _connection: Any, ids: Any) -> dict[int, ReleaseGroupRow]:
        self.calls.append("release_groups")
        return {key: value for key, value in self.groups.items() if key in set(ids)}

    def release_group_secondary_types(self, _connection: Any, ids: Any) -> dict[int, list[str]]:
        self.calls.append("release_group_secondary_types")
        return {key: value for key, value in self.secondary.items() if key in set(ids)}

    def artist_credits(self, _connection: Any, ids: Any) -> dict[int, list[ArtistRow]]:
        self.calls.append("artist_credits")
        self.credit_ids = sorted(ids)
        return self.credits


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Setzt die Attrappe ein und gibt eine Fabrik zurueck."""

    def install(**data: Any) -> FakeQueries:
        stand_in = FakeQueries(**data)
        monkeypatch.setattr(metadata, "queries", stand_in)
        return stand_in

    return install


# --- Ganzzahldivision -------------------------------------------------------


@pytest.mark.parametrize(
    ("milliseconds", "seconds"),
    [
        (None, None),
        (0, 0),
        (999, 0),
        (1000, 1),
        (1999, 1),  # abschneiden, nicht runden — die Kante schlechthin
        (209_999, 209),
        (210_000, 210),
    ],
)
def test_milliseconds_are_truncated_never_rounded(
    milliseconds: int | None, seconds: int | None
) -> None:
    assert duration_seconds(milliseconds) == seconds


# --- Choreografie -----------------------------------------------------------


def test_recordings_need_two_queries(fake: Any) -> None:
    stand_in = fake(recordings={MBID_A: recording(MBID_A)})
    result = lookup_metadata(None, [MBID_A])

    assert stand_in.calls == ["recordings_by_mbids", "artist_credits"]
    assert len(result.rows) == 1
    assert result.rows[0]["recording_id"] == MBID_A
    assert result.rows[0]["recording_duration"] == 209


def test_recordingids_uses_the_existence_check(fake: Any) -> None:
    stand_in = fake(existing={MBID_A})
    result = lookup_metadata(None, [MBID_A], only_ids=True)

    assert stand_in.calls == ["existing_recording_mbids", "artist_credits"]
    assert [row["recording_id"] for row in result.rows] == [MBID_A]
    # Ohne Nutzdaten: Titel leer, keine Laenge.
    assert result.rows[0]["recording_title"] == ""
    assert result.rows[0]["recording_duration"] is None


def test_an_unknown_mbid_is_retried_through_the_redirect_table(fake: Any) -> None:
    stand_in = fake(recordings={MBID_A: recording(MBID_A)}, redirects={MBID_OLD: MBID_A})
    result = lookup_metadata(None, [MBID_OLD])

    assert stand_in.calls == [
        "recordings_by_mbids",
        "resolve_recording_redirects",
        "recordings_by_mbids",
        "artist_credits",
    ]
    assert result.redirects == {MBID_OLD: MBID_A}
    # Die Antwort traegt die kanonische MBID.
    assert [row["recording_id"] for row in result.rows] == [MBID_A]


def test_without_a_redirect_there_is_no_second_attempt(fake: Any) -> None:
    stand_in = fake(recordings={})
    result = lookup_metadata(None, [MBID_OLD])

    assert stand_in.calls == ["recordings_by_mbids", "resolve_recording_redirects"]
    assert result.rows == []
    assert result.redirects == {}


def test_a_hit_needs_no_redirect_query(fake: Any) -> None:
    stand_in = fake(recordings={MBID_A: recording(MBID_A)})
    lookup_metadata(None, [MBID_A])
    assert "resolve_recording_redirects" not in stand_in.calls


def test_releases_add_three_queries(fake: Any) -> None:
    stand_in = fake(
        recordings={MBID_A: recording(MBID_A)},
        release_rows=[release_row(MBID_A)],
        counts={500: ReleaseCounts(medium_count=2, track_count=24)},
        events={500: [ReleaseEventRow(country="DE", date_year=1999, date_month=4, date_day=None)]},
    )
    result = lookup_metadata(None, [MBID_A], load_releases=True)

    assert stand_in.calls == [
        "recordings_by_mbids",
        "recording_release_rows",
        "release_counts",
        "release_events",
        "artist_credits",
    ]
    row = result.rows[0]
    assert row["release_id"] == "release-500"
    assert row["release_medium_count"] == 2
    assert row["release_track_count"] == 24
    assert row["release_events"] == [
        {
            "release_country": "DE",
            "release_date_year": 1999,
            "release_date_month": 4,
            "release_date_day": None,
        }
    ]
    assert row["track_duration"] == 185  # 185500 ms, abgeschnitten


def test_release_groups_add_two_more_queries(fake: Any) -> None:
    stand_in = fake(
        recordings={MBID_A: recording(MBID_A)},
        release_rows=[release_row(MBID_A)],
        groups={
            700: ReleaseGroupRow(
                gid="rg-700", name="Gruppe", artist_credit=13, primary_type="Album"
            )
        },
        secondary={700: ["Compilation"]},
    )
    result = lookup_metadata(None, [MBID_A], load_releases=True, load_release_groups=True)

    assert stand_in.calls == [
        "recordings_by_mbids",
        "recording_release_rows",
        "release_counts",
        "release_events",
        "release_groups",
        "release_group_secondary_types",
        "artist_credits",
    ]
    row = result.rows[0]
    assert row["release_group_id"] == "rg-700"
    assert row["release_group_primary_type"] == "Album"
    assert row["release_group_secondary_types"] == ["Compilation"]


def test_all_artist_credits_are_resolved_in_one_call(fake: Any) -> None:
    """Fallstrick 6: ein Batch ueber alle Ebenen, nie pro Aufnahme."""
    stand_in = fake(
        recordings={MBID_A: recording(MBID_A, credit=10), MBID_B: recording(MBID_B, credit=10)},
        release_rows=[release_row(MBID_A), release_row(MBID_B, release_id=501)],
        groups={700: ReleaseGroupRow(gid="rg-700", name="G", artist_credit=13, primary_type=None)},
    )
    lookup_metadata(None, [MBID_A, MBID_B], load_releases=True, load_release_groups=True)

    assert stand_in.calls.count("artist_credits") == 1
    # Aufnahme (10), Track (11), Release (12), Release-Gruppe (13) — einmal je Wert.
    assert stand_in.credit_ids == [10, 11, 12, 13]


def test_the_row_limit_and_view_flag_are_passed_through(fake: Any) -> None:
    stand_in = fake(recordings={MBID_A: recording(MBID_A)}, release_rows=[release_row(MBID_A)])
    lookup_metadata(None, [MBID_A], load_releases=True, row_limit=17, release_event_view=False)

    assert stand_in.row_limit == 17
    assert stand_in.event_view is False


def test_the_truncation_flag_reaches_the_caller(fake: Any) -> None:
    fake(recordings={MBID_A: recording(MBID_A)}, release_rows=[release_row(MBID_A)], truncated=True)
    result = lookup_metadata(None, [MBID_A], load_releases=True)
    assert result.truncated is True


def test_nothing_happens_without_mbids(fake: Any) -> None:
    stand_in = fake()
    assert lookup_metadata(None, []).rows == []
    assert stand_in.calls == []


# --- Die drei bewussten Abweichungen ---------------------------------------


def test_a_recording_without_releases_keeps_its_base_row(fake: Any) -> None:
    """Fallstrick 1: die INNER JOINs der Referenz wuerden sie verschlucken."""
    fake(recordings={MBID_A: recording(MBID_A)}, release_rows=[])
    result = lookup_metadata(None, [MBID_A], load_releases=True, load_release_groups=True)

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["recording_title"] == "Titel 11"
    assert row["release_id"] is None
    assert row["release_events"] == []
    assert row["release_group_id"] is None


def test_missing_side_rows_do_not_raise(fake: Any) -> None:
    """Fallstrick 3: fehlende Nebenzeilen sind kein KeyError."""
    fake(
        recordings={MBID_A: recording(MBID_A)},
        release_rows=[release_row(MBID_A)],
        counts={},  # kein Medium zum Release
        events={},  # keine Veroeffentlichungsereignisse
        groups={},  # Release-Gruppe fehlt
        credits={},  # Artist-Credit fehlt
    )
    result = lookup_metadata(None, [MBID_A], load_releases=True, load_release_groups=True)

    row = result.rows[0]
    assert row["release_medium_count"] is None
    assert row["release_events"] == []
    assert row["release_group_id"] is None
    assert row["recording_artists"] == []
    assert row["track_artists"] == []


def test_artists_carry_the_join_phrase_only_when_set(fake: Any) -> None:
    fake(
        recordings={MBID_A: recording(MBID_A, credit=10)},
        credits={
            10: [
                ArtistRow(gid="artist-1", name="Erste", join_phrase=" & "),
                ArtistRow(gid="artist-2", name="Zweite", join_phrase=""),
            ]
        },
    )
    result = lookup_metadata(None, [MBID_A])
    assert result.rows[0]["recording_artists"] == [
        {"id": "artist-1", "name": "Erste", "joinphrase": " & "},
        {"id": "artist-2", "name": "Zweite"},
    ]
