"""Fehlerformat und Fehlertabelle gegen die Original-Beispiele.

Die 19 Codes sind Vertrag, nicht Geschmackssache: Clients werten Code und
HTTP-Status aus (Picard verkleinert seine Batches auf 19/413, wiederholt bei
429 und 503). Deshalb wird hier die vollstaendige Tabelle aus dem
Forschungsbericht gegen die Implementierung gehalten — Code, HTTP-Status und
Meldungstext.
"""

from __future__ import annotations

import pytest
from original_examples import ERROR_EXAMPLE, ERROR_TABLE

from acoustid_api.errors import (
    ERROR_CODES,
    AcoustidError,
    InvalidApiKeyError,
    TooManyRequestsError,
    error_payload,
)

#: Platzhalter fuer die Klassen, die einen Parameternamen brauchen.
_PLACEHOLDER = "<name>"


def _instantiate(code: int) -> AcoustidError:
    """Eine Beispielinstanz je Code — mit dem Platzhalter des Berichts."""
    error_class = ERROR_CODES[code]
    if code == 14:
        return TooManyRequestsError(4.0)
    try:
        return error_class(_PLACEHOLDER)  # type: ignore[call-arg]
    except TypeError:
        return error_class()  # type: ignore[call-arg]


def test_all_nineteen_codes_exist() -> None:
    assert sorted(ERROR_CODES) == list(range(1, 20))


@pytest.mark.parametrize(("code", "status", "message"), ERROR_TABLE)
def test_code_status_and_message_match_the_original(code: int, status: int, message: str) -> None:
    error = _instantiate(code)
    assert error.code == code
    assert error.http_status == status
    if code == 14:
        # Der Bericht zeigt den Formatstring; %f wird zu sechs Nachkommastellen.
        assert error.message == message.replace("%f", "4.000000")
    else:
        assert error.message == message


def test_error_payload_matches_the_original_example() -> None:
    assert error_payload(InvalidApiKeyError()) == ERROR_EXAMPLE


def test_error_payload_has_exactly_the_documented_keys() -> None:
    payload = error_payload(InvalidApiKeyError())
    assert set(payload) == {"status", "error"}
    assert set(payload["error"]) == {"code", "message"}


def test_every_error_is_an_acoustid_error() -> None:
    for code in ERROR_CODES:
        assert isinstance(_instantiate(code), AcoustidError)


def test_str_of_an_error_is_its_message() -> None:
    """Das Log soll dieselbe Meldung zeigen wie die Antwort."""
    error = InvalidApiKeyError()
    assert str(error) == error.message == "invalid API key"
