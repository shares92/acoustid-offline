"""Messwerte fuer den Probelauf: DB-Groesse, Index-Groesse, Durchsatz.

Der Vollimport ist ein Blindflug, solange niemand weiss, wie lange er
dauert und wie viel Platz er braucht (DECISIONS „Bootstrap per Voll-Replay",
ARCHITECTURE §12 Punkt 11). Deshalb misst der Probelauf — ein Lauf ueber
einen begrenzten Zeitraum (``--end-date``) — genau drei Dinge und rechnet
sie auf den Vollbestand hoch:

1. **Dauer** je verarbeitetem gz-Byte (der Job-Rumpf stoppt die Zeit),
2. **DB-Groesse** vor und nach dem Lauf (:func:`database_size`),
3. **Index-Groesse** vor und nach dem Feed (:func:`index_size`).

Die Hochrechnung selbst ist eine pure Funktion in
:mod:`acoustid_importer.report` — hier steht nur das Messen.

**Warum die Index-Groesse in Byte oft fehlt.** Der acoustid-index gibt ueber
seine API nur die Dokumentzahl her; sein Datenverzeichnis liegt in einem
anderen Container. Wer die Bytegroesse braucht, mountet das Index-Volume
zusaetzlich read-only in den Importer und nennt den Pfad (``--index-dir``);
sonst bleibt das Feld ``null`` und die Hochrechnung nutzt die Dokumentzahl.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from psycopg import Connection

from shared.fpindex import FpIndexClient, FpIndexError, FpIndexNotFoundError

__all__ = [
    "MEASURED_TABLES",
    "DbSize",
    "IndexSize",
    "database_size",
    "directory_bytes",
    "index_size",
]

_LOG = logging.getLogger(__name__)

#: Tabellen, deren Einzelgroesse im Report auftaucht (ARCHITECTURE §5.2).
MEASURED_TABLES: Final[tuple[str, ...]] = (
    "track",
    "fingerprint",
    "track_mbid",
    "meta",
    "track_meta",
    "track_puid",
    "import_state",
)

_DB_BYTES: Final = "SELECT pg_database_size(current_database())"

#: Groesse inkl. Indizes und TOAST; unbekannte Tabellen liefern NULL statt
#: eines Fehlers (waehrend des Bootstraps existieren die Indizes noch nicht,
#: die Tabellen aber schon).
_TABLE_BYTES: Final = """
SELECT name, pg_total_relation_size(to_regclass(name))
  FROM unnest(%s::text[]) AS t(name)
"""


@dataclass(frozen=True, slots=True)
class DbSize:
    """Belegter Platz der AcoustID-Postgres."""

    total_bytes: int
    #: Tabellenname -> Bytes inkl. Indizes und TOAST.
    tables: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"total_bytes": self.total_bytes, "tables": dict(self.tables)}


@dataclass(frozen=True, slots=True)
class IndexSize:
    """Groesse des acoustid-index, soweit von aussen sichtbar."""

    #: Dokumente laut ``GET /:index`` (Tombstones zaehlen mit).
    documents: int | None
    version: int | None
    #: Bytes des Datenverzeichnisses — nur messbar, wenn es gemountet ist.
    bytes: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {"documents": self.documents, "version": self.version, "bytes": self.bytes}


def database_size(conn: Connection, tables: tuple[str, ...] = MEASURED_TABLES) -> DbSize:
    """Misst Datenbank- und Tabellengroessen.

    Args:
        conn: Verbindung zur AcoustID-Postgres.
        tables: Tabellen, deren Einzelgroesse interessiert.

    Returns:
        :class:`DbSize`; noch nicht existierende Tabellen fehlen im Mapping.
    """
    row = conn.execute(_DB_BYTES).fetchone()
    total = int(row[0]) if row is not None and row[0] is not None else 0
    sizes: dict[str, int] = {}
    for name, size in conn.execute(_TABLE_BYTES, (list(tables),)).fetchall():
        if size is not None:
            sizes[name] = int(size)
    return DbSize(total_bytes=total, tables=sizes)


def index_size(client: FpIndexClient, *, data_dir: Path | None = None) -> IndexSize:
    """Fragt Dokumentzahl und Version des Suchindex ab.

    Ein **noch nicht angelegter** Index ist kein Fehler, sondern der
    Normalfall vor dem allerersten Feed: er haelt null Dokumente. Nur so
    ergibt der Vorher-nachher-Vergleich des Probelaufs eine Zahl.

    Andere Fehler des Index sind hier ebenfalls **kein** Abbruchgrund: die
    Messung ist Zusatzinformation fuer den Report, nicht Teil des Imports.
    Sie werden geloggt, das Feld bleibt ``None``.
    """
    documents: int | None = None
    version: int | None = None
    try:
        info = client.index_info()
    except FpIndexNotFoundError:
        _LOG.debug("Suchindex existiert noch nicht — er haelt null Dokumente")
        documents = 0
    except FpIndexError as error:
        _LOG.warning("Index-Groesse nicht messbar", extra={"reason": str(error).strip()})
    else:
        documents = info.num_docs
        version = info.version
    return IndexSize(
        documents=documents,
        version=version,
        bytes=directory_bytes(data_dir) if data_dir is not None else None,
    )


def directory_bytes(path: Path) -> int | None:
    """Summe der Dateigroessen unterhalb von ``path`` (``None``, wenn nicht lesbar)."""
    try:
        total = 0
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError as error:
        _LOG.warning(
            "Verzeichnisgroesse nicht messbar",
            extra={"measure_path": str(path), "reason": str(error)},
        )
        return None
    return total
