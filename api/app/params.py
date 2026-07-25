"""Parameter der Web-API einlesen (ARCHITECTURE §7, Phase-1-Vertrag).

Die AcoustID-API hat eine sehr eigene Vorstellung davon, woher ein Parameter
kommt und was passiert, wenn er unsinnig ist. Das ist hier nachgebaut, weil
echte Clients sich darauf verlassen:

* **Query-String UND Formular-Rumpf** gelten gleichzeitig; steht ein Name in
  beiden, gewinnt der Query-String (Original: ``request.values``).
* **GET und POST** sind an jeder Route gleichwertig.
* **Kein JSON-Rumpf.** Der Rumpf ist ``application/x-www-form-urlencoded``.
* **Zahlenparameter werden weich gelesen.** ``duration=abc`` ist nicht etwa
  ein Typfehler, sondern gilt als „nicht angegeben" — und fuehrt damit zu
  Fehler 2 („missing required parameter"). Ebenso faellt ein unlesbares
  ``maxdurationdiff`` auf den Standardwert zurueck. Wer hier strenger ist,
  bricht Anfragen ab, die das Original beantwortet.
* **Nummerierte Teilanfragen.** ``fingerprint.0``/``duration.0`` (bzw.
  ``trackid.0``) bilden das Original-Batchprotokoll; ohne ``batch`` wird nur
  die **erste** Teilanfrage beantwortet, mit ``batch=1`` alle.
* **Unlesbare Teilanfragen werden still uebersprungen** — ein Fehler kommt
  nur, wenn am Ende gar keine gueltige uebrig ist.

Was hier bewusst NICHT passiert: ``client`` wird nur auf Anwesenheit
geprueft, nie auf Gueltigkeit. Die Key-Pruefung ist Sache des Waechters
(ARCHITECTURE §7 „Durchsetzungsort Auth & Rate-Limit").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from acoustid_api.errors import (
    InvalidFingerprintError,
    InvalidMaxDurationDiffError,
    InvalidUuidError,
    MissingParameterError,
    RequestTooLargeError,
    UnknownFormatError,
)
from acoustid_api.formats import ResponseFormat
from shared.fingerprint import FingerprintDecodeError, decode_fingerprint

__all__ = [
    "DEFAULT_MAX_DURATION_DIFF",
    "MAX_ALLOWED_DURATION_DIFF",
    "MAX_FINGERPRINT_QUERIES",
    "MAX_TRACK_QUERIES",
    "FingerprintQuery",
    "LookupParams",
    "RequestValues",
    "TrackQuery",
    "iter_suffixes",
    "parse_format",
    "parse_lookup",
]

#: ``FINGERPRINT_MAX_LENGTH_DIFF`` — Standard-Laengentoleranz in Sekunden.
DEFAULT_MAX_DURATION_DIFF: Final = 7

#: ``FINGERPRINT_MAX_ALLOWED_LENGTH_DIFF`` — Obergrenze von ``maxdurationdiff``.
MAX_ALLOWED_DURATION_DIFF: Final = 30

#: Hoechstzahl Fingerprint-Teilanfragen je Request (sonst Fehler 19/413).
MAX_FINGERPRINT_QUERIES: Final = 20

#: Hoechstzahl Track-Teilanfragen je Request (sonst Fehler 19/413).
MAX_TRACK_QUERIES: Final = 100

_UUID_RE: Final = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


class RequestValues:
    """Query-String und Formular-Rumpf als eine Sicht (Original ``values``).

    Der Query-String hat Vorrang: steht ein Name in beiden, kommt sein Wert
    aus der URL. Mehrfach belegte Namen liefern den **ersten** Wert — nur der
    Submit braucht spaeter alle (``mbid.N`` mehrfach), deshalb gibt es dafuer
    :meth:`get_all`.
    """

    def __init__(
        self,
        query: Sequence[tuple[str, str]] = (),
        form: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._items: list[tuple[str, str]] = [*query, *form]
        self._first: dict[str, str] = {}
        for name, value in self._items:
            self._first.setdefault(name, value)

    def __contains__(self, name: object) -> bool:
        return name in self._first

    def __iter__(self) -> Iterator[str]:
        """Namen in Reihenfolge des ersten Auftretens."""
        return iter(self._first)

    def get(self, name: str, default: str | None = None) -> str | None:
        """Erster Wert des Namens (Query-String vor Rumpf)."""
        return self._first.get(name, default)

    def get_all(self, name: str) -> list[str]:
        """Alle Werte des Namens, Query-String zuerst."""
        return [value for key, value in self._items if key == name]

    def get_int(self, name: str, default: int | None = None) -> int | None:
        """Ganzzahl oder ``default`` — auch bei unlesbarem Wert.

        Das ist die weiche Lesart des Originals (``values.get(..., type=int)``):
        ein Wert, der keine Zahl ist, gilt als nicht angegeben.
        """
        raw = self._first.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default


@dataclass(frozen=True, slots=True)
class FingerprintQuery:
    """Eine Teilanfrage mit Fingerprint und Laenge.

    Attributes:
        index: Nummer aus dem Suffix (``fingerprint.3`` -> 3); ``None`` beim
            unnummerierten Parameter.
        duration: Laenge der Aufnahme in Sekunden, wie vom Client gemeldet.
        hashes: Dekodierter Vollvektor (u32).
    """

    index: int | None
    duration: int
    hashes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TrackQuery:
    """Eine Teilanfrage, die direkt eine AcoustID nachschlaegt."""

    index: int | None
    track_gid: str


@dataclass(frozen=True, slots=True)
class LookupParams:
    """Vollstaendig geprueftes Ergebnis von ``/v2/lookup``."""

    response_format: ResponseFormat
    client: str
    queries: tuple[FingerprintQuery | TrackQuery, ...]
    batch: bool
    max_duration_diff: int = DEFAULT_MAX_DURATION_DIFF
    #: ``meta``-Werte, bereits in die Liste zerlegt. Phase 9 wertet sie noch
    #: nicht aus (Metadaten kommen in Phase 10), nimmt sie aber entgegen —
    #: Picard und beets schicken sie immer mit.
    meta: tuple[str, ...] = ()
    #: Optionale Versionsangabe des Clients; nur fuer Logs.
    client_version: str | None = None

    def selected(self) -> tuple[FingerprintQuery | TrackQuery, ...]:
        """Die tatsaechlich zu beantwortenden Teilanfragen.

        Ohne ``batch`` beantwortet das Original nur die **erste** — auch
        wenn zwanzig mitgeschickt wurden.
        """
        return self.queries if self.batch else self.queries[:1]


def iter_suffixes(names: Iterable[str], *prefixes: str) -> list[str]:
    """Sammelt die Suffixe nummerierter Parameter (Original ``iter_args_suffixes``).

    ``fingerprint`` liefert ``""``, ``fingerprint.3`` liefert ``".3"``.
    Sortiert nach der Nummer, der unnummerierte Parameter zuerst.
    """
    found: set[int] = set()
    for name in names:
        for prefix in prefixes:
            if name == prefix:
                found.add(-1)
            elif name.startswith(prefix + "."):
                suffix = name[len(prefix) + 1 :]
                if suffix.isdigit():
                    found.add(int(suffix))
    return [f".{number}" if number != -1 else "" for number in sorted(found)]


def _parse_meta(raw: str | None) -> tuple[str, ...]:
    """``meta`` in seine Einzelwerte zerlegen (inkl. der numerischen Kurzform)."""
    if not raw or raw == "0":
        return ()
    if raw == "1":
        return ("recordingids",)
    if raw == "2":
        return ("m2",)
    return tuple(raw.split())


def _parse_query(values: RequestValues, suffix: str) -> FingerprintQuery | TrackQuery:
    """Eine einzelne Teilanfrage lesen.

    Raises:
        InvalidUuidError: ``trackid`` ist keine UUID.
        MissingParameterError: ``duration`` oder ``fingerprint`` fehlt.
        InvalidFingerprintError: Der Fingerprint liess sich nicht dekodieren.
    """
    index = int(suffix[1:]) if suffix else None

    track_gid = values.get("trackid" + suffix)
    if track_gid:
        if not _UUID_RE.match(track_gid):
            raise InvalidUuidError("trackid" + suffix)
        return TrackQuery(index=index, track_gid=track_gid)

    # `0` und unlesbare Werte gelten beide als „nicht angegeben".
    duration = values.get_int("duration" + suffix, 0) or 0
    if not duration:
        raise MissingParameterError("duration" + suffix)
    encoded = values.get("fingerprint" + suffix)
    if not encoded:
        raise MissingParameterError("fingerprint" + suffix)
    try:
        decoded = decode_fingerprint(encoded)
    except FingerprintDecodeError as exc:
        raise InvalidFingerprintError() from exc
    if not decoded.hashes:
        # Formal gueltiger Kopf ohne Subfingerprints — fuer das Original ist
        # eine leere Trefferliste kein Fingerprint.
        raise InvalidFingerprintError()
    return FingerprintQuery(index=index, duration=duration, hashes=decoded.hashes)


def parse_format(values: RequestValues) -> ResponseFormat:
    """Liest ``format`` und ``jsoncallback``.

    Eigener Einstieg, weil das Antwortformat schon feststehen muss, bevor
    irgendein anderer Parameter geprueft wird: eine Fehlerantwort soll in dem
    Format herauskommen, das der Client verlangt hat. Nur wenn genau dieser
    Parameter kaputt ist, faellt die Antwort auf JSON zurueck.

    Raises:
        UnknownFormatError: ``format`` ist keiner der drei Werte.
    """
    try:
        return ResponseFormat.parse(values.get("format"), values.get("jsoncallback"))
    except ValueError as exc:
        raise UnknownFormatError(str(exc)) from exc


def parse_lookup(values: RequestValues) -> LookupParams:
    """Liest und prueft alle Parameter von ``/v2/lookup``.

    Reihenfolge wie im Original: erst ``format`` (damit die Fehlerantwort im
    gewuenschten Format herausgeht), dann ``client``, dann der Rest.

    Raises:
        AcoustidError: Irgendein Parameter verletzt den Vertrag.
    """
    response_format = parse_format(values)

    client = values.get("client")
    if not client:
        raise MissingParameterError("client")

    max_duration_diff = values.get_int("maxdurationdiff")
    if max_duration_diff is None:
        max_duration_diff = DEFAULT_MAX_DURATION_DIFF
    elif not 1 <= max_duration_diff <= MAX_ALLOWED_DURATION_DIFF:
        raise InvalidMaxDurationDiffError("maxdurationdiff")

    batch = bool(values.get_int("batch"))

    suffixes = iter_suffixes(values, "fingerprint", "trackid")
    if not suffixes:
        raise MissingParameterError("fingerprint")

    queries: list[FingerprintQuery | TrackQuery] = []
    last_error: Exception | None = None
    for position, suffix in enumerate(suffixes):
        try:
            queries.append(_parse_query(values, suffix))
        except (
            InvalidFingerprintError,
            InvalidUuidError,
            MissingParameterError,
        ) as exc:
            # Eine kaputte Teilanfrage kippt die Anfrage nur, wenn sie die
            # letzte Hoffnung war.
            last_error = exc
            if not queries and position + 1 == len(suffixes):
                raise
    if not queries and last_error is not None:  # pragma: no cover - Absicherung
        raise last_error

    params = LookupParams(
        response_format=response_format,
        client=client,
        queries=tuple(queries),
        batch=batch,
        max_duration_diff=max_duration_diff,
        meta=_parse_meta(values.get("meta")),
        client_version=values.get("clientversion"),
    )
    _check_limits(params)
    return params


def _check_limits(params: LookupParams) -> None:
    """20 Fingerprint-, 100 Track-Teilanfragen (sonst Fehler 19/413)."""
    selected = params.selected()
    fingerprints = sum(1 for query in selected if isinstance(query, FingerprintQuery))
    tracks = len(selected) - fingerprints
    if fingerprints > MAX_FINGERPRINT_QUERIES or tracks > MAX_TRACK_QUERIES:
        raise RequestTooLargeError()
