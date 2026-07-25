"""Parameter von ``/v2/lookup`` einlesen — inklusive der weichen Lesarten.

Der Vertrag verlangt an mehreren Stellen ausdruecklich **kein** strenges
Verhalten: ein unlesbares ``duration`` gilt als fehlend, ein unlesbares
``maxdurationdiff`` faellt auf den Standardwert zurueck, und eine kaputte
Teilanfrage im Batch wird still uebersprungen. Wer hier strenger ist, lehnt
Anfragen ab, die api.acoustid.org beantwortet.
"""

from __future__ import annotations

import pytest

from acoustid_api.errors import (
    InvalidFingerprintError,
    InvalidMaxDurationDiffError,
    InvalidUuidError,
    MissingParameterError,
    RequestTooLargeError,
    UnknownFormatError,
)
from acoustid_api.params import (
    DEFAULT_MAX_DURATION_DIFF,
    FingerprintQuery,
    RequestValues,
    TrackQuery,
    iter_suffixes,
    parse_lookup,
)
from shared.fingerprint import encode_fingerprint

VECTOR = [0x11111110 + index * 16 for index in range(200)]
FINGERPRINT = encode_fingerprint(VECTOR)
TRACK_GID = "b81f83ee-4da4-11e0-9ed8-0025225356f3"


def values(**params: str) -> RequestValues:
    return RequestValues(list(params.items()))


def base(**overrides: str) -> RequestValues:
    params = {"client": "testkey", "fingerprint": FINGERPRINT, "duration": "241"}
    params.update(overrides)
    return values(**params)


# --- Quelle der Werte ------------------------------------------------------


def test_query_string_wins_over_the_body() -> None:
    merged = RequestValues([("client", "from-url")], [("client", "from-body")])
    assert merged.get("client") == "from-url"


def test_body_fills_what_the_query_string_lacks() -> None:
    merged = RequestValues([("client", "url")], [("duration", "120")])
    assert merged.get("duration") == "120"


def test_all_values_of_a_name_are_available_in_order() -> None:
    merged = RequestValues([("mbid", "a")], [("mbid", "b"), ("mbid", "c")])
    assert merged.get_all("mbid") == ["a", "b", "c"]


def test_unreadable_numbers_fall_back_to_the_default() -> None:
    merged = values(duration="abc")
    assert merged.get_int("duration", 0) == 0
    assert merged.get_int("fehlt") is None


# --- Suffixe ---------------------------------------------------------------


def test_suffixes_are_sorted_numerically_with_the_bare_name_first() -> None:
    names = ["fingerprint.10", "trackid.2", "fingerprint", "duration.3", "fingerprint.1"]
    assert iter_suffixes(names, "fingerprint", "trackid") == ["", ".1", ".2", ".10"]


def test_non_numeric_suffixes_are_ignored() -> None:
    assert iter_suffixes(["fingerprint.x", "fingerprint.-1"], "fingerprint") == []


# --- Format und Pflichtfelder ----------------------------------------------


def test_unknown_format_is_error_one_and_names_the_value() -> None:
    with pytest.raises(UnknownFormatError, match='unknown format "yaml"'):
        parse_lookup(base(format="yaml"))


def test_format_is_read_before_the_client() -> None:
    """Sonst kaeme die Fehlerantwort im falschen Format heraus."""
    with pytest.raises(UnknownFormatError):
        parse_lookup(values(format="yaml"))


def test_missing_client_is_error_two() -> None:
    with pytest.raises(MissingParameterError, match='"client"'):
        parse_lookup(values(fingerprint=FINGERPRINT, duration="241"))


def test_missing_fingerprint_parameter_is_error_two() -> None:
    with pytest.raises(MissingParameterError, match='"fingerprint"'):
        parse_lookup(values(client="testkey", duration="241"))


def test_missing_duration_is_error_two() -> None:
    with pytest.raises(MissingParameterError, match='"duration"'):
        parse_lookup(values(client="testkey", fingerprint=FINGERPRINT))


def test_unreadable_duration_counts_as_missing() -> None:
    with pytest.raises(MissingParameterError, match='"duration"'):
        parse_lookup(base(duration="dreiminuten"))


def test_duration_zero_counts_as_missing() -> None:
    with pytest.raises(MissingParameterError, match='"duration"'):
        parse_lookup(base(duration="0"))


def test_broken_fingerprint_is_error_three() -> None:
    with pytest.raises(InvalidFingerprintError):
        parse_lookup(base(fingerprint="nicht-base64-!!"))


def test_fingerprint_without_subfingerprints_is_error_three() -> None:
    with pytest.raises(InvalidFingerprintError):
        parse_lookup(base(fingerprint=encode_fingerprint([])))


# --- maxdurationdiff -------------------------------------------------------


def test_max_duration_diff_defaults_to_seven() -> None:
    assert parse_lookup(base()).max_duration_diff == DEFAULT_MAX_DURATION_DIFF


@pytest.mark.parametrize("value", ["1", "7", "30"])
def test_max_duration_diff_accepts_one_to_thirty(value: str) -> None:
    assert parse_lookup(base(maxdurationdiff=value)).max_duration_diff == int(value)


