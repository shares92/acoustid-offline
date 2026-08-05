"""Client-Verhalten ohne echten Server: Header, Fristen, Fehlerumsetzung.

Der Transport wird durch ``httpx.MockTransport`` ersetzt — damit laesst sich
pruefen, was genau auf die Leitung geht und wie der Client auf Antworten
reagiert, die sich real nur schwer herbeifuehren lassen (503, Timeout,
abgerissene Verbindung). Das Zusammenspiel mit dem echten Image prueft
`test_fpindex_integration.py`.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import msgpack
import pytest

from shared.env import EnvSettings
from shared.fpindex import (
    CONTENT_TYPE,
    DEFAULT_INDEX_NAME,
    Delete,
    FpIndexAlreadyExistsError,
    FpIndexBadRequestError,
    FpIndexClient,
    FpIndexNotFoundError,
    FpIndexNotReadyError,
    FpIndexProtocolError,
    FpIndexSearchTimeoutError,
    FpIndexTimeoutError,
    FpIndexTransportError,
    FpIndexVersionMismatchError,
    Insert,
    SearchResult,
)
from shared.fpindex.client import READ_TIMEOUT_MARGIN_S

BASE = "http://acoustid-index:6081"

Handler = Callable[[httpx.Request], httpx.Response]


def pack(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=msgpack.packb(payload, use_bin_type=True),
        headers={"Content-Type": CONTENT_TYPE},
    )


def client_with(handler: Handler, **kwargs: object) -> FpIndexClient:
    transport = httpx.MockTransport(handler)
    return FpIndexClient(
        BASE,
        "main",
        client=httpx.Client(transport=transport),
        **kwargs,  # type: ignore[arg-type]
    )


def recording(response: httpx.Response) -> tuple[list[httpx.Request], Handler]:
    """Handler, der jede Anfrage mitschreibt und immer dasselbe antwortet."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return response

    return seen, handler


# --- Aufbau ----------------------------------------------------------------


def test_from_env_uses_the_bootstrap_variables() -> None:
    env = EnvSettings.from_env(
        {"MMO_INDEX_URL": "http://beispiel:6081/", "MMO_INDEX_NAME": "probe"}
    )
    with FpIndexClient.from_env(env) as client:
        assert client.base_url == "http://beispiel:6081"
        assert client.index_name == "probe"


def test_default_index_name() -> None:
    assert EnvSettings.from_env({}).index_name == DEFAULT_INDEX_NAME


@pytest.mark.parametrize("name", ["", "a.b", "_x", "a/b", "a b"])
def test_invalid_index_names_are_refused_at_construction(name: str) -> None:
    with pytest.raises(ValueError, match="Indexname"):
        FpIndexClient(BASE, name)


def test_repr_names_url_and_index() -> None:
    with FpIndexClient(BASE, "main") as client:
        assert "acoustid-index:6081" in repr(client)
        assert "main" in repr(client)


def test_a_borrowed_client_is_not_closed() -> None:
    """Wer seinen Pool selbst mitbringt, behaelt ihn auch."""
    borrowed = httpx.Client(transport=httpx.MockTransport(lambda _: pack({})))
    with FpIndexClient(BASE, "main", client=borrowed):
        pass
    assert not borrowed.is_closed
    borrowed.close()


# --- Header und Pfade ------------------------------------------------------


def test_every_request_sets_content_type_and_accept() -> None:
    seen, handler = recording(pack({"v": 0, "r": True, "g": 1}))
    with client_with(handler) as client:
        client.ensure_index()
    assert seen[0].headers["content-type"] == CONTENT_TYPE
    assert seen[0].headers["accept"] == CONTENT_TYPE


def test_requests_go_to_the_expected_paths() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("_search"):
            return pack({"r": []})
        if request.url.path.endswith("_update"):
            return pack({"v": 1})
        if request.url.path.endswith("_health"):
            return httpx.Response(200, content=b"OK\n")
        if request.method == "PUT":
            return pack({"v": 0, "r": True, "g": 1})
        return pack({"v": 1, "m": {}, "s": {}})

    with client_with(handler) as client:
        client.ensure_index()
        client.index_info()
        client.index_health()
        client.server_health()
        client.update([Delete(doc_id=1)])
        client.search([16], limit=5)
        client.delete_doc(1)
        client.delete_index()

    assert seen == [
        ("PUT", "/main"),
        ("GET", "/main"),
        ("GET", "/main/_health"),
        ("GET", "/_health"),
        ("POST", "/main/_update"),
        ("POST", "/main/_search"),
        ("DELETE", "/main/1"),
        ("DELETE", "/main"),
    ]


def test_the_search_read_timeout_follows_the_server_deadline() -> None:
    """Der Server soll seine Timeout-Antwort noch loswerden koennen."""
    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return pack({"r": []})

    with client_with(handler, timeout_s=30.0) as client:
        client.search([16], timeout_ms=2000, limit=5)
        client.search([16], timeout_ms=9000, limit=5)
        client.index_health()

    assert seen == [2 + READ_TIMEOUT_MARGIN_S, 9 + READ_TIMEOUT_MARGIN_S, 30.0]


# --- Operationen -----------------------------------------------------------


