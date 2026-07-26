"""Parameter von ``/v2/submit`` — ohne HTTP, ohne Dienste.

Geprueft wird der Vertrag aus docs/research/phase1-api-formate.md, Abschnitt
„/v2/submit": welche Parameter es gibt, welche Pflicht sind, welche mehrfach
kommen duerfen, wo Fehlercodes fallen — und die beiden Eigenheiten, die man
sonst uebersieht: ``fix_meta`` und die **stille Verwerfung** einer
Einreichung ohne jede Zuordnung.
"""

from __future__ import annotations

import pytest
from original_examples import SUBMIT_PARAMETERS

from acoustid_api.errors import (
    InvalidBitrateError,
    InvalidDurationError,
    InvalidFingerprintError,
    InvalidForeignIdError,
    InvalidUuidError,
    MissingParameterError,
    UnknownFormatError,
)
from acoustid_api.params import (
    MAX_SUBMIT_DURATION,
    MAX_TRACK_NUMBER,
    RequestValues,
    normalise_text,
    normalise_track_number,
    parse_submit,
)
from shared.fingerprint import encode_fingerprint

VECTOR = [0x22222220 + index * 16 for index in range(300)]
FINGERPRINT = encode_fingerprint(VECTOR)
MBID = "b81f83ee-4da4-11e0-9ed8-0025225356f3"
OTHER_MBID = "c0a1c0de-4da4-11e0-9ed8-0025225356f3"

BASE = {
    "client": "testkey",
    "user": "usertestkey",
    "fingerprint": FINGERPRINT,
    "duration": "241",
}


def values(*pairs: tuple[str, str], **extra: str) -> RequestValues:
    """Parametersicht aus einzelnen Paaren (Mehrfachnamen inklusive)."""
    return RequestValues([*pairs, *extra.items()])


def parse(**extra: str):
    return parse_submit(values(**{**BASE, **extra}))


# --- Pflichtparameter -------------------------------------------------------


def test_the_minimal_submission_is_accepted() -> None:
    params = parse(mbid=MBID)
    assert params.client == "testkey"
    assert params.user == "usertestkey"
    assert len(params.submissions) == 1
    assert params.submissions[0].duration == 241
    assert params.submissions[0].hashes == tuple(VECTOR)
    assert params.submissions[0].index is None


@pytest.mark.parametrize("missing", ["client", "user", "fingerprint", "duration"])
def test_a_missing_required_parameter_is_error_2(missing: str) -> None:
    remaining = {name: value for name, value in BASE.items() if name != missing}
    with pytest.raises(MissingParameterError) as caught:
        parse_submit(values(**remaining, mbid=MBID))
    assert caught.value.parameter == missing


def test_the_user_key_is_never_validated() -> None:
    """Diese Instanz hat keinen Benutzerbestand — Fehler 6 kann nie kommen."""
    assert parse(user="voellig-frei-erfunden", mbid=MBID).user == "voellig-frei-erfunden"


def test_format_is_read_before_anything_else() -> None:
    with pytest.raises(UnknownFormatError):
        parse_submit(values(format="yaml"))


# --- duration ---------------------------------------------------------------


@pytest.mark.parametrize("duration", ["1", str(MAX_SUBMIT_DURATION)])
def test_the_duration_boundaries_are_accepted(duration: str) -> None:
    assert parse(duration=duration, mbid=MBID).submissions[0].duration == int(duration)


@pytest.mark.parametrize("duration", ["0", "-1", str(MAX_SUBMIT_DURATION + 1)])
def test_a_duration_outside_the_range_is_error_8(duration: str) -> None:
    with pytest.raises(InvalidDurationError) as caught:
        parse(duration=duration, mbid=MBID)
    assert caught.value.parameter == "duration"


def test_an_unreadable_duration_counts_as_missing() -> None:
    """Weiche Lesart des Originals: kein Typfehler, sondern Fehler 2."""
    with pytest.raises(MissingParameterError):
        parse(duration="abc", mbid=MBID)


# --- fingerprint ------------------------------------------------------------


def test_a_broken_fingerprint_is_error_3() -> None:
    with pytest.raises(InvalidFingerprintError):
        parse(fingerprint="!!!keine-base64!!!", mbid=MBID)