@pytest.mark.parametrize("value", ["0", "31", "-5"])
def test_max_duration_diff_outside_the_range_is_error_eleven(value: str) -> None:
    with pytest.raises(InvalidMaxDurationDiffError, match="between 1 and 30"):
        parse_lookup(base(maxdurationdiff=value))


def test_unreadable_max_duration_diff_falls_back_to_the_default() -> None:
    assert parse_lookup(base(maxdurationdiff="viel")).max_duration_diff == 7


# --- Teilanfragen ----------------------------------------------------------


def test_a_single_fingerprint_query_is_parsed() -> None:
    params = parse_lookup(base())
    assert len(params.queries) == 1
    query = params.queries[0]
    assert isinstance(query, FingerprintQuery)
    assert query.index is None
    assert query.duration == 241
    assert list(query.hashes) == VECTOR


def test_trackid_beats_fingerprint_in_the_same_slot() -> None:
    params = parse_lookup(base(trackid=TRACK_GID))
    assert params.queries == (TrackQuery(index=None, track_gid=TRACK_GID),)


def test_invalid_trackid_is_error_seven() -> None:
    with pytest.raises(InvalidUuidError, match='"trackid"'):
        parse_lookup(base(trackid="keine-uuid"))


def test_without_batch_only_the_first_query_is_answered() -> None:
    params = parse_lookup(
        values(
            client="testkey",
            **{
                "fingerprint.0": FINGERPRINT,
                "duration.0": "241",
                "fingerprint.1": FINGERPRINT,
                "duration.1": "300",
            },
        )
    )
    assert len(params.queries) == 2
    assert len(params.selected()) == 1
    assert params.selected()[0].index == 0


def test_batch_answers_every_query() -> None:
    params = parse_lookup(
        values(
            client="testkey",
            batch="1",
            **{
                "fingerprint.0": FINGERPRINT,
                "duration.0": "241",
                "fingerprint.5": FINGERPRINT,
                "duration.5": "300",
            },
        )
    )
    assert params.batch is True
    assert [query.index for query in params.selected()] == [0, 5]


def test_batch_zero_is_no_batch() -> None:
    assert parse_lookup(base(batch="0")).batch is False


def test_a_broken_part_is_skipped_while_a_good_one_remains() -> None:
    params = parse_lookup(
        values(
            client="testkey",
            batch="1",
            **{
                "fingerprint.0": "kaputt!!",
                "duration.0": "241",
                "fingerprint.1": FINGERPRINT,
                "duration.1": "300",
            },
        )
    )
    assert [query.index for query in params.queries] == [1]


def test_only_broken_parts_raise_the_last_error() -> None:
    with pytest.raises(InvalidFingerprintError):
        parse_lookup(
            values(
                client="testkey",
                batch="1",
                **{
                    "fingerprint.0": "kaputt!!",
                    "duration.0": "241",
                    "fingerprint.1": "auch-kaputt!!",
                    "duration.1": "300",
                },
            )
        )


# --- Limits ----------------------------------------------------------------


def _many(count: int, prefix: str = "fingerprint", *, batch: bool = True) -> RequestValues:
    params: dict[str, str] = {"client": "testkey"}
    if batch:
        params["batch"] = "1"
    for index in range(count):
        if prefix == "fingerprint":
            params[f"fingerprint.{index}"] = FINGERPRINT
            params[f"duration.{index}"] = "241"
        else:
            params[f"trackid.{index}"] = TRACK_GID
    return values(**params)


def test_twenty_fingerprint_queries_are_allowed() -> None:
    assert len(parse_lookup(_many(20)).selected()) == 20


def test_twentyone_fingerprint_queries_are_error_nineteen() -> None:
    with pytest.raises(RequestTooLargeError):
        parse_lookup(_many(21))


def test_hundred_track_queries_are_allowed() -> None:
    assert len(parse_lookup(_many(100, "trackid")).selected()) == 100


def test_hundredone_track_queries_are_error_nineteen() -> None:
    with pytest.raises(RequestTooLargeError):
        parse_lookup(_many(101, "trackid"))


def test_the_limit_counts_only_the_answered_queries() -> None:
    """Ohne ``batch`` zaehlt nur die erste Teilanfrage — wie im Original."""
    assert len(parse_lookup(_many(50, batch=False)).selected()) == 1


# --- meta ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ()),
        ("", ()),
        ("0", ()),
        ("1", ("recordingids",)),
        ("2", ("m2",)),
        ("recordings releasegroups compress", ("recordings", "releasegroups", "compress")),
    ],
)
def test_meta_is_split_like_the_original(raw: str | None, expected: tuple[str, ...]) -> None:
    params = parse_lookup(base(**({"meta": raw} if raw is not None else {})))
    assert params.meta == expected


def test_clientversion_is_optional_and_kept() -> None:
    assert parse_lookup(base(clientversion="2.14")).client_version == "2.14"
    assert parse_lookup(base()).client_version is None