def test_ensure_index_returns_the_state() -> None:
    with client_with(lambda _: pack({"v": 3, "r": True, "g": 2})) as client:
        state = client.ensure_index()
    assert (state.version, state.ready, state.generation) == (3, True, 2)


def test_ensure_index_passes_the_generation() -> None:
    seen, handler = recording(pack({"v": 0, "r": True, "g": 5}))
    with client_with(handler) as client:
        client.ensure_index(generation=5)
    assert msgpack.unpackb(seen[0].content, raw=False) == {"g": 5}


def test_ensure_index_reports_a_generation_conflict() -> None:
    with (
        client_with(lambda _: pack({"e": "OlderIndexAlreadyExists"}, 409)) as client,
        pytest.raises(FpIndexAlreadyExistsError),
    ):
        client.ensure_index(generation=1)


def test_update_returns_the_new_version() -> None:
    with client_with(lambda _: pack({"v": 42})) as client:
        assert client.update([Insert(doc_id=1, hashes=[16])]) == 42


def test_update_reports_a_version_conflict() -> None:
    with (
        client_with(lambda _: pack({"e": "VersionMismatch"}, 409)) as client,
        pytest.raises(FpIndexVersionMismatchError) as caught,
    ):
        client.update([Delete(doc_id=1)], expected_version=41)
    assert caught.value.status_code == 409
    assert caught.value.error_code == "VersionMismatch"


def test_search_returns_the_candidates() -> None:
    with client_with(lambda _: pack({"r": [{"i": 7, "s": 3}]})) as client:
        assert client.search([16, 32]) == [SearchResult(doc_id=7, score=3)]


def test_search_maps_the_server_timeout_to_its_own_exception() -> None:
    """Serverseitige Frist abgelaufen: HTTP 500 mit `Timeout`."""
    with (
        client_with(lambda _: pack({"e": "Timeout"}, 500)) as client,
        pytest.raises(FpIndexSearchTimeoutError) as caught,
    ):
        client.search([16], timeout_ms=1)
    assert caught.value.status_code == 500


def test_get_metadata_reads_the_attributes() -> None:
    payload = {"v": 9, "m": {"last_fp_id": "104076452"}, "s": {"num_docs": 2}}
    with client_with(lambda _: pack(payload)) as client:
        assert client.get_metadata() == {"last_fp_id": "104076452"}
        assert client.index_info().num_docs == 2


def test_index_health_is_a_bool_for_the_known_states() -> None:
    for status, expected in ((200, True), (404, False), (503, False)):
        with client_with(lambda _, s=status: httpx.Response(s)) as client:
            assert client.index_health() is expected


def test_index_health_raises_on_unexpected_statuses() -> None:
    with (
        client_with(lambda _: pack({"e": "InvalidIndexName"}, 400)) as client,
        pytest.raises(FpIndexBadRequestError),
    ):
        client.index_health()


def test_require_ready_separates_missing_from_loading() -> None:
    with client_with(lambda _: httpx.Response(200, content=b"OK\n")) as client:
        client.require_ready()
    with (
        client_with(lambda _: httpx.Response(404)) as client,
        pytest.raises(FpIndexNotFoundError),
    ):
        client.require_ready()
    with (
        client_with(lambda _: httpx.Response(503)) as client,
        pytest.raises(FpIndexNotReadyError),
    ):
        client.require_ready()


def test_delete_doc_tolerates_a_missing_index_by_default() -> None:
    with client_with(lambda _: pack({"e": "IndexNotFound"}, 404)) as client:
        client.delete_doc(1)
        with pytest.raises(FpIndexNotFoundError):
            client.delete_doc(1, missing_ok=False)


def test_delete_index_tolerates_a_missing_index_by_default() -> None:
    with client_with(lambda _: pack({"e": "IndexNotFound"}, 404)) as client:
        client.delete_index()
        with pytest.raises(FpIndexNotFoundError):
            client.delete_index(missing_ok=False)


def test_metrics_returns_the_prometheus_text() -> None:
    body = b"# TYPE aindex_docs gauge\naindex_docs 2\n"
    with client_with(lambda _: httpx.Response(200, content=body)) as client:
        assert "aindex_docs 2" in client.metrics()


# --- Fehler unterhalb von HTTP ---------------------------------------------


def test_a_client_side_timeout_is_its_own_exception() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("zu langsam")

    with client_with(handler) as client, pytest.raises(FpIndexTimeoutError, match="Frist"):
        client.search([16])


def test_a_refused_connection_is_a_transport_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with client_with(handler) as client, pytest.raises(FpIndexTransportError, match="Antwort"):
        client.index_info()


def test_a_client_timeout_is_a_transport_error_too() -> None:
    """Aufrufer duerfen pauschal auf `FpIndexTransportError` absichern."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("zu langsam")

    with client_with(handler) as client, pytest.raises(FpIndexTransportError):
        client.server_health()


def test_a_broken_body_is_a_protocol_error() -> None:
    with (
        client_with(lambda _: httpx.Response(200, content=b"\xc1\xff\xff")) as client,
        pytest.raises(FpIndexProtocolError),
    ):
        client.index_info()
