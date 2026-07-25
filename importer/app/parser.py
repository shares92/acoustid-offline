"""JSONL-Parser der Tagesdateien (ARCHITECTURE §5.1, §5.2 Regel 3/4, §12.8).

Ein :class:`DeltaReader` liest genau eine Tagesdatei und liefert die Zeilen
als typisierte Records (:mod:`acoustid_importer.records`). Er ist ein
**Iterator**: die 2011er-Fingerprint-Dateien sind bis 4 GiB gross, es wird
nie eine ganze Datei in den Speicher geholt.

Eigenschaften:

* **Direkter zeilenweiser JSON-Parse** (Import-Regel 4) — kein
  COPY-FROM-Staging, das wuerde valides JSONL korrumpieren.
* **Absent-Semantik zentral** (Import-Regel 3): fehlender Schluessel =>
  ``None``, ``track_mbid.disabled`` => ``False``. Jedes Feld wird explizit
  gesetzt, nie „unveraendert gelassen".
* **Feld-Sanity-Check je Strom** (§12.8): ein unbekanntes Feld ist eine
  Warnung — genau **einmal je Datei und Feldname**, nicht je Zeile; ein
  fehlendes Pflichtfeld oder ein Typfehler ist ein harter Fehler mit
  Dateiname und Zeilennummer (:class:`~acoustid_importer.errors.ParseError`).
* **Leere Datei** (23-Byte-gz) => null Records, kein Fehler.

**Zwei Escaping-Epochen der Quelle (empirisch, Phase 6).** Bis
einschliesslich :data:`COPY_TEXT_LAST_DAY` (2024-12-04) liegt ueber dem
JSON zusaetzlich das Text-Escaping von ``COPY … TO STDOUT``: jeder
Backslash ist verdoppelt. Sichtbar wird das nur im Strom ``meta``, dem
einzigen mit Freitext — dort sind je nach Tag 0,5 bis 2,5 % der Zeilen **kein
gueltiges JSON** (Beispiel: ein Titel mit Zoll-Zeichen), und Werte mit
Tabulator oder Backslash kommen doppelt escaped an. Ab 2024-12-05 sind die
Dateien sauberes JSONL. Beides ist dauerhaft: die Tagesdateien sind
upstream unveraenderlich, die Historie bleibt also fuer immer gemischt.

Der Reader waehlt die Lesart per Vorgabe nach dem Tag der Datei
(:class:`Escaping` ``AUTO``) und faellt zeilenweise auf die jeweils andere
zurueck, falls eine Zeile so nicht lesbar ist — die Epochengrenze kann so
nie einen Bootstrap abbrechen, und der Rueckfall wird gezaehlt und einmal
je Datei gemeldet. Wer den Rohzustand braucht, setzt ``escaping`` fest auf
``NONE``.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Self

from acoustid_importer.errors import ParseError
from acoustid_importer.records import Record, StreamSpec, spec_for
from acoustid_importer.streams import DeltaFile, Stream

__all__ = [
    "COPY_TEXT_LAST_DAY",
    "DeltaReader",
    "Escaping",
    "ParseStats",
    "escaping_for_day",
    "unescape_copy_text",
]

_LOG = logging.getLogger(__name__)

#: Letzter Tag, dessen Dateien zusaetzlich COPY-Text-escaped sind.
#: Empirisch bestimmt (Phase 6): ``2024-12-04-meta-update`` ist die letzte
#: Datei mit ungueltigen JSON-Zeilen, ``2024-12-05`` die erste saubere.
COPY_TEXT_LAST_DAY: Final = date(2024, 12, 4)

#: Ausschnitt einer kaputten Zeile in der Fehlermeldung.
_EXCERPT_CHARS: Final = 160


class Escaping(StrEnum):
    """Wie eine Zeile vor dem JSON-Parse zu behandeln ist."""

    #: Nach dem Tag der Datei entscheiden (Vorgabe).
    AUTO = "auto"
    #: Sauberes JSONL (Dateien ab 2024-12-05).
    NONE = "none"
    #: Zusaetzliches COPY-Text-Escaping entfernen (Dateien bis 2024-12-04).
    COPY_TEXT = "copy_text"


def escaping_for_day(day: date | None) -> Escaping:
    """Welche Lesart gilt fuer eine Datei dieses Tages?

    Ohne Tag (``None``) wird sauberes JSONL angenommen; der zeilenweise
    Rueckfall faengt den Irrtum ab.
    """
    if day is not None and day <= COPY_TEXT_LAST_DAY:
        return Escaping.COPY_TEXT
    return Escaping.NONE


def unescape_copy_text(line: bytes) -> bytes:
    r"""Macht das Text-Escaping von ``COPY … TO STDOUT`` rueckgaengig.

    Postgres verdoppelt im Textformat jeden Backslash; Steuerzeichen
    (``\n``, ``\t`` …) hat ``row_to_json`` vorher schon JSON-escaped, im
    Strom steht davon also nur der verdoppelte Backslash. Die Umkehr ist
    deshalb genau eine Ersetzung von links nach rechts: ``\\`` -> ``\``.
    """
    return line.replace(b"\\\\", b"\\")


@dataclass(slots=True)
class ParseStats:
    """Zaehler eines Datei-Durchlaufs (Phase 7 schreibt ``records`` fort)."""

    #: Gelesene Zeilen der entpackten Datei (inkl. Leerzeilen).
    lines: int = 0
    #: Erzeugte Records.
    records: int = 0
    #: Uebersprungene Leerzeilen.
    blank_lines: int = 0
    #: Zeilen, die erst in der anderen Escaping-Lesart lesbar waren.
    escaping_fallbacks: int = 0
    #: Unbekannte Felder (§12.8) mit ihrer Trefferzahl.
    unknown_fields: dict[str, int] = field(default_factory=dict)


class DeltaReader:
    """Liest eine Tagesdatei und liefert typisierte Records.

    Beispiel::

        reader = DeltaReader(Path("dumps/2026-07-22-meta-update.jsonl.gz"))
        for record in reader:  # streamt, haelt nie die ganze Datei
            ...
        reader.stats.records  # Zeilenzahl fuer `import_state`

    Strom und Tag werden aus dem Dateinamen abgeleitet; wer eine Datei unter
    anderem Namen liest, gibt beides mit.
    """

    def __init__(
        self,
        path: Path,
        *,
        stream: Stream | None = None,
        day: date | None = None,
        escaping: Escaping = Escaping.AUTO,
        lines: Iterable[bytes] | None = None,
    ) -> None:
        """
        Args:
            path: gzip-JSONL-Datei. Mit ``lines`` nur noch Quellenname.
            stream: Strom; per Vorgabe aus dem Dateinamen.
            day: Tag der Datei; per Vorgabe aus dem Dateinamen. Bestimmt in
                ``AUTO`` die Escaping-Lesart.
            escaping: Lesart der Zeilen, siehe :class:`Escaping`.
            lines: Fertige Rohzeilen statt der Datei (Tests, Teilstroeme).
                Werden genau einmal durchlaufen; bequemer ueber
                :meth:`from_lines`.

        Raises:
            ValueError: Der Dateiname passt nicht zum Schema und ``stream``
                wurde nicht mitgegeben.
        """
        if stream is None or day is None:
            delta = DeltaFile.from_name(path.name)
            stream = stream or delta.stream
            day = day or delta.day
        self.path = path
        self.stream: Stream = stream
        self.day: date | None = day
        self.source = path.name
        self.escaping = escaping
        self.stats = ParseStats()
        self._lines = lines
        self._spec: StreamSpec = spec_for(stream)

    @classmethod
    def from_lines(
        cls,
        lines: Iterable[bytes],
        stream: Stream,
        *,
        source: str = "<zeilen>",
        day: date | None = None,
        escaping: Escaping = Escaping.AUTO,
    ) -> Self:
        """Reader ueber bereits vorliegende Rohzeilen (Tests, Teilstroeme)."""
        return cls(Path(source), stream=stream, day=day, escaping=escaping, lines=lines)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(source={self.source!r}, stream={self.stream.value!r})"

    def __iter__(self) -> Iterator[Record]:
        """Streamt die Datei; setzt :attr:`stats` zu Beginn zurueck."""
        self.stats = ParseStats()
        if self._lines is not None:
            lines, self._lines = self._lines, None
            yield from self._parse(lines)
            return
        try:
            with gzip.open(self.path, "rb") as raw:
                yield from self._parse(raw)
        except (OSError, EOFError) as exc:
            # gzip.BadGzipFile und abgeschnittene Streams sind OSError bzw.
            # EOFError: dann ist die ganze Datei unbrauchbar, nicht eine Zeile.
            raise ParseError(
                f"gzip-Strom nicht lesbar: {exc}",
                source=self.source,
                line_no=self.stats.lines + 1,
            ) from exc

    # --- Innenleben --------------------------------------------------------

    def _parse(self, lines: Iterable[bytes]) -> Iterator[Record]:
        stats = self.stats
        spec = self._spec
        record_type = spec.record
        field_specs = spec.field_specs
        known = spec.known_names
        primary = self.escaping
        if primary is Escaping.AUTO:
            primary = escaping_for_day(self.day)
        alternate = Escaping.NONE if primary is Escaping.COPY_TEXT else Escaping.COPY_TEXT
        warned_fields: set[str] = set()
        warned_escaping = False

        for line in lines:
            stats.lines += 1
            line_no = stats.lines
            # Jede Datenzeile beginnt mit '{' (row_to_json). Die Pruefung davor
            # spart das `strip()` — auf dem Fingerprint-Strom waere das eine
            # Kopie von bis zu 30 kB je Zeile, nur um Leerzeilen zu erkennen.
            if not line.startswith(b"{") and not line.strip():
                stats.blank_lines += 1
                continue

            row = self._decode(line, line_no, primary, alternate)
            if stats.escaping_fallbacks and not warned_escaping:
                warned_escaping = True
                _LOG.warning(
                    "Zeile nur in der anderen Escaping-Lesart lesbar",
                    extra={
                        "delta_file": self.source,
                        "line": line_no,
                        "escaping": primary.value,
                        "fallback": alternate.value,
                    },
                )

            values: dict[str, Any] = {}
            found = 0
            for field_spec in field_specs:
                value = row.get(field_spec.name, _MISSING)
                if value is _MISSING:
                    if field_spec.required:
                        raise ParseError(
                            f"Pflichtfeld {field_spec.name!r} fehlt (Strom {self.stream.value})",
                            source=self.source,
                            line_no=line_no,
                        )
                    values[field_spec.name] = field_spec.default
                    continue
                found += 1
                if value is None and not field_spec.required:
                    # Bei json_strip_nulls kann kein ausdruecklicher `null`
                    # vorkommen; falls doch, heisst er dasselbe wie „fehlt".
                    values[field_spec.name] = field_spec.default
                    continue
                if not field_spec.check(value):
                    raise ParseError(
                        f"Feld {field_spec.name!r} hat den falschen Typ: "
                        f"{field_spec.kind} erwartet, {_describe(value)} bekommen "
                        f"(Strom {self.stream.value})",
                        source=self.source,
                        line_no=line_no,
                    )
                values[field_spec.name] = value

            if found != len(row):
                # Erst hier den Mengenvergleich zahlen: im Normalfall stimmen
                # die Zaehler ueberein und die Zeile ist ohne Extrakosten durch.
                self._warn_unknown(row.keys() - known, line_no, warned_fields)

            stats.records += 1
            yield record_type(**values)

        _LOG.debug(
            "Tagesdatei geparst",
            extra={
                "delta_file": self.source,
                "stream": self.stream.value,
                "records": stats.records,
                "escaping": primary.value,
                "escaping_fallbacks": stats.escaping_fallbacks,
                "unknown_fields": sorted(stats.unknown_fields),
            },
        )

    def _decode(
        self, line: bytes, line_no: int, primary: Escaping, alternate: Escaping
    ) -> dict[str, Any]:
        """Rohzeile -> JSON-Objekt, mit Rueckfall auf die andere Lesart.

        ``json.loads`` nimmt Bytes direkt (dekodiert UTF-8 in C) — das spart
        gegenueber dem Textmodus einen kompletten Dekodier-Durchlauf je
        Zeile. ``UnicodeDecodeError`` und ``JSONDecodeError`` sind beide
        ``ValueError``.
        """
        try:
            row = json.loads(unescape_copy_text(line) if primary is Escaping.COPY_TEXT else line)
        except ValueError as first:
            try:
                row = json.loads(
                    unescape_copy_text(line) if alternate is Escaping.COPY_TEXT else line
                )
            except ValueError:
                raise ParseError(
                    f"kein gueltiges JSON ({first}); Zeilenanfang: {_excerpt(line)}",
                    source=self.source,
                    line_no=line_no,
                ) from first
            self.stats.escaping_fallbacks += 1
        if type(row) is not dict:
            raise ParseError(
                f"JSON-Objekt erwartet, {_describe(row)} bekommen; Zeilenanfang: {_excerpt(line)}",
                source=self.source,
                line_no=line_no,
            )
        return row

    def _warn_unknown(self, unknown: set[str], line_no: int, warned: set[str]) -> None:
        """Unbekanntes Feld: einmal je Datei und Feldname warnen (§12.8)."""
        counts = self.stats.unknown_fields
        for name in sorted(unknown):
            counts[name] = counts.get(name, 0) + 1
            if name in warned:
                continue
            warned.add(name)
            _LOG.warning(
                "Unbekanntes Feld im Delta — Feldsatz pruefen (ARCHITECTURE §5.1)",
                extra={
                    "delta_file": self.source,
                    "stream": self.stream.value,
                    "field": name,
                    "line": line_no,
                },
            )


class _Missing:
    """Sentinel: der Schluessel war nicht in der Zeile (!= JSON ``null``)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - nur Diagnose
        return "<fehlt>"


_MISSING: Final = _Missing()


def _describe(value: Any) -> str:
    """Typname eines Werts fuer Fehlermeldungen."""
    return "null" if value is None else type(value).__name__


def _excerpt(line: bytes) -> str:
    text = line[:_EXCERPT_CHARS].decode("utf-8", errors="replace").rstrip("\n")
    return f"{text!r}{'…' if len(line) > _EXCERPT_CHARS else ''}"
