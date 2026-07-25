"""Draht-Format: Kurzfeldnamen, Header, Fehlerabbildung — ohne Server.

Die erwarteten Bytes stammen aus einer empirischen Sitzung gegen das
gepinnte Image (Phase 5). Faellt hier etwas um, hat sich entweder unser
Client oder das Protokoll geaendert — beides muss auffallen, bevor es der
Integrationstest merkt.
"""

from __future__ import annotations

import msgpack
import pytest

from shared.fpindex import (
    CONTENT_TYPE,
    MAX_BODY_BYTES,
    Delete,
    Insert,
    SearchResult,
)
from shared.fpindex.errors import (
    FpIndexAlreadyExistsError,
    FpIndexBadRequestError,
    FpIndexConflictError,
    FpIndexNotFoundError,
    FpIndexNotReadyError,
    FpIndexProtocolError,
    FpIndexSearchTimeoutError,
    FpIndexServerError,
    FpIndexVersionMismatchError,
)
from shared.fpindex.wire import (
    HEADERS,
    MAX_SEARCH_TIMEOUT_MS,
    WireRequest,
    decode_body,
    delete_doc_request,
    delete_index_request,
    get_index_request,
    index_health_request,
    parse_index_info,
    parse_index_state,
    parse_search,
    parse_version,
    put_index_request,
    search_request,
    server_health_request,
    status_error,
    update_request,
    valid_index_name,
)


def unpack(body: bytes | None) -> object:
    assert body is not None
    return msgpack.unpackb(body, raw=False)


# --- Header ----------------------------------------------------------------


def test_both_content_headers_are_set_explicitly() -> None:
    """Die Content-Type-Falle: ohne Header nimmt der Server msgpack an."""
    assert HEADERS["Content-Type"] == CONTENT_TYPE
    assert HEADERS["Accept"] == CONTENT_TYPE
    assert CONTENT_TYPE == "application/vnd.msgpack"


# --- Anfragen --------------------------------------------------------------


def test_put_index_is_empty_by_default() -> None:
    request = put_index_request("main")
    assert (request.method, request.path) == ("PUT", "/main")
    assert unpack(request.body) == {}


def test_put_index_uses_short_field_names() -> None:
    assert unpack(put_index_request("main", expect_does_not_exist=True).body) == {"e": True}
    assert unpack(put_index_request("main", generation=7).body) == {"g": 7}


def test_read_requests_carry_no_body() -> None:
    assert get_index_request("main") == WireRequest("GET", "/main", None)
    assert index_health_request("main") == WireRequest("GET", "/main/_health", None)
    assert server_health_request() == WireRequest("GET", "/_health", None)
    assert delete_index_request("main") == WireRequest("DELETE", "/main", None)
    assert delete_doc_request("main", 42) == WireRequest("DELETE", "/main/42", None)


def test_update_uses_short_field_names() -> None:
    request = update_request(
        "main",
        [Insert(doc_id=1, hashes=[16, 32]), Delete(doc_id=2)],
        metadata={"last_fp_id": "1"},
        expected_version=41,
    )
    assert (request.method, request.path) == ("POST", "/main/_update")
    assert unpack(request.body) == {
        "c": [{"i": {"i": 1, "h": [16, 32]}}, {"d": {"i": 2}}],
        "m": {"last_fp_id": "1"},
        "e": 41,
    }


def test_update_without_options_sends_only_changes() -> None:
    """`m`/`e` weglassen heisst „nicht anfassen" — nicht „leer setzen"."""
    assert unpack(update_request("main", [Delete(doc_id=9)]).body) == {"c": [{"d": {"i": 9}}]}


def test_update_may_carry_metadata_only() -> None:
    """Ein Batch ohne Changes ist zulaessig (empirisch: erhoeht die Version)."""
    body = unpack(update_request("main", [], metadata={"k": "v"}).body)
    assert body == {"c": [], "m": {"k": "v"}}


def test_update_rejects_foreign_change_types() -> None:
    with pytest.raises(TypeError, match="Change-Typ"):
        update_request("main", [{"insert": {"id": 1}}])  # type: ignore[list-item]


