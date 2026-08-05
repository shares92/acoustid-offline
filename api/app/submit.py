"""``/v2/submit`` — eigene Einreichungen speichern und indexieren (Phase 11).

Der Endpunkt nimmt entgegen, was ein Tagging-Client ueber eine Datei weiss:
Fingerprint, Laenge und eine Zuordnung (MusicBrainz-Recording, PUID,
Fremd-ID oder schlicht Textmetadaten). Was daraus wird, entscheidet
``acoustid.submit.mode`` (ARCHITECTURE §6):

======================  ====================================================
``off``                 Der Endpunkt nimmt nichts an: Fehler 12
                        („not allowed") / HTTP 400, noch bevor Parameter
                        gelesen werden.
``local``               Speichern in ``local_submission`` und indexieren.
``local+upstream``      Zusaetzlich weiterleiten an api.acoustid.org
                        (:mod:`acoustid_api.upstream`, Phase 12) — als
                        Zugabe, nie als Bedingung.
======================  ====================================================

**Warum ein reservierter Dokument-ID-Bereich.** Eigene Einreichungen duerfen
nicht in ``track``/``fingerprint`` stehen (der Delta-Importer wuerde sie
ueberschreiben, ARCHITECTURE §5.2). Auffindbar sind sie trotzdem, weil sie im
Suchindex Dokument-IDs ab :data:`~acoustid_api.store.LOCAL_DOC_ID_BASE`
belegen — oberhalb von allem, was ``fingerprint.id`` je annehmen kann. Der
Lookup erkennt sie an dieser Grenze wieder (:mod:`acoustid_api.store`).

**Statusmaschine.** ``new`` -> ``indexed`` (hier) -> ``forwarded`` |
``forward_failed`` (:mod:`acoustid_api.upstream`). Der Weg von ``new`` nach
``indexed`` laeuft **synchron in der Anfrage**, aber in der richtigen
Reihenfolge: erst das ``_update`` des Index, dann der Statuswechsel. Bricht
etwas dazwischen ab, bleibt die Einreichung ``new`` und wird beim naechsten
Submit nachgetragen — dieselbe Resume-Denke wie beim Index-Feed des
Importers (§8.4). Die Weiterleitung setzt danach auf: sie fasst nur an, was
schon indexiert ist.

**Die Antwort ist immer ``pending``** — auch dann, wenn schon alles
indexiert ist. Das ist keine Nachlaessigkeit, sondern der Vertrag: das
Original verarbeitet Einreichungen asynchron und liefert nie einen anderen
Wert. Deshalb ist auch ein nicht erreichbarer Suchindex **kein** Fehler
dieser Anfrage: gespeichert ist gespeichert, und „pending" beschreibt die
Lage dann sogar praeziser als jeder Fehlercode. Ein 503 wuerde Picard und
beets bloss zum Wiederholen bringen — und damit Dubletten erzeugen.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import psycopg

from acoustid_api.errors import NotAllowedError
from acoustid_api.params import SubmitParams
from acoustid_api.store import (
    LOCAL_DOC_ID_BASE,
    StoredSubmission,
    load_pending_submissions,
    mark_submissions_indexed,
    store_submission,
)
from acoustid_api.upstream import forward_after_submit
from shared.config import Config
from shared.fpindex import FpIndexError, Insert, extract_query
from shared.models import SubmitMode

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from acoustid_api.service import ApiService

__all__ = [
    "MAX_INDEX_BATCH",
    "SUBMISSION_PENDING",
    "SUBMISSION_STORED_EVENT",
    "check_mode",
    "handle_submit",
    "index_pending",
]

_LOG = logging.getLogger(__name__)

#: Der einzige Wert, den ``status`` je Eintrag in ``submissions[]`` annimmt.
#: **Nicht** zu verwechseln mit :class:`shared.models.SubmissionStatus`: das
#: ist die Statusmaschine in der Datenbank, dies hier ist der Vertrag nach
#: aussen — er kennt nur diesen einen Wert.
SUBMISSION_PENDING: Final = "pending"

#: Hoechstzahl Einreichungen, die **eine** Anfrage in den Suchindex
#: nachtraegt. Im Normalbetrieb steht dort genau die gerade gespeicherte;
#: die Grenze greift nur, wenn der Index laenger weg war und sich ein Rueckstand
#: gebildet hat — dann soll eine einzelne Anfrage nicht beliebig lange laufen.
MAX_INDEX_BATCH: Final = 200

#: Ereignisname im Log, sobald eine Einreichung gespeichert ist. Der Waechter
#: braucht ihn ab Phase 17, um seinen Lookup-Cache zu verwerfen (Invariante
#: §8.6) — hier wird er nur **geloggt**, nicht ausgewertet.
SUBMISSION_STORED_EVENT: Final = "local_submission_stored"


def check_mode(config: Config) -> None:
    """Prueft, ob der Endpunkt ueberhaupt annimmt.

    Wird vor dem Parsen aufgerufen: im Modus ``off`` soll eine Anfrage nicht
    erst hundert Fingerprints dekodieren, um dann abgelehnt zu werden.

    Raises:
        NotAllowedError: ``acoustid.submit.mode`` ist ``off`` (Fehler 12 / HTTP 400).
    """
    if config.acoustid.submit.mode is SubmitMode.OFF:
        raise NotAllowedError()


def handle_submit(
    connection: psycopg.Connection, service: ApiService, params: SubmitParams
) -> dict[str, Any]:
    """Beantwortet einen Submit (ohne den ``status``-Schluessel).

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        service: Laufzeitumgebung (Index-Client, Config).
        params: Geprueftes Ergebnis von :func:`acoustid_api.params.parse_submit`.

    Returns:
        ``{"submissions": [...]}`` — die Huelle mit ``status`` setzt die
        HTTP-Schicht.
    """
    stored: list[StoredSubmission] = []
    if params.submissions:
        # Eine Anfrage, eine Transaktion: entweder alle Teilanfragen stehen in
        # der Datenbank oder keine.
        with connection.transaction():
            stored = [
                store_submission(
                    connection,
                    submission,
                    client=params.client,
                    client_version=params.client_version,
                    user=params.user,
                )
                for submission in params.submissions
            ]
        _log_stored(params, stored)
        index_pending(connection, service)
        # Nur im Modus `local+upstream`, und nur fuer das, was diese Anfrage
        # gespeichert hat. Wirft nie — die Antwort haengt nicht daran.
        forward_after_submit(connection, service, [item.local_track_id for item in stored])

    return {"submissions": _response(params, stored)}


def index_pending(
    connection: psycopg.Connection, service: ApiService, *, limit: int = MAX_INDEX_BATCH
) -> int:
    """Traegt Einreichungen im Status ``new`` in den Suchindex nach.

    Reihenfolge wie beim Index-Feed des Importers: erst ``_update``, dann der
    Statuswechsel. Scheitert der Index, bleibt alles ``new`` — die naechste
    Anfrage versucht es erneut, und der Aufrufer erfaehrt davon nichts (die
    Antwort ist ohnehin ``pending``).

    Bewusst **ohne** ``expected_version``: anders als der Importer ist die API
    nicht der einzige Schreiber am Index, und ein gleichzeitig laufender
    Import wuerde jede Einreichung scheitern lassen. Die Idempotenz kommt
    stattdessen aus dem Inhalt — dieselbe Dokument-ID mit denselben Hashes zu
    schicken, ist folgenlos.

    Returns:
        Zahl der abgearbeiteten Einreichungen (0, wenn der Index nicht
        mitspielte oder nichts offen war).
    """
    pending = load_pending_submissions(connection, limit=limit)
    if not pending:
        return 0

    changes: list[Insert] = []
    handled: list[int] = []
    empty = 0
    for item in pending:
        handled.append(item.local_track_id)
        query = extract_query(item.hashes, max_hashes=service.config.acoustid.index.query_hashes)
        if not query:
            # Nur Stille: es gibt nichts zu indexieren. Die Einreichung bleibt
            # trotzdem nicht ewig im Arbeitsvorrat liegen (wie beim Importer).
            empty += 1
            continue
        changes.append(Insert(doc_id=LOCAL_DOC_ID_BASE + item.local_track_id, hashes=query))

    if changes:
        try:
            version = service.index.update(changes)
        except FpIndexError as exc:
            _LOG.warning(
                "Lokale Einreichungen bleiben unindexiert — Suchindex nicht erreichbar",
                extra={"pending": len(pending), "error": str(exc)},
            )
            return 0
        _LOG.info(
            "Lokale Einreichungen indexiert",
            extra={
                "documents": len(changes),
                "empty_queries": empty,
                "index_version": version,
            },
        )

    mark_submissions_indexed(connection, handled)
    return len(handled)


def _response(params: SubmitParams, stored: list[StoredSubmission]) -> list[dict[str, Any]]:
    """Die ``submissions[]``-Liste der Antwort.

    Je gespeicherter Zeile ein Eintrag; ``index`` traegt die Nummer aus dem
    Parameter-Suffix **als Zeichenkette** und fehlt ganz, wenn der Client ohne
    Suffix eingereicht hat (Eigenheit des Originals, dessen Doku an dieser
    Stelle eine Zahl zeigt).
    """
    entries: list[dict[str, Any]] = []
    for submission, result in zip(params.submissions, stored, strict=True):
        for row_id in result.row_ids:
            entry: dict[str, Any] = {"id": row_id, "status": SUBMISSION_PENDING}
            if submission.index is not None:
                entry["index"] = submission.index
            entries.append(entry)
    return entries


def _log_stored(params: SubmitParams, stored: list[StoredSubmission]) -> None:
    """Ein strukturiertes Ereignis je Anfrage (Grundlage der Cache-Invalidierung).

    Der Waechter verwirft ab Phase 17 nach **jeder** lokalen Submission
    seinen Lookup-Cache (Invariante §8.6). Damit er das kann, muss das
    Ereignis maschinenlesbar im Log stehen — mit den IDs, aber ohne die
    Nutzdaten.
    """
    _LOG.info(
        "Lokale Submission gespeichert",
        extra={
            "event": SUBMISSION_STORED_EVENT,
            "submissions": sum(len(item.row_ids) for item in stored),
            "recordings": len(stored),
            "submission_ids": [row_id for item in stored for row_id in item.row_ids][:20],
            "acoustids": [str(item.local_track_gid) for item in stored][:20],
            "client": params.client,
            "client_version": params.client_version,
        },
    )
