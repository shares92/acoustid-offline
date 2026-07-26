"""``GET/POST /v2/submission_status`` — was wurde aus meiner Einreichung? (Phase 13)

Der kleine Bruder von ``/v2/submit``: ein Client, der eingereicht hat, bekam
Submission-IDs und die Auskunft ``"pending"``; hier fragt er spaeter nach, ob
daraus etwas geworden ist. Der Endpunkt heisst **``/v2/submission_status``**
(nicht ``/v2/submit/status`` — Handoff-Korrektur aus der Phase-1-Recherche)
und kennt genau vier Parameter: ``format``, ``client``, ``clientversion`` und
**mehrfaches** ``id``. Ein ``user`` kommt bewusst nicht vor.

Antwort — je angefragter ID ein Eintrag, in Anfragereihenfolge::

    {
        "status": "ok",
        "submissions": [
            {"id": 17, "status": "imported", "result": {"id": "<acoustid>"}},
            {"id": 18, "status": "pending"},
        ],
    }

**Unbekannte IDs sind kein Fehler.** Sie werden still ``"pending"``
beantwortet — nie 404, nie Fehler 18. Das ist der Vertrag des Originals, und
er ist auch der sinnvollere: eine Instanz, die auf jede fremde ID mit 404
antwortet, verraet, welche IDs es gibt.

**Abbildung auf unsere Statusmaschine.** ``local_submission.status`` hat vier
Werte (ARCHITECTURE §5.2), die Antwort kennt zwei:

====================  ============  ==================================================
Status in der DB      Antwort       Warum
====================  ============  ==================================================
``new``               ``pending``   Gespeichert, aber der Suchindex kennt sie noch
                                    nicht — sie ist noch nicht auffindbar. Genau das
                                    heisst „wird noch verarbeitet".
``indexed``           ``imported``  Ab hier liefert der Lookup sie aus. Das ist
                                    lokal exakt das, was ``imported`` upstream
                                    bedeutet: die Einreichung hat eine AcoustID und
                                    ist nachschlagbar.
``forwarded``         ``imported``  Wie ``indexed``, zusaetzlich weitergeleitet. Die
                                    Weiterleitung aendert am lokalen Ergebnis nichts.
``forward_failed``    ``imported``  Ebenfalls: die Einreichung ist **lokal**
                                    auffindbar. Nur der Weg nach api.acoustid.org
                                    scheiterte — eine Sache des Betreibers
                                    (Warteschlange, §8.9), nicht des Clients. Sie
                                    hier ``pending`` zu nennen, wuerde Clients ewig
                                    weiterfragen lassen, obwohl alles fertig ist.
unbekannte ID         ``pending``   Siehe oben.
====================  ============  ==================================================

``result.id`` traegt die AcoustID der Einreichung (``local_track_gid``) —
dieselbe UUID, die der Lookup ausliefert. Sie ist damit auch die Bruecke
zwischen einer Submission-ID und dem, was man mit ihr anfangen kann.
"""

from __future__ import annotations

from typing import Any, Final

import psycopg

from acoustid_api.params import SubmissionStatusParams
from acoustid_api.store import load_submission_states
from shared.models import SubmissionStatus

__all__ = [
    "STATUS_IMPORTED",
    "STATUS_PENDING",
    "handle_submission_status",
]

#: „wird noch verarbeitet" — auch die Antwort auf jede unbekannte ID.
STATUS_PENDING: Final = "pending"

#: „verarbeitet, es gibt eine AcoustID dazu".
STATUS_IMPORTED: Final = "imported"

#: Die Abbildung aus dem Modul-Docstring, maschinenlesbar. Alles, was der
#: Suchindex kennt, gilt als ``imported``; ein Status, der hier fehlt, faellt
#: auf ``pending`` zurueck (die vorsichtige Richtung).
_ANSWER: Final[dict[str, str]] = {
    SubmissionStatus.NEW: STATUS_PENDING,
    SubmissionStatus.INDEXED: STATUS_IMPORTED,
    SubmissionStatus.FORWARDED: STATUS_IMPORTED,
    SubmissionStatus.FORWARD_FAILED: STATUS_IMPORTED,
}


def handle_submission_status(
    connection: psycopg.Connection, params: SubmissionStatusParams
) -> dict[str, Any]:
    """Beantwortet eine Statusabfrage (ohne den ``status``-Schluessel).

    Args:
        connection: Verbindung zur AcoustID-Postgres.
        params: Ergebnis von
            :func:`acoustid_api.params.parse_submission_status`.

    Returns:
        ``{"submissions": [...]}`` — ein Eintrag je angefragter ID, in
        Anfragereihenfolge; die Huelle mit ``status`` setzt die HTTP-Schicht.
    """
    states = load_submission_states(connection, params.ids)

    submissions: list[dict[str, Any]] = []
    for submission_id in params.ids:
        state = states.get(submission_id)
        if state is None or _ANSWER.get(state.status, STATUS_PENDING) == STATUS_PENDING:
            submissions.append({"id": submission_id, "status": STATUS_PENDING})
            continue
        submissions.append(
            {
                "id": submission_id,
                "status": STATUS_IMPORTED,
                "result": {"id": str(state.local_track_gid)},
            }
        )
    return {"submissions": submissions}
