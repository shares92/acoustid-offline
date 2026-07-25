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
"""

from __future__ import annotations

__all__ = [
    "DeltaNotFoundError",
    "DownloadError",
    "GapError",
    "ImporterError",
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
