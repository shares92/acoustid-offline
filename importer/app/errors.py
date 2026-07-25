"""Fehlerhierarchie des Importers (ARCHITECTURE §5.1, §5.2).

Alle Fehler stammen von :class:`ImporterError` ab, damit ein Job-Rumpf den
Importer pauschal absichern kann. Darunter trennen sich drei Welten:

* **Arbeitsliste** (:class:`GapError`) — die Lueckenpruefung nach
  Import-Regel 5 hat einen fehlenden Kalendertag gefunden. Eine *leere*
  Datei ist keine Luecke, eine *fehlende* schon.
* **Download** (:class:`DownloadError`) — die Tagesdatei kam nicht sauber
  vom Server: kein Netz, Fehlerstatus, falsche Groesse, kaputtes gzip.
  :class:`DeltaNotFoundError` ist der Sonderfall „Datei existiert upstream
  nicht" — genau die Luecke aus Regel 5, nur zur Ladezeit bemerkt.
* **Parser** (:class:`ParseError`) — eine Zeile ist kein gueltiges JSON,
  ein Pflichtfeld fehlt oder ein Wert hat den falschen Typ. Der Fehler
  nennt immer Datei und Zeilennummer, weil ein Parse-Fehler laut
  Import-Regel 4 die ganze Datei-Transaktion abbricht.
* **DB-Import** (:class:`DbImportError`) — die Tagesdatei liess sich nicht
  einspielen, weil eine Zusicherung des Datenmodells verletzt ist oder die
  Verbindung nicht in dem Zustand ist, den „eine Datei = eine Transaktion"
  verlangt. Reine Datenbankfehler bleiben ``psycopg.Error``; sie rollen die
  Datei-Transaktion genauso zurueck.
* **Index-Feed** (:class:`IndexFeedError`) — die Uebergabe neuer
  Fingerprints an den acoustid-index ist gescheitert. Transport- und
  Protokollfehler des Index bleiben ``FpIndexError`` aus dem shared-Paket.
* **Job-Rumpf** (Phase 8) — :class:`DiskSpaceError` ist der kontrollierte
  Abbruch des Plattenplatz-Guards (Invariante §8.8), :class:`BulkModeError`
  meldet, dass die Bulk-Einstellungen des Bootstraps nicht gesetzt oder
  nicht zurueckgenommen werden konnten.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "BulkModeError",
    "DbImportError",
    "DeltaNotFoundError",
    "DiskSpaceError",
    "DownloadError",
    "GapError",
    "ImporterError",
    "IndexFeedError",
    "ParseError",
    "SizeMismatchError",
]


class ImporterError(Exception):
    """Basis aller Importer-Fehler."""


class GapError(ImporterError):
    """Fehlende Tagesdateien in der Vergangenheit (Import-Regel 5)."""

    def __init__(self, message: str, gaps: tuple[object, ...] = ()) -> None:
        super().__init__(message)
        #: Die gefundenen :class:`~acoustid_importer.worklist.Gap`-Objekte.
        self.gaps = gaps


class DownloadError(ImporterError):
    """Eine Tagesdatei konnte nicht vollstaendig geholt werden."""


class DeltaNotFoundError(DownloadError):
    """Der Server kennt die Datei nicht (HTTP 404 bzw. nicht im index.json).

    Fuer einen Tag in der Vergangenheit ist das die Luecke aus
    Import-Regel 5 und kein voruebergehendes Problem — Wiederholen hilft
    nicht, deshalb wird hier nicht erneut versucht.
    """


class SizeMismatchError(DownloadError):
    """Die geladene Datei hat nicht die im ``index.json`` genannte Groesse."""


class DbImportError(ImporterError):
    """Eine Tagesdatei liess sich nicht in die Postgres einspielen.

    Gemeint sind die Faelle, die der Importer selbst erkennt — eine
    verletzte Zusicherung des Datenmodells (``track_fingerprint.id`` weicht
    von ``fingerprint_id`` ab) oder eine Verbindung mit bereits offener
    Transaktion. Fehler der Datenbank selbst bleiben ``psycopg.Error``.
    """


class IndexFeedError(ImporterError):
    """Der Index-Feed konnte nicht sauber durchlaufen (ARCHITECTURE §5.3)."""


class DiskSpaceError(ImporterError):
    """Der freie Plattenplatz unterschreitet ``update.min_free_gb`` (§8.8).

    Kein Fehler im Sinne von „kaputt", sondern der **kontrollierte Abbruch**:
    der Lauf hoert zwischen zwei Tagesdateien auf, ``import_state`` steht auf
    der letzten vollstaendigen Datei, und der naechste Lauf setzt dort fort.

    Args:
        message: Meldung fuer Log und Report.
        path: Das gemessene Verzeichnis.
        free_bytes: Gemessener freier Platz; ``None``, wenn nicht messbar.
        min_free_bytes: Geforderte Reserve in Byte.
    """

    def __init__(
        self,
        message: str,
        *,
        path: Path,
        free_bytes: int | None,
        min_free_bytes: int,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.free_bytes = free_bytes
        self.min_free_bytes = min_free_bytes


class BulkModeError(ImporterError):
    """Die Bulk-Einstellungen des Bootstraps sind nicht sauber fuehrbar.

    Entweder verlangt der Bulk-Modus eine Verbindung, die er nicht bekommt
    (Sitzungseinstellungen brauchen ``autocommit`` und keine offene
    Transaktion — in einer zurueckgerollten Transaktion waere ein ``SET``
    wieder weg), oder das Zuruecknehmen ist gescheitert. Beides ist laut zu
    melden: unsichere Einstellungen duerfen den Lauf nicht ueberleben.
    """


class ParseError(ImporterError):
    """Eine Zeile einer Tagesdatei ist unlesbar (Import-Regel 4).

    Args:
        detail: Was genau nicht stimmt.
        source: Dateiname, wie er im Log erscheinen soll.
        line_no: 1-basierte Zeilennummer in der entpackten Datei.
    """

    def __init__(self, detail: str, *, source: str, line_no: int) -> None:
        super().__init__(f"{source}:{line_no}: {detail}")
        self.detail = detail
        self.source = source
        self.line_no = line_no