def test_search_uses_short_field_names() -> None:
    request = search_request("main", [16, 32], timeout_ms=2000, limit=40)
    assert (request.method, request.path) == ("POST", "/main/_search")
    assert unpack(request.body) == {"q": [16, 32], "t": 2000, "l": 40}


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_search_limit_is_checked_before_sending(limit: int) -> None:
    """Der Server kappt still auf 100 — wir sagen es lieber vorher."""
    with pytest.raises(ValueError, match="limit"):
        search_request("main", [16], timeout_ms=500, limit=limit)


@pytest.mark.parametrize("timeout_ms", [0, -1, 10_001, 60_000])
def test_search_timeout_must_stay_inside_the_documented_range(timeout_ms: int) -> None:
    """Der Server deckelt still auf 10 s — lieber eine klare Absage."""
    with pytest.raises(ValueError, match="timeout_ms"):
        search_request("main", [16], timeout_ms=timeout_ms, limit=10)


@pytest.mark.parametrize("timeout_ms", [1, 2000, MAX_SEARCH_TIMEOUT_MS])
def test_search_timeout_inside_the_range_is_accepted(timeout_ms: int) -> None:
    assert search_request("main", [16], timeout_ms=timeout_ms, limit=10).body is not None


# --- Wertebereiche ---------------------------------------------------------


@pytest.mark.parametrize("value", [-1, -2076182035, 2**32, 2**40])
def test_hashes_outside_u32_are_rejected_with_the_culprit(value: int) -> None:
    """Sonst antwortet der Server mit IntegerOverflow fuer den ganzen Batch."""
    with pytest.raises(ValueError, match=str(value)):
        update_request("main", [Insert(doc_id=5, hashes=[16, value])])
    with pytest.raises(ValueError, match=str(value)):
        search_request("main", [16, value], timeout_ms=500, limit=10)


def test_the_error_names_the_document() -> None:
    with pytest.raises(ValueError, match="Dokument 5"):
        update_request("main", [Insert(doc_id=5, hashes=[-1])])


@pytest.mark.parametrize("doc_id", [-1, 2**64])
def test_document_ids_outside_u64_are_rejected(doc_id: int) -> None:
    with pytest.raises(ValueError, match="ausserhalb von u64"):
        update_request("main", [Delete(doc_id=doc_id)])
    with pytest.raises(ValueError, match="ausserhalb von u64"):
        delete_doc_request("main", doc_id)


def test_u32_boundaries_are_accepted() -> None:
    request = update_request("main", [Insert(doc_id=0, hashes=[0, 0xFFFFFFFF])])
    assert unpack(request.body) == {"c": [{"i": {"i": 0, "h": [0, 0xFFFFFFFF]}}]}


def test_a_body_above_sixteen_mib_is_refused_before_sending() -> None:
    """16 MiB ist die harte Grenze des Servers (`max_body_size`)."""
    huge = [Insert(doc_id=n, hashes=list(range(0, 120 * 16, 16))) for n in range(60_000)]
    with pytest.raises(ValueError, match=str(MAX_BODY_BYTES)):
        update_request("main", huge)


# --- Indexnamen ------------------------------------------------------------


@pytest.mark.parametrize("name", ["main", "test_1", "Test-2", "1", "a" * 80])
def test_valid_index_names(name: str) -> None:
    assert valid_index_name(name)


@pytest.mark.parametrize("name", ["", "a.b", "a/b", "_leading", "-leading", "a b", "aeß"])
def test_invalid_index_names(name: str) -> None:
    """Genau die Namen, die der Server empirisch mit HTTP 400 ablehnt."""
    assert not valid_index_name(name)


# --- Antworten -------------------------------------------------------------


def test_decode_body_handles_the_empty_body() -> None:
    assert decode_body(b"") is None


def test_decode_body_rejects_garbage() -> None:
    with pytest.raises(FpIndexProtocolError, match="msgpack"):
        decode_body(b"\xc1\xff\xff")


def test_parse_index_state() -> None:
    state = parse_index_state({"v": 0, "r": True, "g": 1})
    assert (state.version, state.ready, state.generation) == (0, True, 1)


