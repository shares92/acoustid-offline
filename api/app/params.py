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
  nur, wenn am Ende gar keine gueltige uebrig ist. Das gilt **nur fuer den
  Lookup**: beim Submit kippt eine kaputte Teilanfrage die ganze Anfrage
  (das Original prueft dort alle Parameter im Voraus).

Was hier bewusst NICHT passiert: ``client`` und ``user`` werden nur auf
Anwesenheit geprueft, nie auf Gueltigkeit. Die Key-Pruefung ist Sache des
Waechters (ARCHITECTURE §7 „Durchsetzungsort Auth & Rate-Limit"); einen
Benutzerbestand hat diese Instanz gar nicht, Fehler 6 kann hier also nie
entstehen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from acoustid_api.errors import (
    InvalidBitrateError,
    InvalidDurationError,
    InvalidFingerprintError,
    InvalidForeignIdError,
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
    "MAX_SUBMIT_DURATION",
    "MAX_TRACK_NUMBER",
    "MAX_TRACK_QUERIES",
    "FingerprintQuery",
    "LookupParams",
    "RequestValues",
    "Submission",
    "SubmitParams",
    "TrackQuery",
    "iter_suffixes",
    "normalise_text",
    "normalise_track_number",
    "parse_format",
    "parse_lookup",
    "parse_submit",
]

#: ``FINGERPRINT_MAX_LENGTH_DIFF`` — Standard-Laengentoleranz in Sekunden.
DEFAULT_MAX_DURATION_DIFF: Final = 7

#: ``FINGERPRINT_MAX_ALLOWED_LENGTH_DIFF`` — Obergrenze von ``maxdurationdiff``.
MAX_ALLOWED_DURATION_DIFF: Final = 30

#: Hoechstzahl Fingerprint-Teilanfragen je Request (sonst Fehler 19/413).
MAX_FINGERPRINT_QUERIES: Final = 20

#: Hoechstzahl Track-Teilanfragen je Request (sonst Fehler 19/413).
MAX_TRACK_QUERIES: Final = 100

#: Groesster zulaessiger Wert von ``duration.N`` beim Submit (Original:
#: ``0x7FFF``). Darueber (und bei ``0``) kommt Fehler 8.
MAX_SUBMIT_DURATION: Final = 32767

#: ``fix_meta`` des Originals verwirft Track- und Disc-Nummern **oberhalb**
#: dieser Grenze — sie stammen erfahrungsgemaess aus kaputten Tags.
MAX_TRACK_NUMBER: Final = 10000

_UUID_RE: Final = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")

#: ``foreignid`` hat die Form ``vendor:id`` (Original-Muster).
_FOREIGN_ID_RE: Final = re.compile(r"^[0-9a-z]+:.+$")


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
    #: ``meta``-Werte, bereits in die Liste zerlegt. Ausgewertet werden sie
    #: erst im Antwortaufbau (:mod:`acoustid_api.meta`) — dort sitzt auch
    #: die Praezedenzregel.
    meta: tuple[str, ...] = ()
    #: Optionale Versionsangabe des Clients; nur fuer Logs.
    client_version: str | None = None

    def selected(self) -> tuple[FingerprintQuery | TrackQuery, ...]:
        """Die tatsaechlich zu beantwortenden Teilanfragen.

        Ohne ``batch`` beantwortet das Original nur die **erste** — auch
        wenn zwanzig mitgeschickt wurden.
        """
        return self.queries if self.batch else self.queries[:1]


@dataclass(frozen=True, slots=True)
class Submission:
    """Eine eingereichte Aufnahme aus ``/v2/submit`` (eine Teilanfrage).

    Die MBIDs stehen hier noch als **Liste**: das Original erzeugt je MBID
    eine eigene Submission-Zeile mit eigener ID in der Antwort — das
    Auffaechern macht :mod:`acoustid_api.submit`, nicht das Parsen.

    Attributes:
        index: Nummer aus dem Suffix **als Zeichenkette** (``fingerprint.3``
            -> ``"3"``); ``None`` beim unnummerierten Parameter. Der Typ ist
            Absicht: das Feld geht so in die Antwort (Eigenheit des
            Originals, dessen Doku faelschlich eine Zahl zeigt).
        duration: ``duration.N`` in Sekunden, 1…:data:`MAX_SUBMIT_DURATION`.
        hashes: Dekodierter Vollvektor (u32).
    """

    index: str | None
    duration: int
    hashes: tuple[int, ...]
    mbids: tuple[str, ...] = ()
    puid: str | None = None
    foreignid: str | None = None
    bitrate: int | None = None
    fileformat: str | None = None
    track: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    year: int | None = None

    @property
    def has_content(self) -> bool:
        """Traegt die Einreichung ueberhaupt etwas Auswertbares?

        Ist die Antwort ``False``, verwirft das Original die Teilanfrage
        **kommentarlos** — sie taucht in ``submissions[]`` gar nicht erst auf
        (Phase-1-Bericht, „Stille Verwerfung"). Ein Fingerprint ohne jede
        Zuordnung waere auch tatsaechlich wertlos: nachschlagen liesse er
        sich, aber die Antwort bliebe leer.
        """
        return bool(
            self.mbids
            or self.puid
            or self.foreignid
            or self.track
            or self.artist
            or self.album
            or self.album_artist
            or self.track_no
            or self.disc_no
            or self.year
        )


@dataclass(frozen=True, slots=True)
class SubmitParams:
    """Vollstaendig geprueftes Ergebnis von ``/v2/submit``."""

    response_format: ResponseFormat
    client: str
    #: ``user`` — im Original der Account-Key des einreichenden Nutzers. Wird
    #: hier nur verlangt, nie geprueft, und fuer Phase 12 mitgeschrieben.
    user: str
    submissions: tuple[Submission, ...]
    client_version: str | None = None
    #: ``wait`` wird gelesen und **ignoriert** (Original-Verhalten): die
    #: Verarbeitung ist ohnehin asynchron, die Antwort immer ``pending``.
    wait: int | None = None


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


# --- /v2/submit -------------------------------------------------------------


def normalise_text(value: str | None) -> str | None:
    """Whitespace eines Textmetadatums vereinheitlichen (Teil von ``fix_meta``).

    Fuehrende und schliessende Leerzeichen fallen weg, innere Folgen werden
    zu genau einem Leerzeichen — und was danach leer ist, gilt als nicht
    angegeben. Tags aus echten Dateien tragen erstaunlich oft Tabulatoren,
    geschuetzte Leerzeichen oder Zeilenumbrueche; ohne die Normalisierung
    stuenden zwei Einreichungen derselben Aufnahme als verschiedene Werte in
    der Datenbank.
    """
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def normalise_track_number(value: int | None) -> int | None:
    """Track-/Disc-Nummer pruefen (Teil von ``fix_meta``).

    Werte oberhalb von :data:`MAX_TRACK_NUMBER` verwirft schon das Original:
    sie kommen aus kaputten Tags (Jahreszahlen, Zeitstempel, Zufallszahlen)
    und waeren als Nummer sinnlos.
    """
    if value is None or value > MAX_TRACK_NUMBER:
        return None
    return value


def parse_submit(values: RequestValues) -> SubmitParams:
    """Liest und prueft alle Parameter von ``/v2/submit``.

    Reihenfolge wie im Original: ``format`` (damit die Fehlerantwort im
    gewuenschten Format herausgeht), dann ``client``, dann ``user``, dann die
    Teilanfragen. Anders als beim Lookup wird **keine** kaputte Teilanfrage
    uebersprungen — eine fehlerhafte Angabe kippt die ganze Anfrage.

    Raises:
        AcoustidError: Irgendein Parameter verletzt den Vertrag.
    """
    response_format = parse_format(values)

    client = values.get("client")
    if not client:
        raise MissingParameterError("client")
    user = values.get("user")
    if not user:
        raise MissingParameterError("user")

    suffixes = iter_suffixes(values, "fingerprint")
    if not suffixes:
        raise MissingParameterError("fingerprint")

    parsed = [_parse_submission(values, suffix) for suffix in suffixes]
    return SubmitParams(
        response_format=response_format,
        client=client,
        user=user,
        # Stille Verwerfung: erst pruefen (Fehler sollen kommen), dann
        # aussortieren, was gar nichts zuzuordnen hat.
        submissions=tuple(item for item in parsed if item.has_content),
        client_version=values.get("clientversion"),
        wait=values.get_int("wait"),
    )


def _parse_submission(values: RequestValues, suffix: str) -> Submission:
    """Eine einzelne eingereichte Aufnahme lesen.

    Raises:
        MissingParameterError: ``duration`` oder ``fingerprint`` fehlt.
        InvalidDurationError: ``duration`` liegt ausserhalb von 1…32767.
        InvalidFingerprintError: Der Fingerprint liess sich nicht dekodieren.
        InvalidBitrateError: ``bitrate`` ist keine positive Ganzzahl.
        InvalidUuidError: ``mbid`` oder ``puid`` ist keine UUID.
        InvalidForeignIdError: ``foreignid`` ist nicht ``vendor:id``.
    """
    # `.3` -> `"3"`, `` -> None. Bewusst als Zeichenkette (siehe Submission).
    index = suffix[1:] or None

    duration = values.get_int("duration" + suffix)
    if duration is None:
        raise MissingParameterError("duration" + suffix)
    if not 1 <= duration <= MAX_SUBMIT_DURATION:
        raise InvalidDurationError("duration" + suffix)

    encoded = values.get("fingerprint" + suffix)
    if not encoded:
        raise MissingParameterError("fingerprint" + suffix)
    try:
        decoded = decode_fingerprint(encoded)
    except FingerprintDecodeError as exc:
        raise InvalidFingerprintError() from exc
    if not decoded.hashes:
        raise InvalidFingerprintError()

    bitrate = values.get_int("bitrate" + suffix)
    if bitrate is not None and bitrate <= 0:
        raise InvalidBitrateError("bitrate" + suffix)

    return Submission(
        index=index,
        duration=duration,
        hashes=decoded.hashes,
        mbids=tuple(
            _uuid(value, "mbid" + suffix) for value in values.get_all("mbid" + suffix) if value
        ),
        puid=_optional_uuid(values.get("puid" + suffix), "puid" + suffix),
        foreignid=_foreign_id(values.get("foreignid" + suffix), "foreignid" + suffix),
        bitrate=bitrate,
        fileformat=normalise_text(values.get("fileformat" + suffix)),
        track=normalise_text(values.get("track" + suffix)),
        artist=normalise_text(values.get("artist" + suffix)),
        album=normalise_text(values.get("album" + suffix)),
        album_artist=normalise_text(values.get("albumartist" + suffix)),
        track_no=normalise_track_number(values.get_int("trackno" + suffix)),
        disc_no=normalise_track_number(values.get_int("discno" + suffix)),
        year=values.get_int("year" + suffix),
    )


def _uuid(value: str, name: str) -> str:
    if not _UUID_RE.match(value):
        raise InvalidUuidError(name)
    return value


def _optional_uuid(value: str | None, name: str) -> str | None:
    return _uuid(value, name) if value else None


def _foreign_id(value: str | None, name: str) -> str | None:
    if not value:
        return None
    if not _FOREIGN_ID_RE.match(value):
        raise InvalidForeignIdError(name)
    return value
