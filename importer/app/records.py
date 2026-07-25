"""Typisierte Records der sieben Stroeme und ihr Feldvertrag (§5.1, §12.8).

Je Strom gibt es genau eine Record-Klasse mit **exakt** den Feldern aus der
Tabelle in ARCHITECTURE §5.1 — in derselben Reihenfolge, mit denselben
Namen. Daneben steht je Strom ein :class:`StreamSpec`: die maschinenlesbare
Fassung derselben Tabelle (Pflicht/optional, erwarteter Typ, Default bei
Abwesenheit). Der Parser arbeitet ausschliesslich gegen diese Specs, der
Feld-Sanity-Check aus §12.8 ebenfalls, und ein Test haelt sie gegen die
Markdown-Tabelle in ARCHITECTURE.md.

**Warum ``dataclass`` und nicht ``pydantic``.** Die Records sind ein
Heisspfad: der Bootstrap spielt 38.178 Dateien mit Milliarden Zeilen ein,
allein ``fingerprint`` sind 390 GB gz. ``@dataclass(frozen=True,
slots=True)`` kostet pro Zeile nur den Konstruktoraufruf und belegt dank
``slots`` kein ``__dict__``; ein pydantic-Modell wuerde jede Zeile ein
zweites Mal validieren, obwohl der Parser die Typen ohnehin selbst prueft
(er muss es: er braucht Dateiname und Zeilennummer in der Fehlermeldung,
die pydantic nicht kennt). Im Projekt ist das die etablierte Trennung —
pydantic fuer Konfiguration (einmal je Prozessstart, `shared.config`,
`shared.env`), Dataclasses fuer Datenstroeme (`shared.fpindex.wire`).

**Absent-Semantik (Import-Regel 3).** Der Export laeuft mit
``json_strip_nulls``: ein fehlender Schluessel heisst NULL bzw. ``false``,
niemals „unveraendert". Deshalb hat **kein** Feld einen Default-Wert im
Konstruktor — der Parser setzt jedes Feld explizit, auch die fehlenden
(``None``, bei ``track_mbid.disabled`` ``False``). Ein Record laesst sich
so nie versehentlich als „Teil-Patch" bauen.

**Zeitstempel bleiben Strings.** ``created``/``updated`` kommen als
ISO-8601 mit Offset (``2026-07-22T00:00:18.312881+00:00``) und gehen genau
so in die Postgres, die sie selbst nach ``timestamptz`` wandelt. Ein
``datetime`` dazwischen waere ein zweites Parsen pro Zeile ohne jeden
Gewinn. Geprueft wird die Form (siehe :data:`TIMESTAMP_RE`), nicht der
Wert.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Final

from acoustid_importer.streams import Stream

__all__ = [
    "SPECS",
    "TIMESTAMP_RE",
    "UUID_RE",
    "FieldSpec",
    "FingerprintRecord",
    "MetaRecord",
    "Record",
    "StreamSpec",
    "TrackFingerprintRecord",
    "TrackMbidRecord",
    "TrackMetaRecord",
    "TrackPuidRecord",
    "TrackRecord",
    "spec_for",
]

#: Zeitstempel des Dumps. Bewusst nachsichtig: die Historie reicht 15 Jahre
#: zurueck, die Nachkommastellen schwanken (``…:45.6684+00:00``), und ein zu
#: enger Ausdruck wuerde den Bootstrap an einer Formvariante abbrechen
#: lassen. Geprueft wird „sieht aus wie ein ISO-Zeitstempel", den Rest
#: erledigt Postgres.
TIMESTAMP_RE: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$"
)

#: UUID in kanonischer Schreibweise (``gid``, ``mbid``, ``puid``).
UUID_RE: Final = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")

#: Erwartete Typmenge eines Fingerprint-Vektors (siehe :func:`_is_int_vector`).
_INT_ONLY: Final = frozenset({int})


# --- Records ---------------------------------------------------------------
# Feldreihenfolge und Feldnamen exakt wie in der Tabelle in ARCHITECTURE §5.1.
# Kein Feld hat einen Default: der Parser setzt jedes explizit (Regel 3).


@dataclass(frozen=True, slots=True)
class TrackRecord:
    """``track-update`` -> Tabelle ``track``."""

    id: int
    gid: str
    new_id: int | None
    created: str
    updated: str | None


@dataclass(frozen=True, slots=True)
class MetaRecord:
    """``meta-update`` -> Tabelle ``meta``.

    Ausser ``id`` ist alles optional; ``created`` fehlt in der Praxis nie,
    ist upstream aber nullable (§5.1: solche Zeilen erscheinen nie im Delta).
    """

    id: int
    track: str | None
    artist: str | None
    album: str | None
    album_artist: str | None
    track_no: int | None
    disc_no: int | None
    year: int | None
    created: str | None


@dataclass(frozen=True, slots=True)
class FingerprintRecord:
    """``fingerprint-update`` -> Tabelle ``fingerprint`` (Vektor-Spalten).

    ``fingerprint`` ist der volle Vektor vorzeichenbehafteter int32
    (dekodierte Chromaprint-Hashes, **nicht** das Base64-Wire-Format der
    Submit-API). In den Suchindex geht spaeter nur der Query-Extrakt
    (:func:`shared.fpindex.extract_query`).
    """

    id: int
    fingerprint: list[int]
    length: int
    created: str


@dataclass(frozen=True, slots=True)
class TrackFingerprintRecord:
    """``track_fingerprint-update`` -> Tabelle ``fingerprint`` (Zuordnung).

    Zweite Projektion derselben Upstream-Tabelle wie
    :class:`FingerprintRecord`; ``fingerprint_id`` ist identisch mit ``id``.
    """

    id: int
    track_id: int
    fingerprint_id: int
    submission_count: int
    created: str
    updated: str | None


@dataclass(frozen=True, slots=True)
class TrackMbidRecord:
    """``track_mbid-update`` -> Tabelle ``track_mbid``.

    ``disabled`` erscheint im Dump **nur** bei ``true``; fehlt der
    Schluessel, ist der Wert ``False`` — nicht „unveraendert". Wer das
    anders macht, baut die Reaktivierungs-Falle aus §5.1 ein.
    """

    id: int
    track_id: int
    mbid: str
    submission_count: int
    disabled: bool
    created: str
    updated: str | None


@dataclass(frozen=True, slots=True)
class TrackMetaRecord:
    """``track_meta-update`` -> Tabelle ``track_meta``."""

    id: int
    track_id: int
    meta_id: int
    submission_count: int
    created: str
    updated: str | None


@dataclass(frozen=True, slots=True)
class TrackPuidRecord:
    """``track_puid-update`` -> Tabelle ``track_puid`` (Legacy-Strom).

    Laeuft in der Lueckenpruefung mit. Der Strom ist ueberwiegend, aber
    **nicht** dauerhaft leer (2011: 412k Zeilen/Tag; auch 2026 gibt es
    einzelne Tage mit Inhalt) — er wird deshalb regulaer geparst.
    """

    id: int
    track_id: int
    puid: str
    submission_count: int
    created: str
    updated: str | None


#: Union aller Record-Typen; der Parser liefert je Strom genau einen davon.
type Record = (
    TrackRecord
    | MetaRecord
    | FingerprintRecord
    | TrackFingerprintRecord
    | TrackMbidRecord
    | TrackMetaRecord
    | TrackPuidRecord
)


# --- Feldvertrag -----------------------------------------------------------


def _is_int(value: Any) -> bool:
    # `type(...) is int` statt isinstance: `True` ist in Python ein int und
    # waere sonst eine gueltige `id`.
    return type(value) is int


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _is_text(value: Any) -> bool:
    return type(value) is str


def _is_timestamp(value: Any) -> bool:
    return type(value) is str and TIMESTAMP_RE.match(value) is not None


def _is_uuid(value: Any) -> bool:
    return type(value) is str and UUID_RE.match(value) is not None


def _is_int_vector(value: Any) -> bool:
    # `set(map(type, …))` statt `all(type(x) is int for x in …)`: exakt
    # dasselbe Ergebnis, aber die Schleife laeuft in C. Auf dem
    # Fingerprint-Strom (390 GB gz, ~1300 Werte je Zeile) ist das der
    # Unterschied zwischen +16 % und +37 % Parse-Zeit — gemessen in Phase 6.
    return type(value) is list and (not value or set(map(type, value)) == _INT_ONLY)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Ein Feld eines Stroms: Name, Pflicht, Typpruefung, Absent-Default."""

    name: str
    #: Menschenlesbarer Typ fuer Fehlermeldungen (``int``, ``uuid`` …).
    kind: str
    required: bool
    #: Wert, wenn der Schluessel fehlt (Import-Regel 3).
    default: Any
    check: Callable[[Any], bool]

    def __str__(self) -> str:
        return self.name if self.required else f"({self.name})"


