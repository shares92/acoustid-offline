"""Fehlerhierarchie des Index-Clients (ARCHITECTURE §5.3).

Alle Fehler stammen von :class:`FpIndexError` ab, sodass ein Aufrufer den
Index pauschal absichern kann. Darunter trennen sich drei Welten:

* **Transport** (:class:`FpIndexTransportError`) — es kam gar keine HTTP-
  Antwort: Verbindung abgelehnt, DNS, Netz weg, oder unser eigenes
  Client-Timeout (:class:`FpIndexTimeoutError`).
* **Protokoll** (:class:`FpIndexProtocolError`) — es kam eine Antwort, aber
  sie war kein msgpack bzw. hatte nicht die erwartete Struktur.
* **Status** (:class:`FpIndexStatusError`) — der Server hat sauber mit einem
  Fehlerstatus geantwortet. Die Unterklassen bilden genau die Faelle ab, die
  in Phase 5 empirisch gegen das echte Image geprueft wurden.

**Zwei Timeout-Begriffe, bewusst getrennt** (Forschungsbericht, empirisch
bestaetigt): laeuft die serverseitige Suchfrist ab, antwortet der Index mit
**HTTP 500** und ``{"e": "Timeout"}`` — das ist
:class:`FpIndexSearchTimeoutError` und bedeutet „der Index lebt, hat die
Antwort nur nicht rechtzeitig gefunden". Bricht dagegen unsere eigene
HTTP-Frist ab, ist es :class:`FpIndexTimeoutError` (Transport).

Die Fehlerkennung des Servers steht in ``error_code`` (z. B.
``"VersionMismatch"``, ``"IndexNotFound"``, ``"IntegerOverflow"``); sie ist
``None``, wenn der Server nur einen Status ohne Rumpf geschickt hat — was er
z. B. bei ``GET /:index/_health`` (404) tut.
"""

from __future__ import annotations

__all__ = [
    "FpIndexAlreadyExistsError",
    "FpIndexBadRequestError",
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
]


class FpIndexError(Exception):
    """Basis aller Fehler des Index-Clients."""


class FpIndexTransportError(FpIndexError):
    """Keine Antwort erhalten (Verbindung, Netz, Namensaufloesung)."""


class FpIndexTimeoutError(FpIndexTransportError):
    """Unsere eigene HTTP-Frist ist abgelaufen.

    Nicht zu verwechseln mit :class:`FpIndexSearchTimeoutError` — das ist die
    serverseitige Suchfrist und kommt als HTTP 500 zurueck.
    """


class FpIndexProtocolError(FpIndexError):
    """Antwort war kein msgpack oder hatte eine unerwartete Struktur."""


class FpIndexStatusError(FpIndexError):
    """Der Server hat mit einem HTTP-Fehlerstatus geantwortet."""

    def __init__(self, message: str, *, status_code: int, error_code: str | None = None) -> None:
        super().__init__(message)
        #: HTTP-Statuscode der Antwort.
        self.status_code = status_code
        #: Fehlerkennung des Servers (Feld ``e``), falls vorhanden.
        self.error_code = error_code


class FpIndexBadRequestError(FpIndexStatusError):
    """HTTP 400 — der Server hat die Anfrage nicht angenommen.

    Empirisch belegte Kennungen: ``InvalidFormat`` (Rumpf nicht lesbar bzw.
    fehlender Content-Type), ``MissingStructFields``, ``UnknownUnionField``,
    ``IntegerOverflow`` (Hash oder ID ausserhalb von u32), ``InvalidIndexName``,
    ``InvalidCharacter``.
    """


class FpIndexNotFoundError(FpIndexStatusError):
    """HTTP 404 — Index (oder Route) existiert nicht (``IndexNotFound``)."""


class FpIndexConflictError(FpIndexStatusError):
    """HTTP 409 — Konflikt mit dem aktuellen Zustand des Index."""


class FpIndexVersionMismatchError(FpIndexConflictError):
    """HTTP 409 ``VersionMismatch`` — ``expected_version`` passt nicht.

    Das ist der optimistische Sperrmechanismus von ``_update``: der Batch
    wurde **nicht** angewendet. Fuer den Importer heisst das „jemand anders
    hat geschrieben" bzw. „dieser Batch lief schon" (Idempotenz-Signal).
    """


class FpIndexAlreadyExistsError(FpIndexConflictError):
    """HTTP 409 beim Anlegen: ``IndexAlreadyExists`` und die Generationsfaelle.

    Auch ``OlderIndexAlreadyExists`` und ``NewerIndexAlreadyExists`` landen
    hier — beides tritt nur auf, wenn beim Anlegen eine ``generation``
    mitgegeben wird.
    """


class FpIndexNotReadyError(FpIndexStatusError):
    """HTTP 503 ``IndexNotReady`` — der Index laedt noch.

    Der einzige echte Bereitschaftsindikator des Servers: ``/_health`` prueft
    nichts und ``/:index/_health`` nur die Existenz.
    """


class FpIndexServerError(FpIndexStatusError):
    """HTTP 5xx ohne speziellere Bedeutung."""


class FpIndexSearchTimeoutError(FpIndexServerError):
    """HTTP 500 ``Timeout`` — die serverseitige Suchfrist ist abgelaufen.

    Es gibt **kein Teilergebnis**; der Aufrufer entscheidet, ob er mit
    groesserer Frist wiederholt oder den Lookup als ergebnislos wertet.
    """