def test_a_fingerprint_without_subfingerprints_is_error_3() -> None:
    with pytest.raises(InvalidFingerprintError):
        parse(fingerprint=encode_fingerprint([]), mbid=MBID)


# --- bitrate / fileformat ---------------------------------------------------


def test_a_bitrate_reaches_the_submission() -> None:
    assert parse(mbid=MBID, bitrate="320").submissions[0].bitrate == 320


@pytest.mark.parametrize("bitrate", ["0", "-128"])
def test_a_non_positive_bitrate_is_error_9(bitrate: str) -> None:
    with pytest.raises(InvalidBitrateError) as caught:
        parse(mbid=MBID, bitrate=bitrate)
    assert caught.value.parameter == "bitrate"


def test_an_unreadable_bitrate_counts_as_absent() -> None:
    assert parse(mbid=MBID, bitrate="quatsch").submissions[0].bitrate is None


def test_the_fileformat_is_kept() -> None:
    assert parse(mbid=MBID, fileformat="FLAC").submissions[0].fileformat == "FLAC"


# --- Zuordnungen ------------------------------------------------------------


def test_several_mbids_stay_in_order() -> None:
    """`mbid.N` darf mehrfach kommen — je MBID entsteht spaeter eine Zeile."""
    params = parse_submit(values(("mbid", MBID), ("mbid", OTHER_MBID), **BASE))
    assert params.submissions[0].mbids == (MBID, OTHER_MBID)


def test_a_broken_mbid_is_error_7() -> None:
    with pytest.raises(InvalidUuidError) as caught:
        parse(mbid="keine-uuid")
    assert caught.value.parameter == "mbid"


def test_a_puid_is_accepted_and_validated() -> None:
    assert parse(puid=MBID).submissions[0].puid == MBID
    with pytest.raises(InvalidUuidError) as caught:
        parse(puid="keine-uuid")
    assert caught.value.parameter == "puid"


def test_a_foreign_id_must_look_like_vendor_id() -> None:
    assert parse(foreignid="spotify:4711").submissions[0].foreignid == "spotify:4711"
    with pytest.raises(InvalidForeignIdError) as caught:
        parse(foreignid="ohne-doppelpunkt")
    assert caught.value.parameter == "foreignid"


# --- fix_meta ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Titel  ", "Titel"),
        ("Zwei   Woerter", "Zwei Woerter"),
        ("Zeilen\numbruch", "Zeilen umbruch"),
        ("Tab\tstopp", "Tab stopp"),
        ("   ", None),
        ("", None),
        (None, None),
    ],
)
def test_text_metadata_is_whitespace_normalised(raw: str | None, expected: str | None) -> None:
    assert normalise_text(raw) == expected


def test_all_four_text_fields_go_through_fix_meta() -> None:
    submission = parse(
        mbid=MBID,
        track="  Der   Titel ",
        artist=" Die  Band ",
        album="\tDas Album\n",
        albumartist="  Diverse ",
    ).submissions[0]
    assert (submission.track, submission.artist) == ("Der Titel", "Die Band")
    assert (submission.album, submission.album_artist) == ("Das Album", "Diverse")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), (1, 1), (MAX_TRACK_NUMBER, MAX_TRACK_NUMBER), (MAX_TRACK_NUMBER + 1, None)],
)
def test_absurd_track_numbers_are_dropped(raw: int | None, expected: int | None) -> None:
    assert normalise_track_number(raw) == expected


def test_track_and_disc_number_use_the_same_rule() -> None:
    submission = parse(mbid=MBID, trackno="3", discno="20260722", year="1999").submissions[0]
    assert submission.track_no == 3
    assert submission.disc_no is None
    # `year` bleibt unangetastet — `fix_meta` kappt nur Track- und Disc-Nummer.
    assert submission.year == 1999


# --- Stille Verwerfung ------------------------------------------------------


def test_a_submission_without_any_assignment_is_dropped_silently() -> None:
    params = parse()
    assert params.submissions == ()


@pytest.mark.parametrize(
    "field",
    ["mbid", "puid", "track", "artist", "album", "albumartist", "trackno", "discno", "year"],
)
def test_a_single_piece_of_metadata_keeps_the_submission(field: str) -> None:
    value = MBID if field in ("mbid", "puid") else "1" if field.endswith("no") else "1999"
    assert len(parse(**{field: value}).submissions) == 1


