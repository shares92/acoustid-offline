"""Download-Prefetch: laden, waehrend importiert wird (DECISIONS 2026-07-25).

Der Bootstrap holt 38.178 Dateien mit zusammen 414 GB und schreibt sie in
eine Postgres auf Spindeln. Beides nacheinander zu tun, hiesse: waehrend der
Import laeuft, liegt die Leitung brach, und waehrend geladen wird, langweilt
sich die Datenbank. Der :class:`Prefetcher` entkoppelt die beiden Seiten —
ein Hintergrund-Thread laedt die naechsten Dateien, waehrend der Aufrufer
die aktuelle importiert::

    with Prefetcher(downloader, plan.files, ahead=2) as ahead_of_us:
        for download in ahead_of_us:
            import_file(conn, download.path, file=download.file)

**Genau ein Ladethread.** Die Reihenfolge der Dateien ist keine Kosmetik,
sondern Import-Regel 1 (Tage chronologisch, im Tag nach Strom) — ein zweiter
Thread wuerde sie brechen oder muesste kuenstlich resynchronisiert werden.
Der Engpass ist ohnehin die Datenbank; ein Vorlauf von zwei Dateien reicht,
damit die Leitung nie stillsteht.

**Vorlauftiefe kostet Platz.** Es liegen bis zu ``ahead`` fertige Dateien
zusaetzlich zur gerade importierten auf der Platte — bei
Fingerprint-Tagesdateien von mehreren GB ist das der Grund, warum der
Plattenplatz-Guard (:mod:`acoustid_importer.diskguard`) waehrend des Laufs
weiter misst und nicht nur einmal am Anfang.

**Fehler kommen im Aufrufer an.** Ein Ladefehler wird nicht im Thread
geloggt und verschluckt, sondern beim naechsten ``next()`` unveraendert
weitergeworfen — der Job-Rumpf sieht denselben Fehler, den ein direkter
``downloader.fetch()`` geworfen haette, nur eben ein bis zwei Dateien
frueher als noetig.

**Abbruch.** :meth:`Prefetcher.close` (auch ueber ``with``) stoppt den
Thread; eine bereits laufende Uebertragung wird nicht abgeschnitten, sondern
zu Ende gefuehrt — sie ist Sekunden entfernt und ihr ``.part`` waere sonst
Muell. Danach ist der Prefetcher verbraucht.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Self

from acoustid_importer.download import DeltaDownloader, Download
from acoustid_importer.streams import DeltaFile

__all__ = [
    "DEFAULT_AHEAD",
    "PrefetchStats",
    "Prefetcher",
]

_LOG = logging.getLogger(__name__)

#: Wie viele fertige Dateien hoechstens auf ihren Import warten.
DEFAULT_AHEAD: Final = 2

#: Taktung der Warteschlangen-Zugriffe des Threads; klein genug, dass ein
#: ``close()`` sofort wirkt, gross genug fuer eine ruhige CPU.
_POLL_S: Final = 0.05


@dataclass(frozen=True, slots=True)
class PrefetchStats:
    """Was der Ladethread geschafft hat (fuer den Ergebnis-Report)."""

    #: Tatsaechlich uebergebene Dateien.
    files: int = 0
    #: Davon frisch geladen (der Rest lag schon vollstaendig da).
    downloaded: int = 0
    #: Davon aus einem ``.part`` fortgesetzt.
    resumed: int = 0
    #: Summe der gz-Bytes aller uebergebenen Dateien.
    bytes: int = 0

    def plus(self, download: Download) -> PrefetchStats:
        """Ergebnis dazuzaehlen (pure)."""
        return PrefetchStats(
            files=self.files + 1,
            downloaded=self.downloaded + (0 if download.reused else 1),
            resumed=self.resumed + (1 if download.resumed else 0),
            bytes=self.bytes + download.size,
        )


@dataclass(frozen=True, slots=True)
class _Item:
    """Ein Eintrag der Warteschlange: Ergebnis **oder** Fehler."""

    download: Download | None = None
    error: BaseException | None = None


class Prefetcher:
    """Laedt Tagesdateien im Voraus und gibt sie in der Eingabereihenfolge aus."""

    def __init__(
        self,
        downloader: DeltaDownloader,
        files: Iterable[DeltaFile],
        *,
        ahead: int = DEFAULT_AHEAD,
        verify_gzip: bool = True,
    ) -> None:
        """
        Args:
            downloader: Der :class:`~acoustid_importer.download.DeltaDownloader`;
                er wird **nicht** geschlossen, das bleibt beim Aufrufer.
            files: Tagesdateien in Ausfuehrungsreihenfolge (Arbeitsliste).
            ahead: Wie viele fertige Dateien hoechstens vorgehalten werden.
                ``1`` heisst: eine laedt, waehrend eine importiert wird.
            verify_gzip: Wird an :meth:`DeltaDownloader.fetch` durchgereicht;
                fuer den Bootstrap abschaltbar (der Parser liest die Datei
                ohnehin sofort danach vollstaendig).

        Raises:
            ValueError: ``ahead`` ist kleiner als 1.
        """
        if ahead < 1:
            raise ValueError(f"ahead muss mindestens 1 sein, war {ahead}")
        self._downloader = downloader
        self._files = list(files)
        self._verify_gzip = verify_gzip
        self._queue: queue.Queue[_Item | None] = queue.Queue(maxsize=ahead)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = PrefetchStats()
        self._closed = False

    # --- Lebenszyklus ------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(files={len(self._files)}, ahead={self._queue.maxsize})"

    @property
    def stats(self) -> PrefetchStats:
        """Zwischenstand; nach dem Durchlauf der Endstand."""
        return self._stats

    @property
    def pending(self) -> int:
        """Wie viele fertige Dateien gerade auf ihren Import warten."""
        return self._queue.qsize()

    def close(self) -> None:
        """Stoppt den Ladethread und wartet auf sein Ende (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        # Die Warteschlange leeren, damit ein blockiertes `put` durchkommt.
        while thread.is_alive():
            try:
                self._queue.get(timeout=_POLL_S)
            except queue.Empty:
                continue
        thread.join()
        self._thread = None

    # --- Iteration ---------------------------------------------------------

    def __iter__(self) -> Iterator[Download]:
        """Gibt die Downloads in der Eingabereihenfolge aus.

        Raises:
            RuntimeError: Der Prefetcher wurde bereits durchlaufen.
            acoustid_importer.errors.DownloadError: Fehler des Ladethreads —
                unveraendert weitergereicht (auch ``DeltaNotFoundError`` und
                ``SizeMismatchError``).
        """
        if self._closed or self._thread is not None:
            raise RuntimeError("Ein Prefetcher laesst sich nur einmal durchlaufen")
        self._thread = threading.Thread(target=self._work, name="delta-prefetch", daemon=True)
        self._thread.start()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if item.error is not None:
                    raise item.error
                if item.download is None:  # pragma: no cover - Invariante von _Item
                    continue
                self._stats = self._stats.plus(item.download)
                yield item.download
        finally:
            self.close()

    # --- Innenleben --------------------------------------------------------

    def _work(self) -> None:
        """Laedt die Dateien der Reihe nach in die Warteschlange."""
        try:
            for file in self._files:
                if self._stop.is_set():
                    return
                try:
                    download = self._downloader.fetch(file, verify_gzip=self._verify_gzip)
                except Exception as error:
                    # Bewusst breit: was auch immer beim Laden schiefgeht,
                    # gehoert in den Aufrufer-Thread und nicht in ein
                    # verschlucktes Thread-Traceback.
                    self._offer(_Item(error=error))
                    return
                if not self._offer(_Item(download=download)):
                    return
        finally:
            # Endezeichen; verschluckt wird es nur, wenn ohnehin gestoppt wird.
            self._offer(None)

    def _offer(self, item: _Item | None) -> bool:
        """Legt einen Eintrag ab; ``False``, wenn dabei gestoppt wurde."""
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=_POLL_S)
            except queue.Full:
                continue
            return True
        _LOG.debug("Prefetch gestoppt — Eintrag verworfen")
        return False