def _field(name: str, kind: str, *, required: bool = True, default: Any = None) -> FieldSpec:
    checks: Mapping[str, Callable[[Any], bool]] = {
        "int": _is_int,
        "bool": _is_bool,
        "text": _is_text,
        "timestamp": _is_timestamp,
        "uuid": _is_uuid,
        "int32[]": _is_int_vector,
    }
    return FieldSpec(name, kind, required, default, checks[kind])


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """Der vollstaendige Feldvertrag eines Stroms (ARCHITECTURE §5.1)."""

    stream: Stream
    record: type[Record]
    field_specs: tuple[FieldSpec, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """Alle Feldnamen in der Reihenfolge aus §5.1."""
        return tuple(spec.name for spec in self.field_specs)

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.field_specs if spec.required)

    @property
    def optional_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.field_specs if not spec.required)

    @property
    def known_names(self) -> frozenset[str]:
        """Fuer den Feld-Sanity-Check: alles, was nicht hier steht, ist neu."""
        return frozenset(self.names)


SPECS: Final[Mapping[Stream, StreamSpec]] = {
    Stream.TRACK: StreamSpec(
        Stream.TRACK,
        TrackRecord,
        (
            _field("id", "int"),
            _field("gid", "uuid"),
            _field("new_id", "int", required=False),
            _field("created", "timestamp"),
            _field("updated", "timestamp", required=False),
        ),
    ),
    Stream.META: StreamSpec(
        Stream.META,
        MetaRecord,
        (
            _field("id", "int"),
            _field("track", "text", required=False),
            _field("artist", "text", required=False),
            _field("album", "text", required=False),
            _field("album_artist", "text", required=False),
            _field("track_no", "int", required=False),
            _field("disc_no", "int", required=False),
            _field("year", "int", required=False),
            _field("created", "timestamp", required=False),
        ),
    ),
    Stream.FINGERPRINT: StreamSpec(
        Stream.FINGERPRINT,
        FingerprintRecord,
        (
            _field("id", "int"),
            _field("fingerprint", "int32[]"),
            _field("length", "int"),
            _field("created", "timestamp"),
        ),
    ),
    Stream.TRACK_FINGERPRINT: StreamSpec(
        Stream.TRACK_FINGERPRINT,
        TrackFingerprintRecord,
        (
            _field("id", "int"),
            _field("track_id", "int"),
            _field("fingerprint_id", "int"),
            _field("submission_count", "int"),
            _field("created", "timestamp"),
            _field("updated", "timestamp", required=False),
        ),
    ),
    Stream.TRACK_MBID: StreamSpec(
        Stream.TRACK_MBID,
        TrackMbidRecord,
        (
            _field("id", "int"),
            _field("track_id", "int"),
            _field("mbid", "uuid"),
            _field("submission_count", "int"),
            # Der einzige Nicht-None-Default im ganzen Format (Regel 3).
            _field("disabled", "bool", required=False, default=False),
            _field("created", "timestamp"),
            _field("updated", "timestamp", required=False),
        ),
    ),
    Stream.TRACK_META: StreamSpec(
        Stream.TRACK_META,
        TrackMetaRecord,
        (
            _field("id", "int"),
            _field("track_id", "int"),
            _field("meta_id", "int"),
            _field("submission_count", "int"),
            _field("created", "timestamp"),
            _field("updated", "timestamp", required=False),
        ),
    ),
    Stream.TRACK_PUID: StreamSpec(
        Stream.TRACK_PUID,
        TrackPuidRecord,
        (
            _field("id", "int"),
            _field("track_id", "int"),
            _field("puid", "uuid"),
            _field("submission_count", "int"),
            _field("created", "timestamp"),
            _field("updated", "timestamp", required=False),
        ),
    ),
}


def spec_for(stream: Stream) -> StreamSpec:
    """Feldvertrag eines Stroms."""
    return SPECS[stream]


# Selbstschutz beim Import: Spec und Record-Klasse duerfen nicht
# auseinanderlaufen (Feldnamen und -reihenfolge muessen gleich sein).
for _spec in SPECS.values():
    _record_names = tuple(field.name for field in fields(_spec.record))
    if _record_names != _spec.names:
        raise AssertionError(
            f"Spec und Record fuer {_spec.stream.value} passen nicht zusammen: "
            f"{_spec.names} != {_record_names}"
        )
del _spec, _record_names
