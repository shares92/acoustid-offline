"""Fehlerformat der AcoustID-Web-API (ARCHITECTURE §7, Phase-1-Vertrag).

Ein Fehler sieht bei api.acoustid.org immer gleich aus — und zwar so::

    {"status": "error", "error": {"code": 4, "message": "invalid API key"}}

Dazu gehoeren **19 feste Codes** mit festen Meldungstexten und einem festen
HTTP-Status; Clients werten beides aus (Picard z. B. verkleinert seine
Submit-Batches, sobald ein 413 mit Code 19 kommt). Deshalb steht hier die
vollstaendige Tabelle, auch fuer die Codes, die erst spaetere Phasen
ausloesen — der Vertrag ist die Tabelle, nicht die Teilmenge, die wir gerade
brauchen.

Quelle: ``acoustid/api/errors.py`` des Original-Servers, uebernommen in
docs/research/phase1-api-formate.md. Abweichungen von den Meldungstexten
waeren fuer Clients unsichtbar — sie waeren aber auch nutzlos, und sie
wuerden die Vergleichbarkeit mit den Original-Beispielantworten kosten.

Der HTTP-Status ist **nicht** durchgaengig 400:

======  ======  ==================================================
Code    HTTP    Bedeutung
======  ======  ==================================================
5       500     interner Fehler
13      503     Dienst gerade nicht verfuegbar
14      429     Rate-Limit (setzt der Waechter durch, nicht die API)
18      404     Fingerprint unbekannt
19      413     Anfrage zu gross
alle    400     Parameter-/Formatfehler
anderen
======  ======  ==================================================

Der Rumpf traegt **kein** ``Retry-After`` und keine ``X-RateLimit``-Header;
das Original schickt sie nicht, und Clients erwarten sie nicht.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "ERROR_CODES",
    "AcoustidError",
    "InsecureRequestError",
    "InternalError",
    "InvalidApiKeyError",
    "InvalidBitrateError",
    "InvalidDurationError",
    "InvalidFingerprintError",
    "InvalidForeignIdError",
    "InvalidMaxDurationDiffError",
    "InvalidMusicBrainzAccessTokenError",
    "InvalidUserApiKeyError",
    "InvalidUuidError",
    "MissingParameterError",
    "NotAllowedError",
    "RequestTooLargeError",
    "ServiceUnavailableError",
    "TooManyRequestsError",
    "TrackNotFoundError",
    "UnknownApplicationError",
    "UnknownFormatError",
    "error_payload",
]

#: Groesster zulaessiger Wert von ``maxdurationdiff``
#: (``FINGERPRINT_MAX_ALLOWED_LENGTH_DIFF`` im Original).
MAX_ALLOWED_DURATION_DIFF: Final = 30


class AcoustidError(Exception):
    """Basis aller Fehler, die als AcoustID-Fehlerantwort herausgehen.

    Unterklassen setzen :attr:`code` und :attr:`http_status`; die Meldung
    baut jede Klasse selbst, damit der Text exakt dem Original entspricht.
    """

    #: Fehlercode aus der 19er-Tabelle.
    code: int = 0
    #: HTTP-Status dieser Antwort.
    http_status: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        #: Der Meldungstext, wie er in der Antwort steht.
        self.message = message


class UnknownFormatError(AcoustidError):
    """1 — `format` ist weder json noch jsonp noch xml."""

    code = 1

    def __init__(self, name: str) -> None:
        super().__init__(f'unknown format "{name}"')


class MissingParameterError(AcoustidError):
    """2 — ein Pflichtparameter fehlt (oder ist leer)."""

    code = 2

    def __init__(self, name: str) -> None:
        super().__init__(f'missing required parameter "{name}"')
        self.parameter = name


class InvalidFingerprintError(AcoustidError):
    """3 — der Fingerprint liess sich nicht dekodieren."""

    code = 3

    def __init__(self) -> None:
        super().__init__("invalid fingerprint")


class InvalidApiKeyError(AcoustidError):
    """4 — unbekannter Application-Key.

    Wird von der API **nie** ausgeloest: die Key-Pruefung macht der Waechter
    am Proxy (ARCHITECTURE §7 „Durchsetzungsort Auth & Rate-Limit"). Die
    Klasse steht hier, weil der Waechter dasselbe Format braucht.
    """

    code = 4

    def __init__(self) -> None:
        super().__init__("invalid API key")


class InternalError(AcoustidError):
    """5 — unerwarteter Fehler; alles Ungefangene landet hier."""

    code = 5
    http_status = 500

    def __init__(self) -> None:
        super().__init__("internal error")


class InvalidUserApiKeyError(AcoustidError):
    """6 — unbekannter Benutzer-Key (Submit, Phase 11)."""

    code = 6

    def __init__(self) -> None:
        super().__init__('invalid user API key ("User with the API key does not exist")')


class InvalidUuidError(AcoustidError):
    """7 — ein Parameter sollte eine UUID sein, ist aber keine."""

    code = 7

    def __init__(self, name: str) -> None:
        super().__init__(f'parameter "{name}" is not a valid UUID')
        self.parameter = name


class InvalidDurationError(AcoustidError):
    """8 — `duration` ist keine positive Ganzzahl (Submit, Phase 11)."""

    code = 8

    def __init__(self, name: str) -> None:
        super().__init__(f'parameter "{name}" must be a positive integer')
        self.parameter = name


class InvalidBitrateError(AcoustidError):
    """9 — `bitrate` ist keine positive Ganzzahl (Submit, Phase 11)."""

    code = 9

    def __init__(self, name: str) -> None:
        super().__init__(f'parameter "{name}" must be a positive integer')
        self.parameter = name


class InvalidForeignIdError(AcoustidError):
    """10 — `foreignid` ist nicht `vendor:id` (Submit, Phase 11)."""

    code = 10

    def __init__(self, name: str) -> None:
        super().__init__(
            f'parameter "{name}" is not a valid foreign ID, it must be in the format vendor:id'
        )
        self.parameter = name


class InvalidMaxDurationDiffError(AcoustidError):
    """11 — `maxdurationdiff` liegt ausserhalb von 1…30."""

    code = 11

    def __init__(self, name: str) -> None:
        super().__init__(f'parameter "{name}" must be between 1 and {MAX_ALLOWED_DURATION_DIFF}')
        self.parameter = name


class NotAllowedError(AcoustidError):
    """12 — Vorgang nicht erlaubt."""

    code = 12

    def __init__(self) -> None:
        super().__init__("not allowed")


class ServiceUnavailableError(AcoustidError):
    """13 — ein gebrauchter Dienst antwortet gerade nicht.

    Bei uns: der acoustid-index ist nicht erreichbar, laedt noch oder hat
    die Suchfrist ueberschritten. Der Lookup **erfindet dann keine leere
    Trefferliste** — ein „nichts gefunden" waere von einem echten
    Nicht-Treffer nicht zu unterscheiden.
    """

    code = 13
    http_status = 503

    def __init__(self) -> None:
        super().__init__("service currently unavailable, try again later")


class TooManyRequestsError(AcoustidError):
    """14 — Rate-Limit; setzt der Waechter durch, nicht die API."""

    code = 14
    http_status = 429

    def __init__(self, rate: float) -> None:
        super().__init__(f"rate limit ({rate:f} requests per second) exceeded, try again later")


class InvalidMusicBrainzAccessTokenError(AcoustidError):
    """15 — ungueltiges MusicBrainz-Token."""

    code = 15

    def __init__(self) -> None:
        super().__init__("invalid MusicBrainz access token")


class InsecureRequestError(AcoustidError):
    """16 — die Route verlangt HTTPS."""

    code = 16

    def __init__(self) -> None:
        super().__init__("only requests over HTTPS are allowed here")


class UnknownApplicationError(AcoustidError):
    """17 — unbekannte Anwendung."""

    code = 17

    def __init__(self) -> None:
        super().__init__("unknown application")


class TrackNotFoundError(AcoustidError):
    """18 — Fingerprint/Track unbekannt (``/v2/fingerprint``)."""

    code = 18
    http_status = 404

    def __init__(self) -> None:
        super().__init__("fingerprint not found")


class RequestTooLargeError(AcoustidError):
    """19 — Anfrage zu gross: Rumpf > 1 MiB oder zu viele Teilanfragen.

    Picard stuetzt sein Batch-Sizing genau auf diese Antwort und
    verkleinert danach seine Pakete — der Code muss also wirklich kommen,
    ein stilles Abschneiden waere schaedlich.
    """

    code = 19
    http_status = 413

    def __init__(self) -> None:
        super().__init__("request too large")


#: Vollstaendige Tabelle Code -> (Klasse, HTTP-Status). Ein Test haelt sie
#: gegen den Forschungsbericht; sie ist zugleich die Fundstelle fuer den
#: Waechter, der dieselben Fehler erzeugen muss.
ERROR_CODES: Final[dict[int, type[AcoustidError]]] = {
    1: UnknownFormatError,
    2: MissingParameterError,
    3: InvalidFingerprintError,
    4: InvalidApiKeyError,
    5: InternalError,
    6: InvalidUserApiKeyError,
    7: InvalidUuidError,
    8: InvalidDurationError,
    9: InvalidBitrateError,
    10: InvalidForeignIdError,
    11: InvalidMaxDurationDiffError,
    12: NotAllowedError,
    13: ServiceUnavailableError,
    14: TooManyRequestsError,
    15: InvalidMusicBrainzAccessTokenError,
    16: InsecureRequestError,
    17: UnknownApplicationError,
    18: TrackNotFoundError,
    19: RequestTooLargeError,
}


def error_payload(error: AcoustidError) -> dict[str, Any]:
    """Der Antwortrumpf zu einem Fehler — genau die drei Schluessel."""
    return {"status": "error", "error": {"code": error.code, "message": error.message}}
