"""Client fuer den acoustid-index (Phase 5, ARCHITECTURE §5.3).

Der Index ist der Kandidatenlieferant der Matching-Pipeline. Drei Module:

* :mod:`shared.fpindex.query` — :func:`extract_query`, die Umrechnung
  Fingerprint-Vollvektor -> Index-Query. Pure Funktion, keine IO; wird in
  Phase 9 bit-genau gegen die Original-C-Extension geprueft.
* :mod:`shared.fpindex.wire` — msgpack-Draht-Format samt Datenmodellen und
  Fehlerabbildung. Kennt kein HTTP und ist ohne Server testbar.
* :mod:`shared.fpindex.client` — :class:`FpIndexClient`, der synchrone
  Transport darueber.

Typischer Ablauf im Importer::

    from shared.fpindex import FpIndexClient, Insert, extract_query

    with FpIndexClient.from_env() as index:
        index.ensure_index()
        batch = [
            Insert(doc_id=row.id, hashes=extract_query(row.fingerprint, max_hashes=120))
            for row in rows
        ]
        index.update(batch, metadata={"last_fp_id": str(rows[-1].id)})

Bewusst **nicht** in :mod:`shared` re-exportiert: der Waechter braucht
weder httpx noch msgpack und soll sie nicht mitladen (gleiche Regel wie bei
:mod:`shared.db`).
"""

from __future__ import annotations

from shared.fpindex.client import (
    DEFAULT_INDEX_NAME,
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_SEARCH_TIMEOUT_MS,
    FpIndexClient,
)
from shared.fpindex.errors import (
    FpIndexAlreadyExistsError,
    FpIndexBadRequestError,
    FpIndexConflictError,
    FpIndexError,
    FpIndexNotFoundError,
    FpIndexNotReadyError,
    FpIndexProtocolError,
    FpIndexSearchTimeoutError,
    FpIndexServerError,
    FpIndexStatusError,
    FpIndexTimeoutError,
    FpIndexTransportError,
    FpIndexVersionMismatchError,
)
from shared.fpindex.query import (
    HASH_MASK,
    QUERY_START_OFFSET,
    SILENCE_HASH,
    extract_query,
)
from shared.fpindex.wire import (
    CONTENT_TYPE,
    MAX_BODY_BYTES,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_TIMEOUT_MS,
    RECOMMENDED_BATCH_SIZE,
    Change,
    Delete,
    IndexInfo,
    IndexState,
    Insert,
    SearchResult,
)

__all__ = [
    "CONTENT_TYPE",
    "DEFAULT_INDEX_NAME",
    "DEFAULT_SEARCH_LIMIT",
    "DEFAULT_SEARCH_TIMEOUT_MS",
    "HASH_MASK",
    "MAX_BODY_BYTES",
    "MAX_SEARCH_LIMIT",
    "MAX_SEARCH_TIMEOUT_MS",
    "QUERY_START_OFFSET",
    "RECOMMENDED_BATCH_SIZE",
    "SILENCE_HASH",
    "Change",
    "Delete",
    "FpIndexAlreadyExistsError",
    "FpIndexBadRequestError",
    "FpIndexClient",
    "FpIndexConflictError",
    "FpIndexError",
    "FpIndexNotFoundError",
    "FpIndexNotReadyError",
    "FpIndexProtocolError",
    "FpIndexSearchTimeoutError",
    "FpIndexServerError",
    "FpIndexStatusError",
    "FpIndexTimeoutError",
    "FpIndexTransportError",
    "FpIndexVersionMismatchError",
    "IndexInfo",
    "IndexState",
    "Insert",
    "SearchResult",
    "extract_query",
]
