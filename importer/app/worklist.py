"""Arbeitsliste: welche Tagesdateien fehlen noch? (§5.1, §5.2 Regel 1/5)

Reine Rechenlogik — kein Netz, keine Datenbank, keine Uhr. „Heute" ist
immer ein Parameter (:func:`plan_from_state`, :func:`pending_files`), damit
die Randfaelle testbar bleiben; Phase 7 fuellt den Zustand aus der Tabelle
``import_state``, Phase 8 nutzt dieselbe Liste fuer den Bootstrap.

Zwei Fragen, zwei Antworten:

1. **Was ist noch zu tun?** Alle Tage ab dem ersten unerledigten bis zum
   letzten verfuegbaren, je Tag in der Reihenfolge aus Import-Regel 1
   (``track`` -> ``meta`` -> ``fingerprint`` + ``track_fingerprint`` ->
   ``track_mbid`` -> ``track_meta`` -> ``track_puid``), Tage strikt
   chronologisch ab 2011-08-19.
2. **Fehlt etwas in der Vergangenheit?** Ein Kalendertag *vor* dem
   Fortschrittsstand, der nie importiert wurde, ist eine **Luecke**
   (Import-Regel 5) und wird als :class:`Gap` gemeldet — nicht stillschweigend
   nachgeholt. Ein nachtraeglicher Import wuerde die strikte Chronologie
   brechen: die Zeilen des alten Tages wuerden neuere Werte ueberschreiben
   (der Strom ist ein Upsert-Strom ohne Operationsmarker). Was mit einer
   Luecke geschieht, entscheidet der Aufrufer bzw. der Betreiber.

**Leer ist nicht fehlend.** Eine 23-Byte-gz ohne Zeilen ist ein regulaerer
Tag; sie steht ganz normal in ``import_state`` und erzeugt keine Luecke.
Fehlend heisst: der Tag ist im Zustand nicht verzeichnet.

**Letzter verfuegbarer Tag** ist ``heute - 1``: die Datei eines Tages kann
erst existieren, wenn der Tag vorbei ist (:func:`newest_available_day`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from acoustid_importer.errors import GapError
from acoustid_importer.streams import FIRST_DAY, IMPORT_ORDER, DeltaFile, Stream, days_between

__all__ = [
    "Gap",
    "ImportPlan",
    "newest_available_day",
    "pending_files",
    "plan_from_state",
]


@dataclass(frozen=True, slots=True)
class Gap:
    """Eine fehlende Tagesdatei in der Vergangenheit (Import-Regel 5)."""

    file: DeltaFile
    #: Warum das eine Luecke ist (fuer Log und Admin-UI).
    reason: str = "Tag liegt vor dem Fortschrittsstand, wurde aber nie importiert"

    @property
    def day(self) -> date:
        return self.file.day

    @property
    def stream(self) -> Stream:
        return self.file.stream

    def __str__(self) -> str:
        return f"{self.file.name}: {self.reason}"


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """Ergebnis der Arbeitslisten-Rechnung."""

    #: Zu holende und zu importierende Dateien, in Ausfuehrungsreihenfolge.
    files: tuple[DeltaFile, ...]
    #: Gefundene Luecken; leer ist der Gutfall.
    gaps: tuple[Gap, ...] = ()

    @property
    def days(self) -> tuple[date, ...]:
        """Die betroffenen Tage, chronologisch und ohne Wiederholung."""
        seen: dict[date, None] = {}
        for item in self.files:
            seen.setdefault(item.day, None)
        return tuple(seen)

    def raise_on_gaps(self) -> None:
        """Bricht ab, wenn Luecken vorliegen (Import-Regel 5).

        Raises:
            GapError: Mindestens eine Tagesdatei fehlt in der Vergangenheit.
        """
        if not self.gaps:
            return
        names = ", ".join(gap.file.name for gap in self.gaps[:5])
        more = "" if len(self.gaps) <= 5 else f" (und {len(self.gaps) - 5} weitere)"
        raise GapError(
            f"{len(self.gaps)} fehlende Tagesdatei(en) in der Vergangenheit: {names}{more}",
            self.gaps,
        )

    def __len__(self) -> int:
        return len(self.files)


def newest_available_day(today: date) -> date:
    """Letzter Tag, dessen Dateien es geben kann: der Vortag von ``today``.

    Die Tagesdatei entsteht upstream erst nach Ablauf des Tages; „heute"
    ist deshalb nie Teil der Arbeitsliste.
    """
    return today - timedelta(days=1)


def pending_files(
    last_done: Mapping[Stream, date | None],
    *,
    today: date,
    first_day: date = FIRST_DAY,
) -> tuple[DeltaFile, ...]:
    """Offene Tagesdateien in Ausfuehrungsreihenfolge.

    Args:
        last_done: Je Strom der letzte **vollstaendig** importierte Tag;
            ``None`` oder ein fehlender Eintrag heissen „noch nie".
        today: Der laufende Tag (immer explizit, nie ``date.today()``).
        first_day: Beginn der Historie; nur fuer Tests abweichend.

    Returns:
        Tage chronologisch, innerhalb eines Tages in der Reihenfolge aus
        Import-Regel 1. Leer, wenn alles erledigt ist.

    Raises:
        ValueError: ``last_done`` enthaelt einen unbekannten Schluessel.
    """
    starts = _start_days(last_done, first_day=first_day)
    newest = newest_available_day(today)
    files: list[DeltaFile] = []
    for day in days_between(min(starts.values(), default=newest), newest):
        for stream in IMPORT_ORDER:
            if day >= starts[stream]:
                files.append(DeltaFile(day, stream))
    return tuple(files)


def plan_from_state(
    imported: Mapping[Stream, Iterable[date]],
    *,
    today: date,
    first_day: date = FIRST_DAY,
) -> ImportPlan:
    """Arbeitsliste **und** Lueckenpruefung aus dem vollen Fortschrittsstand.

    Das ist die Sicht der Tabelle ``import_state``: je Strom die Menge der
    bereits importierten Kalendertage (Import-Regel 5 prueft genau diese
    Menge gegen den Kalender).

    Args:
        imported: Je Strom die importierten Tage; leer oder fehlend heisst
            „noch nie". Reihenfolge und Duplikate sind egal.
        today: Der laufende Tag.
        first_day: Beginn der Historie; nur fuer Tests abweichend.

    Returns:
        :class:`ImportPlan` mit den offenen Dateien (chronologisch, je Tag
        nach Import-Regel 1) und den Luecken der Vergangenheit.

    Raises:
        ValueError: ``imported`` enthaelt einen unbekannten Schluessel.
    """
    _check_keys(imported)
    days_by_stream = {stream: frozenset(imported.get(stream, ())) for stream in IMPORT_ORDER}
    last_done = {stream: max(days, default=None) for stream, days in days_by_stream.items()}
    gaps = [
        Gap(DeltaFile(day, stream))
        for stream, days in days_by_stream.items()
        for day in days_between(first_day, last_done[stream] or first_day)
        if last_done[stream] is not None and day not in days
    ]
    gaps.sort(key=lambda gap: gap.file.sort_key)
    return ImportPlan(
        pending_files(last_done, today=today, first_day=first_day),
        tuple(gaps),
    )


def _start_days(last_done: Mapping[Stream, date | None], *, first_day: date) -> dict[Stream, date]:
    """Je Strom der erste noch zu importierende Tag."""
    _check_keys(last_done)
    starts: dict[Stream, date] = {}
    for stream in IMPORT_ORDER:
        done = last_done.get(stream)
        starts[stream] = first_day if done is None else max(done + timedelta(days=1), first_day)
    return starts


def _check_keys(mapping: Mapping[Stream, object]) -> None:
    unknown = set(mapping) - set(IMPORT_ORDER)
    if unknown:
        known = ", ".join(stream.value for stream in IMPORT_ORDER)
        raise ValueError(
            f"Unbekannte Stroeme im Zustand: {sorted(map(str, unknown))}; bekannt sind: {known}"
        )
