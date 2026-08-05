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
from datetime import UTC, datetime
from pathlib import Path
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
    "INDEX_BUSY_FILENAME",
    "INDEX_BUSY_MAX_AGE_S",
    "MAX_INDEX_BATCH",
    "SUBMISSION_PENDING",
    "SUBMISSION_STORED_EVENT",
    "check_mode",
    "handle_submit",
    "index_deferred",
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

#: Marke im Datenverzeichnis, mit der der Waechter einen laufenden
#: Delta-Import anzeigt (M2.5, Betreiber-Entscheid 2026-08-05). Sie liegt
#: neben der ``config.yaml`` auf dem ``/config``-Mount — demselben Weg, den
#: schon die Reload-Marke nimmt (:mod:`acoustid_watchdog.reload`).
#:
#: **Warum ueberhaupt.** Der Index-Feed des Importers sichert jeden Batch
#: mit ``expected_version`` ab (§5.3, Idempotenz gegen einen zweiten
#: Schreiber). Eine Einreichung, die waehrenddessen indexiert wird, erhoeht
#: die Version — und der laufende Import bricht ab. Der Betreiber-Entscheid
#: stellt deshalb die **Indexierung** zurueck: die Einreichung wird
#: angenommen und gespeichert (Status ``new``), nur eben spaeter sichtbar.
#: Die Antwort bleibt ``pending``, also unveraendert (docs/api-submit.md).
#:
#: Der Name steht auch in :mod:`acoustid_watchdog.jobs`; ein Test haelt
#: beide aneinander (der Waechter haengt bewusst nicht vom API-Paket ab).
INDEX_BUSY_FILENAME: Final = "index-feed.busy"

#: Hoechstalter der Marke — danach gilt sie als **verwaist** (F7).
#:
#: Stirbt der Waechter mit ``SIGKILL``, laeuft kein ``finally``, und die
#: Marke bliebe fuer immer liegen: eigene Einreichungen waeren dauerhaft
#: gespeichert, aber im Index unauffindbar (und die Upstream-Queue staende
#: mit still). Der Setzzeitpunkt steht in der Datei.
#:
#: **24 Stunden und nicht weniger:** ein Bootstrap-Feed laeuft Stunden bis
#: Tage (414 GB gz, §5.1). Ein knapperer Wert erklaerte einen ehrlich
#: laufenden Import fuer tot und oeffnete genau das Fenster, das die Marke
#: schliessen soll. Derselbe Wert steht in :mod:`acoustid_watchdog.jobs`;
#: ein Test haelt beide aneinander.
INDEX_BUSY_MAX_AGE_S: Final = 24 * 3600.0


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


def index_deferred(service: ApiService) -> bool:
    """Laeuft gerade ein Delta-Import? (Marke :data:`INDEX_BUSY_FILENAME`.)

    Der Betreiber-Entscheid vom 2026-08-05 in einer Funktion: waehrend des
    Update-Laufs wird **nicht** indexiert. Einreichungen werden weiterhin
    angenommen und gespeichert; die Antwort ist ohnehin ``pending``.

    **Eine verwaiste Marke laeuft ab** (:data:`INDEX_BUSY_MAX_AGE_S`).
    Stirbt der Waechter mit ``SIGKILL``, laeuft kein ``finally``, und die
    Marke bliebe fuer immer liegen — eigene Einreichungen waeren dauerhaft
    gespeichert, aber im Index unauffindbar, und die Upstream-Queue staende
    mit still. Der Setzzeitpunkt steht in der Datei; ist er alt genug,
    gilt die Marke als tot und wird **mit Warnung** uebergangen.
    """
    marker = service.data_dir / INDEX_BUSY_FILENAME
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    age_s = _marker_age_s(raw, marker)
    if age_s is None or age_s <= INDEX_BUSY_MAX_AGE_S:
        return True
    _LOG.warning(
        "Import-Marke ist ueberaltert — sie wird uebergangen und die Einreichung indexiert",
        extra={
            "marker_path": str(marker),
            "age_s": round(age_s),
            "max_age_s": INDEX_BUSY_MAX_AGE_S,
        },
    )
    return False


def _marker_age_s(raw: str, marker: Path) -> float | None:
    """Alter der Marke in Sekunden — ``None``, wenn nicht bestimmbar.

    Gelesen wird der **Inhalt** (der Setzzeitpunkt), nicht die mtime: die
    Datei liegt auf einem gemounteten Dateisystem, und ein ``touch`` waere
    dort keine Aussage ueber den Lauf. Ist der Inhalt unverstaendlich,
    faellt die Antwort auf die mtime zurueck — und im Zweifel auf „noch
    gueltig": eine faelschlich fuer tot erklaerte Marke ist der teurere
    Fehler (sie schuetzt einen laufenden Index-Feed).
    """
    try:
        written = datetime.fromisoformat(raw)
    except ValueError:
        try:
            written = datetime.fromtimestamp(marker.stat().st_mtime, tz=UTC)
        except OSError:  # pragma: no cover - die Datei war eben noch da
            return None
    if written.tzinfo is None:  # pragma: no cover - der Waechter schreibt eine Zone
        written = written.replace(tzinfo=UTC)
    return (datetime.now(UTC) - written).total_seconds()


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

    **Waehrend eines Delta-Imports wird zurueckgestellt** (M2.5,
    Betreiber-Entscheid 2026-08-05, :func:`index_deferred`): jede
    Indexierung erhoehte die Version, und der Index-Feed des Importers
    braeche an seinem ``expected_version``-Guard ab — der Lauf endete als
    Fehler und kostete einen Tag Datenstand. Die Einreichung bleibt
    ``new``; nachgetragen wird sie direkt nach dem Lauf
    (:mod:`acoustid_api.queuejob`) oder beim naechsten Submit.

    Returns:
        Zahl der abgearbeiteten Einreichungen (0, wenn der Index nicht
        mitspielte, nichts offen war oder gerade importiert wird).
    """
    if index_deferred(service):
        _LOG.info(
            "Indexierung zurueckgestellt — es laeuft ein Delta-Import",
            extra={"marker": INDEX_BUSY_FILENAME},
        )
        return 0

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
