"""Die sieben Delta-Stroeme und ihre Dateinamen (ARCHITECTURE §5.1, §5.2).

Hier steht alles, was Downloader, Parser und Arbeitsliste gemeinsam ueber
die Datenquelle wissen muessen — und sonst nichts:

* :class:`Stream` — die sieben Stroeme. **Die Reihenfolge der Enum-Werte
  ist die Importreihenfolge aus Import-Regel 1** (``track`` -> ``meta`` ->
  ``fingerprint`` + ``track_fingerprint`` -> ``track_mbid`` ->
  ``track_meta`` -> ``track_puid``); :data:`IMPORT_ORDER` macht sie
  explizit, :attr:`Stream.order` sortierbar.
* :class:`DeltaFile` — ein Paar (Tag, Strom) und damit genau eine
  Tagesdatei: Name, URL, Monatsverzeichnis.
* Die festen Werte der Quelle: :data:`BASE_URL`, :data:`FIRST_DAY`
  (2011-08-19, Beginn der Historie) und :data:`EMPTY_GZ_SIZE` (23 Byte,
  die legitime leere Datei).

Pfadschema (§5.1)::

    https://data.acoustid.org/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>-<strom>-update.jsonl.gz

Zeitangaben sind immer reine Kalendertage (:class:`datetime.date`) — die
Dateinamen tragen kein Uhrzeit- und kein Zeitzonenwissen, und die Logik
bekommt „heute" grundsaetzlich als Parameter uebergeben (nie ein
``date.today()`` tief im Code, sonst sind die Randfaelle nicht testbar).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Final, Self

__all__ = [
    "BASE_URL",
    "EMPTY_GZ_SIZE",
    "FIRST_DAY",
    "IMPORT_ORDER",
    "DeltaFile",
    "Stream",
    "days_between",
    "month_index_url",
]

#: Wurzel der oeffentlichen Delta-Exporte (ARCHITECTURE §5.1).
BASE_URL: Final = "https://data.acoustid.org"

#: Erster Tag der Historie; davor gibt es keine Dateien (§5.1).
FIRST_DAY: Final = date(2011, 8, 19)

#: Groesse einer leeren gzip-Datei. Kommt regulaer vor (Daten-Flaute,
#: Legacy-Stroeme) und ist **kein** Fehler — anders als eine fehlende Datei.
EMPTY_GZ_SIZE: Final = 23

#: Dateiname einer Tagesdatei, z. B. ``2026-07-22-track_mbid-update.jsonl.gz``.
_FILE_NAME_RE: Final = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})-(?P<stream>[a-z_]+)-update\.jsonl\.gz$"
)


class Stream(StrEnum):
    """Ein Delta-Strom; der Wert ist der Namensteil in der URL.

    **Die Definitionsreihenfolge ist die Importreihenfolge** (Import-Regel 1
    in ARCHITECTURE §5.2): erst ``track``, dann ``meta``, dann die beiden
    Projektionen der Fingerprint-Tabelle, dann die Zuordnungstabellen.
    Wer sie aendert, aendert die Semantik des Imports — nicht sortieren.
    """

    TRACK = "track"
    META = "meta"
    FINGERPRINT = "fingerprint"
    TRACK_FINGERPRINT = "track_fingerprint"
    TRACK_MBID = "track_mbid"
    TRACK_META = "track_meta"
    TRACK_PUID = "track_puid"

    @property
    def order(self) -> int:
        """Platz in der Importreihenfolge (0 = zuerst)."""
        return _ORDER[self]

    @property
    def table(self) -> str:
        """Zieltabelle in der Postgres (ARCHITECTURE §5.2).

        ``fingerprint`` und ``track_fingerprint`` sind zwei Projektionen
        **derselben** Upstream-Tabelle und schreiben deshalb beide nach
        ``fingerprint`` — mit disjunkten Spaltenmengen (Import-Regel 2).
        """
        return "fingerprint" if self is Stream.TRACK_FINGERPRINT else self.value


#: Alle Stroeme in Importreihenfolge (Import-Regel 1).
IMPORT_ORDER: Final[tuple[Stream, ...]] = tuple(Stream)

_ORDER: Final[dict[Stream, int]] = {stream: i for i, stream in enumerate(IMPORT_ORDER)}


@dataclass(frozen=True, slots=True)
class DeltaFile:
    """Genau eine Tagesdatei: ein Kalendertag in einem Strom.

    Die Klasse ist der gemeinsame Bezeichner von Arbeitsliste, Downloader
    und Parser — sie kennt den Dateinamen und die URL, aber weder Zielpfad
    noch Inhalt.
    """

    day: date
    stream: Stream

    @property
    def name(self) -> str:
        """Dateiname laut Pfadschema (§5.1)."""
        return f"{self.day.isoformat()}-{self.stream.value}-update.jsonl.gz"

    @property
    def month(self) -> str:
        """Monatsverzeichnis, z. B. ``2026-07``."""
        return f"{self.day.year:04d}-{self.day.month:02d}"

    @property
    def sort_key(self) -> tuple[date, int]:
        """Sortierschluessel: Tage chronologisch, im Tag nach Import-Regel 1."""
        return (self.day, self.stream.order)

    def url(self, base_url: str = BASE_URL) -> str:
        """Vollstaendige Download-URL."""
        return f"{base_url.rstrip('/')}/{self.day.year:04d}/{self.month}/{self.name}"

    @classmethod
    def from_name(cls, name: str) -> Self:
        """Zerlegt einen Dateinamen wieder in Tag und Strom.

        Raises:
            ValueError: Der Name passt nicht zum Schema oder nennt einen
                unbekannten Strom.
        """
        match = _FILE_NAME_RE.match(name)
        if match is None:
            raise ValueError(
                f"Kein Delta-Dateiname: {name!r} (erwartet <YYYY-MM-DD>-<strom>-update.jsonl.gz)"
            )
        try:
            stream = Stream(match["stream"])
        except ValueError:
            known = ", ".join(s.value for s in IMPORT_ORDER)
            raise ValueError(
                f"Unbekannter Strom {match['stream']!r} in {name!r}; bekannt sind: {known}"
            ) from None
        return cls(date.fromisoformat(match["day"]), stream)

    def __str__(self) -> str:
        return self.name


def month_index_url(day: date, base_url: str = BASE_URL) -> str:
    """URL des ``index.json`` des Monatsverzeichnisses von ``day``.

    Das Verzeichnis-Listing ist die einzige maschinenlesbare Quelle fuer die
    **exakten Bytegroessen** (die HTML-Listings labeln Binaerpraefixe falsch
    als SI, LEARNINGS „Einheiten-Falle") und zugleich die Antwort auf die
    Frage, ob es die Datei eines Tages ueberhaupt gibt.
    """
    return f"{base_url.rstrip('/')}/{day.year:04d}/{day.year:04d}-{day.month:02d}/index.json"


def days_between(first: date, last: date) -> list[date]:
    """Alle Kalendertage von ``first`` bis ``last`` (beide inklusive).

    Leer, wenn ``last`` vor ``first`` liegt.
    """
    if last < first:
        return []
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