def test_a_foreign_id_alone_keeps_the_submission() -> None:
    """Bewusste Auslegung: `foreignid` ist ein Identifikator wie `puid`."""
    assert len(parse(foreignid="spotify:4711").submissions) == 1


def test_only_the_empty_submission_of_a_pair_is_dropped() -> None:
    params = parse_submit(
        values(
            client="testkey",
            user="usertestkey",
            **{
                "fingerprint.0": FINGERPRINT,
                "duration.0": "241",
                "fingerprint.1": FINGERPRINT,
                "duration.1": "199",
                "mbid.1": MBID,
            },
        )
    )
    assert [item.index for item in params.submissions] == ["1"]


# --- Nummerierte Teilanfragen ----------------------------------------------


def test_the_index_is_a_string_and_follows_the_suffix() -> None:
    params = parse_submit(
        values(
            client="testkey",
            user="usertestkey",
            **{
                "fingerprint.0": FINGERPRINT,
                "duration.0": "241",
                "mbid.0": MBID,
                "fingerprint.7": FINGERPRINT,
                "duration.7": "199",
                "mbid.7": OTHER_MBID,
            },
        )
    )
    assert [item.index for item in params.submissions] == ["0", "7"]
    assert all(isinstance(item.index, str) for item in params.submissions)


def test_a_broken_part_kills_the_whole_request() -> None:
    """Anders als beim Lookup wird beim Submit nichts still uebersprungen."""
    with pytest.raises(MissingParameterError) as caught:
        parse_submit(
            values(
                client="testkey",
                user="usertestkey",
                **{
                    "fingerprint.0": FINGERPRINT,
                    "duration.0": "241",
                    "mbid.0": MBID,
                    "fingerprint.1": FINGERPRINT,
                },
            )
        )
    assert caught.value.parameter == "duration.1"


def test_suffixed_parameters_belong_to_their_own_submission() -> None:
    params = parse_submit(
        values(
            client="testkey",
            user="usertestkey",
            **{
                "fingerprint.0": FINGERPRINT,
                "duration.0": "241",
                "track.0": "Erster",
                "fingerprint.1": FINGERPRINT,
                "duration.1": "199",
                "track.1": "Zweiter",
            },
        )
    )
    assert [item.track for item in params.submissions] == ["Erster", "Zweiter"]


# --- Sonstiges --------------------------------------------------------------


def test_wait_is_parsed_and_ignored() -> None:
    params = parse(mbid=MBID, wait="5")
    assert params.wait == 5
    assert len(params.submissions) == 1


def test_clientversion_is_kept_for_the_log() -> None:
    assert parse(mbid=MBID, clientversion="2.14").client_version == "2.14"


def test_every_documented_parameter_is_understood() -> None:
    """Die Parameterliste des Forschungsberichts, Feld fuer Feld."""
    extra = {
        "duration": "241",
        "fingerprint": FINGERPRINT,
        "bitrate": "320",
        "fileformat": "FLAC",
        "mbid": MBID,
        "track": "Titel",
        "artist": "Band",
        "album": "Album",
        "albumartist": "Diverse",
        "year": "1999",
        "trackno": "4",
        "discno": "1",
        "puid": OTHER_MBID,
        "foreignid": "spotify:4711",
    }
    assert {name for name, _ in SUBMIT_PARAMETERS} == set(extra)
    submission = parse(**extra).submissions[0]
    assert (submission.mbids, submission.puid) == ((MBID,), OTHER_MBID)
    assert (submission.track, submission.year, submission.track_no) == ("Titel", 1999, 4)
    assert (submission.foreignid, submission.fileformat) == ("spotify:4711", "FLAC")


def test_only_mbid_may_appear_more_than_once() -> None:
    """Alle anderen Namen liefern den ersten Wert (Original: `values.get`)."""
    multiple = [name for name, repeated in SUBMIT_PARAMETERS if repeated]
    assert multiple == ["mbid"]
    params = parse_submit(values(("track", "Erster"), ("track", "Zweiter"), **BASE))
    assert params.submissions[0].track == "Erster"