def test_parse_index_info_keeps_the_long_stats_names() -> None:
    """Die Statistik ist die einzige Struktur ohne Kurzfeldnamen."""
    info = parse_index_info(
        {
            "v": 4,
            "m": {"last_fp_id": "104076452"},
            "s": {"min_doc_id": 1, "max_doc_id": 9, "num_segments": 4, "num_docs": 4},
        }
    )
    assert info.version == 4
    assert info.metadata == {"last_fp_id": "104076452"}
    assert info.num_docs == 4


def test_parse_index_info_tolerates_missing_metadata() -> None:
    info = parse_index_info({"v": 0, "m": {}, "s": {}})
    assert info.metadata == {}
    assert info.num_docs is None


def test_parse_version() -> None:
    assert parse_version({"v": 42}) == 42


def test_parse_search() -> None:
    assert parse_search({"r": [{"i": 7, "s": 3}, {"i": 8, "s": 1}]}) == [
        SearchResult(doc_id=7, score=3),
        SearchResult(doc_id=8, score=1),
    ]


def test_parse_search_without_hits() -> None:
    assert parse_search({"r": []}) == []


@pytest.mark.parametrize(
    "payload",
    [None, [], {"v": "keine zahl"}, {"nix": 1}, {"v": True}],
)
def test_unexpected_structures_raise_a_protocol_error(payload: object) -> None:
    with pytest.raises(FpIndexProtocolError):
        parse_version(payload)


def test_search_with_a_broken_result_list() -> None:
    with pytest.raises(FpIndexProtocolError):
        parse_search({"r": "keine liste"})
    with pytest.raises(FpIndexProtocolError):
        parse_search({"r": [{"i": 1}]})


# --- Fehlerabbildung -------------------------------------------------------


def error_body(code: str) -> bytes:
    return msgpack.packb({"e": code}, use_bin_type=True)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "InvalidFormat", FpIndexBadRequestError),
        (400, "MissingStructFields", FpIndexBadRequestError),
        (400, "UnknownStructField", FpIndexBadRequestError),
        (400, "UnknownUnionField", FpIndexBadRequestError),
        (400, "IntegerOverflow", FpIndexBadRequestError),
        (400, "InvalidIndexName", FpIndexBadRequestError),
        (400, "InvalidCharacter", FpIndexBadRequestError),
        (404, "IndexNotFound", FpIndexNotFoundError),
        (409, "VersionMismatch", FpIndexVersionMismatchError),
        (409, "IndexAlreadyExists", FpIndexAlreadyExistsError),
        (409, "OlderIndexAlreadyExists", FpIndexAlreadyExistsError),
        (409, "NewerIndexAlreadyExists", FpIndexAlreadyExistsError),
        (409, "IndexBeingDeleted", FpIndexConflictError),
        (500, "Timeout", FpIndexSearchTimeoutError),
        (500, "SonstWas", FpIndexServerError),
        (503, "IndexNotReady", FpIndexNotReadyError),
    ],
)
def test_status_and_code_pick_the_exception(
    status: int, code: str, expected: type[Exception]
) -> None:
    error = status_error(status, error_body(code))
    assert type(error) is expected
    assert error.status_code == status
    assert error.error_code == code
    assert code in str(error) or status == 500


def test_version_mismatch_is_a_conflict() -> None:
    """Aufrufer duerfen grob (Conflict) oder genau (VersionMismatch) fangen."""
    error = status_error(409, error_body("VersionMismatch"))
    assert isinstance(error, FpIndexConflictError)


def test_search_timeout_is_a_server_error() -> None:
    error = status_error(500, error_body("Timeout"))
    assert isinstance(error, FpIndexServerError)
    assert "Teilergebnis" in str(error)


def test_an_empty_error_body_is_tolerated() -> None:
    """`GET /:index/_health` antwortet mit einer nackten 404 ohne Rumpf."""
    error = status_error(404, b"")
    assert isinstance(error, FpIndexNotFoundError)
    assert error.error_code is None


def test_a_non_msgpack_error_body_is_tolerated() -> None:
    error = status_error(400, b'{"error":"InvalidFormat"}')
    assert isinstance(error, FpIndexBadRequestError)
