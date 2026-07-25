"""Upsert-Bausteine des DB-Imports (ARCHITECTURE §5.2, Import-Regeln 2 und 3).

Je Strom steht hier genau ein ``INSERT … ON CONFLICT (id) DO UPDATE`` und
genau eine Funktion, die einen Record in die Parameterliste dieses
Statements uebersetzt. Beides ist pure Logik: kein Cursor, keine Verbindung,
keine Uhr — der eigentliche Import (:mod:`acoustid_importer.dbimport`)
fuehrt die Statements nur noch aus.

**Regel 2 — ein Upsert je Zeile, zwei fuer ``fingerprint``.** Der Delta-Strom
kennt keine Operationsmarker: jede Zeile ist ein Upsert auf den
Primaerschluessel ``id``. Die Tabelle ``fingerprint`` wird von **zwei**
Stroemen befuellt (``fingerprint-update`` liefert den Vektor,
``track_fingerprint-update`` die Zuordnung). Ihre ``DO UPDATE SET``-Listen
sind deshalb **disjunkt** — jeder Strom schreibt nur seine eigenen Spalten,
und keiner setzt die des anderen auf NULL zurueck. Einzige Ausnahme ist
``created``, das beide liefern: es wird per
``COALESCE(fingerprint.created, EXCLUDED.created)`` nur gefuellt, nie
ueberschrieben. Ein Selbsttest beim Import dieses Moduls haelt die
Disjunktheit fest.

**Regel 3 — Absent bedeutet Default, nicht „unveraendert".** Der Parser hat
jedes fehlende Feld bereits explizit auf ``None`` bzw. bei
``track_mbid.disabled`` auf ``False`` gesetzt. Die Statements schreiben
darum **immer die volle Spaltenliste**; ein Teil-Patch (nur vorhandene
Felder) wuerde genau die Reaktivierungs-Falle aus §5.1 einbauen.

**Buchfuehrung.** Zu jeder Zeile kommen die beiden projektspezifischen
Spalten aus §5.2: ``src_day`` (die Tagesdatei der letzten Anwendung, als
Parameter mitgegeben) und ``imported_at`` (``now()``, also der Beginn der
Datei-Transaktion — alle Zeilen einer Tagesdatei tragen denselben Wert).
Sie stehen bewusst in **beiden** Fingerprint-Upserts: sie protokollieren,
welcher Lauf die Zeile zuletzt angefasst hat, und gehoeren keinem der
beiden Stroeme allein.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from operator import attrgetter
from typing import Any, Final

from acoustid_importer.errors import DbImportError
from acoustid_importer.records import Record, TrackFingerprintRecord
from acoustid_importer.streams import IMPORT_ORDER, Stream

__all__ = [
    "BOOKKEEPING_COLUMNS",
    "BOOKKEEPING_UPDATES",
    "UPSERTS",
    "Upsert",
    "upsert_for",
]

#: Spalten, die der Importer selbst beisteuert (§5.2, [P]). ``src_day`` kommt
#: als Parameter, ``imported_at`` als SQL-Ausdruck.
BOOKKEEPING_COLUMNS: Final[tuple[str, ...]] = ("src_day",)

#: Buchfuehrung im ``DO UPDATE SET`` — bei jedem Strom identisch.
BOOKKEEPING_UPDATES: Final[tuple[tuple[str, str], ...]] = (
    ("src_day", "EXCLUDED.src_day"),
    ("imported_at", "now()"),
)


@dataclass(frozen=True, slots=True)
class Upsert:
    """Das Upsert-Rezept eines Stroms: Statement plus Record-Uebersetzung."""

    stream: Stream
    #: Zieltabelle (``fingerprint`` fuer beide Fingerprint-Stroeme).
    table: str
    #: Spalten der ``VALUES``-Liste, in Parameterreihenfolge; die letzten
    #: sind :data:`BOOKKEEPING_COLUMNS`.
    columns: tuple[str, ...]
    #: ``DO UPDATE SET`` als (Spalte, SQL-Ausdruck)-Paare, inkl. Buchfuehrung.
    updates: tuple[tuple[str, str], ...]
    #: Fertiges SQL mit ``%s``-Platzhaltern.
    statement: str
    #: Record -> Werte der Dump-Spalten (ohne Buchfuehrung).
    read: Callable[[Any], tuple[Any, ...]]

    @property
    def data_columns(self) -> tuple[str, ...]:
        """Spalten aus dem Dump — die Buchfuehrung zaehlt nicht mit."""
        return self.columns[: -len(BOOKKEEPING_COLUMNS)]

    @property
    def update_columns(self) -> tuple[str, ...]:
        """Alle Spalten, die ein Konflikt fortschreibt."""
        return tuple(name for name, _ in self.updates)

    @property
    def data_update_columns(self) -> frozenset[str]:
        """Fortgeschriebene Dump-Spalten — Grundlage der Disjunktheitspruefung.

        ``created`` zaehlt hier bewusst **nicht** mit: die beiden
        Fingerprint-Stroeme fuellen es nur per COALESCE und ueberschreiben
        sich damit gegenseitig nicht.
        """
        book = {name for name, _ in BOOKKEEPING_UPDATES}
        return frozenset(
            name
            for name, expression in self.updates
            if name not in book and expression.startswith("EXCLUDED.")
        )

    def row(self, record: Record, src_day: date) -> tuple[Any, ...]:
        """Ein Record -> die Parameterliste von :attr:`statement`.

        Raises:
            DbImportError: Der Record verletzt eine Zusicherung des
                Datenmodells (siehe :func:`_read_track_fingerprint`).
        """
        return (*self.read(record), src_day)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(stream={self.stream.value!r}, table={self.table!r})"


# --- Record -> Spaltenwerte -------------------------------------------------
# `attrgetter` mit mehreren Namen liefert direkt ein Tupel und laeuft in C —
# auf dem Fingerprint-Strom (390 GB gz) ist das der Heisspfad schlechthin.

_read_track = attrgetter("id", "gid", "new_id", "created", "updated")
_read_meta = attrgetter(
    "id", "track", "artist", "album", "album_artist", "track_no", "disc_no", "year", "created"
)
_read_fingerprint = attrgetter("id", "fingerprint", "length", "created")
_read_track_mbid = attrgetter(
    "id", "track_id", "mbid", "submission_count", "disabled", "created", "updated"
)
_read_track_meta = attrgetter("id", "track_id", "meta_id", "submission_count", "created", "updated")
_read_track_puid = attrgetter("id", "track_id", "puid", "submission_count", "created", "updated")


def _read_track_fingerprint(record: TrackFingerprintRecord) -> tuple[Any, ...]:
    """Wie die anderen ``_read_*`` — mit einer Zusicherungspruefung.

    ``track_fingerprint-update`` ist die zweite Projektion **derselben**
    Upstream-Tabelle wie ``fingerprint-update``; ``fingerprint_id`` ist
    laut §5.1 immer identisch mit ``id``. Die Spalte wird deshalb nicht
    geschrieben — aber geprueft: waeren die beiden je verschieden, schriebe
    der Import die Zuordnung an die falsche Fingerprint-Zeile. Der Vergleich
    kostet nichts und faellt lieber laut aus, als still falsch zu verknuepfen.

    Raises:
        DbImportError: ``fingerprint_id`` weicht von ``id`` ab.
    """
    if record.fingerprint_id != record.id:
        raise DbImportError(
            f"track_fingerprint-Zeile {record.id}: fingerprint_id ist "
            f"{record.fingerprint_id}, laut ARCHITECTURE §5.1 muss sie mit id "
            "uebereinstimmen — der Feldsatz der Quelle hat sich geaendert"
        )
    return (record.id, record.track_id, record.submission_count, record.created, record.updated)


# --- Statements -------------------------------------------------------------


def _own(*columns: str) -> tuple[tuple[str, str], ...]:
    """Spalten, die dieser Strom besitzt: schlicht aus ``EXCLUDED``."""
    return tuple((name, f"EXCLUDED.{name}") for name in columns)


#: ``created`` liefern beide Fingerprint-Stroeme. Nur fuellen, nie
#: ueberschreiben — so bleibt der zuerst gesehene Wert stehen und keiner der
#: beiden Stroeme haengt davon ab, wer zuerst da war.
_FINGERPRINT_CREATED: Final[tuple[str, str]] = (
    "created",
    "COALESCE(fingerprint.created, EXCLUDED.created)",
)


def _statement(table: str, columns: tuple[str, ...], updates: tuple[tuple[str, str], ...]) -> str:
    """Baut das ``INSERT … ON CONFLICT (id) DO UPDATE``.

    Alle Bezeichner sind Konstanten dieses Moduls (nie Eingabedaten), das
    Zusammensetzen per f-String ist damit unbedenklich; die *Werte* gehen
    ausschliesslich als ``%s``-Parameter.
    """
    names = ", ".join(columns)
    placeholders = ", ".join("%s" for _ in columns)
    assignments = ",\n           ".join(f"{name} = {expression}" for name, expression in updates)
    return (
        f"INSERT INTO {table} ({names})\n"
        f"    VALUES ({placeholders})\n"
        f"    ON CONFLICT (id) DO UPDATE\n"
        f"       SET {assignments}"
    )


def _upsert(
    stream: Stream,
    data_columns: tuple[str, ...],
    updates: tuple[tuple[str, str], ...],
    read: Callable[[Any], tuple[Any, ...]],
) -> Upsert:
    columns = data_columns + BOOKKEEPING_COLUMNS
    all_updates = updates + BOOKKEEPING_UPDATES
    return Upsert(
        stream=stream,
        table=stream.table,
        columns=columns,
        updates=all_updates,
        statement=_statement(stream.table, columns, all_updates),
        read=read,
    )


UPSERTS: Final[Mapping[Stream, Upsert]] = {
    Stream.TRACK: _upsert(
        Stream.TRACK,
        ("id", "gid", "new_id", "created", "updated"),
        _own("gid", "new_id", "created", "updated"),
        _read_track,
    ),
    Stream.META: _upsert(
        Stream.META,
        (
            "id",
            "track",
            "artist",
            "album",
            "album_artist",
            "track_no",
            "disc_no",
            "year",
            "created",
        ),
        _own("track", "artist", "album", "album_artist", "track_no", "disc_no", "year", "created"),
        _read_meta,
    ),
    # Strom 1 von 2 auf `fingerprint`: der Vektor.
    Stream.FINGERPRINT: _upsert(
        Stream.FINGERPRINT,
        ("id", "fingerprint", "length", "created"),
        (*_own("fingerprint", "length"), _FINGERPRINT_CREATED),
        _read_fingerprint,
    ),
    # Strom 2 von 2 auf `fingerprint`: die Zuordnung. Disjunkt zu oben.
    Stream.TRACK_FINGERPRINT: _upsert(
        Stream.TRACK_FINGERPRINT,
        ("id", "track_id", "submission_count", "created", "updated"),
        (*_own("track_id", "submission_count", "updated"), _FINGERPRINT_CREATED),
        _read_track_fingerprint,
    ),
    Stream.TRACK_MBID: _upsert(
        Stream.TRACK_MBID,
        ("id", "track_id", "mbid", "submission_count", "disabled", "created", "updated"),
        # `disabled` steht ausdruecklich mit drin: der Dump liefert den
        # Schluessel nur bei true, der Parser setzt ihn sonst auf false —
        # wer ihn hier auslaesst, kann eine Zuordnung nie reaktivieren (§5.1).
        _own("track_id", "mbid", "submission_count", "disabled", "created", "updated"),
        _read_track_mbid,
    ),
    Stream.TRACK_META: _upsert(
        Stream.TRACK_META,
        ("id", "track_id", "meta_id", "submission_count", "created", "updated"),
        _own("track_id", "meta_id", "submission_count", "created", "updated"),
        _read_track_meta,
    ),
    Stream.TRACK_PUID: _upsert(
        Stream.TRACK_PUID,
        ("id", "track_id", "puid", "submission_count", "created", "updated"),
        _own("track_id", "puid", "submission_count", "created", "updated"),
        _read_track_puid,
    ),
}


def upsert_for(stream: Stream) -> Upsert:
    """Das Upsert-Rezept eines Stroms."""
    return UPSERTS[stream]


# --- Selbstschutz beim Import ----------------------------------------------
# Dieselbe Idee wie in `records.py`: was die Doku zusichert, faellt hier beim
# Laden des Moduls auf und nicht erst nach dem halben Bootstrap.

if set(UPSERTS) != set(IMPORT_ORDER):  # pragma: no cover - Konstruktionsfehler
    missing = sorted(stream.value for stream in set(IMPORT_ORDER) - set(UPSERTS))
    raise AssertionError(f"Kein Upsert fuer die Stroeme: {missing}")

for _upsert_item in UPSERTS.values():  # pragma: no cover - Konstruktionsfehler
    if _upsert_item.columns[0] != "id":
        raise AssertionError(f"{_upsert_item.stream.value}: erste Spalte muss 'id' sein")
    if "id" in _upsert_item.update_columns:
        raise AssertionError(
            f"{_upsert_item.stream.value}: 'id' ist der Konfliktschluessel und darf "
            "nicht fortgeschrieben werden"
        )
    if _upsert_item.columns[-len(BOOKKEEPING_COLUMNS) :] != BOOKKEEPING_COLUMNS:
        raise AssertionError(f"{_upsert_item.stream.value}: Buchfuehrung fehlt am Ende")

# Import-Regel 2: die beiden Projektionen von `fingerprint` duerfen sich
# nicht in die Spalten schreiben.
_shared = (
    UPSERTS[Stream.FINGERPRINT].data_update_columns
    & UPSERTS[Stream.TRACK_FINGERPRINT].data_update_columns
)
if _shared:  # pragma: no cover - Konstruktionsfehler
    raise AssertionError(
        "fingerprint und track_fingerprint muessen disjunkte DO-UPDATE-Spalten "
        f"haben (Import-Regel 2), gemeinsam sind: {sorted(_shared)}"
    )

del _upsert_item, _shared
