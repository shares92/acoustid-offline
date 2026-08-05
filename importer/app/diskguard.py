"""Plattenplatz-Guard des Importers (Invariante §8.8).

„Vor jedem Import: freier Platz >= ``update.min_free_gb``, sonst Abbruch mit
klarem Ergebnis." Der Bootstrap laedt 414 GB gz und schreibt daraus ein
Vielfaches in die Postgres — ohne Wachhund laeuft irgendwann die Platte
voll, und zwar mitten in einer Transaktion.

**Was geprueft wird.** Der Guard sieht nur das Dateisystem *seines eigenen
Containers*: das Arbeitsverzeichnis der Tagesdateien (``MMO_DUMP_DIR``).
Das Datenverzeichnis der Postgres liegt in einem anderen Container und ist
von hier aus nicht messbar — liegen beide auf demselben Unraid-Share (der
Regelfall), misst der Guard trotzdem denselben Pool. Weitere Pfade kann der
Aufrufer dazugeben (:func:`require_free_space` je Pfad).

**Wann geprueft wird.** Einmal vor dem Lauf und danach in Abstaenden
(:class:`DiskGuard`): nach je :data:`DEFAULT_EVERY_FILES` Dateien oder je
:data:`DEFAULT_EVERY_BYTES` verarbeiteten gz-Bytes, je nachdem was zuerst
eintritt. Dazwischen zu pruefen kostet nur Syscalls, aber eine einzelne
Fingerprint-Tagesdatei kann mehrere GB gross sein — deshalb zusaetzlich die
Byte-Schranke.

**Einheit.** ``update.min_free_gb`` wird hier als **GiB** gelesen
(:data:`BYTES_PER_GB` = 1024^3), also die strengere Lesart: wer 50 fordert,
bekommt 53,7 SI-GB Reserve, nie weniger (LEARNINGS „Einheiten-Falle GB vs.
GiB").

Der Abbruch ist bewusst ein *kontrollierter*: :class:`DiskSpaceError` wird
vom Job-Rumpf zu einem eigenen Exit-Code und einem Ergebnis-Report gemacht,
und weil jede Tagesdatei ihre eigene Transaktion ist, bleibt ``import_state``
dabei genau auf der letzten vollstaendigen Datei stehen (§8.4).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from acoustid_importer.errors import DiskSpaceError

__all__ = [
    "BYTES_PER_GB",
    "DEFAULT_EVERY_BYTES",
    "DEFAULT_EVERY_FILES",
    "DiskGuard",
    "DiskSpace",
    "evaluate",
    "measure",
    "require_free_space",
]

_LOG = logging.getLogger(__name__)

#: 1 „GB" im Sinne von ``update.min_free_gb`` — bewusst binaer (GiB).
BYTES_PER_GB: Final = 1 << 30

#: Wiederholungspruefung nach so vielen Dateien.
DEFAULT_EVERY_FILES: Final = 25

#: Wiederholungspruefung nach so vielen verarbeiteten gz-Bytes (2 GiB).
DEFAULT_EVERY_BYTES: Final = 2 << 30


class Usage(Protocol):
    """Rueckgabe von :func:`shutil.disk_usage` (nur die Felder, die zaehlen)."""

    total: int
    free: int


#: Messfunktion in der Signatur von :func:`shutil.disk_usage` — in Tests
#: durch eine Attrappe ersetzbar, damit die Randfaelle ohne volle Platte
#: pruefbar sind.
UsageFn = Callable[[Path], Usage]


@dataclass(frozen=True, slots=True)
class DiskSpace:
    """Ein Messwert des freien Platzes an einem Pfad."""

    #: Der gepruefte Pfad (das naechste existierende Verzeichnis, s. u.).
    path: Path
    total_bytes: int
    free_bytes: int
    #: Geforderte Reserve in Byte (``min_free_gb`` * :data:`BYTES_PER_GB`).
    min_free_bytes: int

    @property
    def ok(self) -> bool:
        """Reicht der freie Platz?"""
        return self.free_bytes >= self.min_free_bytes

    @property
    def shortfall_bytes(self) -> int:
        """Wie viel fehlt zur geforderten Reserve (0, wenn sie erfuellt ist)."""
        return max(0, self.min_free_bytes - self.free_bytes)

    @property
    def free_gb(self) -> float:
        """Freier Platz in GiB — fuer Meldungen."""
        return self.free_bytes / BYTES_PER_GB

    @property
    def min_free_gb(self) -> float:
        """Geforderte Reserve in GiB — fuer Meldungen."""
        return self.min_free_bytes / BYTES_PER_GB

    def as_dict(self) -> dict[str, object]:
        """Maschinenlesbare Form fuer den Ergebnis-Report."""
        return {
            "path": str(self.path),
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "min_free_bytes": self.min_free_bytes,
            "ok": self.ok,
        }

    def __str__(self) -> str:
        return (
            f"{self.path}: {self.free_gb:.1f} GiB frei, gefordert sind {self.min_free_gb:.1f} GiB"
        )


def evaluate(path: Path, *, total_bytes: int, free_bytes: int, min_free_gb: int) -> DiskSpace:
    """Baut den Messwert aus rohen Zahlen — pure, ohne Dateisystem.

    Raises:
        ValueError: ``min_free_gb`` ist negativ.
    """
    if min_free_gb < 0:
        raise ValueError(f"min_free_gb darf nicht negativ sein, war {min_free_gb}")
    return DiskSpace(
        path=path,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        min_free_bytes=min_free_gb * BYTES_PER_GB,
    )


def measure(path: Path, *, min_free_gb: int, usage: UsageFn = shutil.disk_usage) -> DiskSpace:
    """Misst den freien Platz am Dateisystem von ``path``.

    Existiert der Pfad noch nicht (das Arbeitsverzeichnis wird erst beim
    ersten Download angelegt), wird das naechste vorhandene Elternverzeichnis
    gemessen — es liegt auf demselben Dateisystem.

    Raises:
        DiskSpaceError: Der Pfad ist nicht messbar (kein Zugriff o. Ae.).
    """
    target = _nearest_existing(path)
    try:
        stats = usage(target)
    except OSError as error:
        raise DiskSpaceError(
            f"Freier Platz unter {target} ist nicht messbar: {error}",
            path=target,
            free_bytes=None,
            min_free_bytes=min_free_gb * BYTES_PER_GB,
        ) from error
    return evaluate(
        target,
        total_bytes=stats.total,
        free_bytes=stats.free,
        min_free_gb=min_free_gb,
    )


def require_free_space(
    path: Path,
    *,
    min_free_gb: int,
    usage: UsageFn = shutil.disk_usage,
    what: str = "Import",
) -> DiskSpace:
    """Misst und bricht ab, wenn die Reserve unterschritten ist (§8.8).

    Args:
        path: Zu pruefendes Verzeichnis.
        min_free_gb: ``update.min_free_gb``; 0 schaltet den Guard ab.
        usage: Messfunktion; in Tests ersetzbar.
        what: Was gerade ansteht — erscheint in der Fehlermeldung.

    Returns:
        Den Messwert, wenn genug Platz da ist.

    Raises:
        DiskSpaceError: Der freie Platz liegt unter der Reserve.
    """
    space = measure(path, min_free_gb=min_free_gb, usage=usage)
    if space.ok:
        _LOG.debug(
            "Plattenplatz geprueft",
            extra={
                "guard_path": str(space.path),
                "free_bytes": space.free_bytes,
                "min_free_bytes": space.min_free_bytes,
            },
        )
        return space
    raise DiskSpaceError(
        f"{what} abgebrochen: unter {space.path} sind nur {space.free_gb:.1f} GiB frei, "
        f"gefordert sind {space.min_free_gb:.1f} GiB (update.min_free_gb). "
        "Der Lauf ist resumierbar — nach dem Aufraeumen setzt der naechste Lauf fort.",
        path=space.path,
        free_bytes=space.free_bytes,
        min_free_bytes=space.min_free_bytes,
    )


class DiskGuard:
    """Wiederholte Platzpruefung waehrend eines langen Laufs.

    Der Guard ist bewusst zustandsbehaftet (er zaehlt Dateien und Bytes seit
    der letzten Pruefung), die Entscheidung „jetzt pruefen?" selbst ist die
    pure Funktion :meth:`due`.

    Beispiel::

        guard = DiskGuard(dump_dir, min_free_gb=config.update.min_free_gb)
        guard.check()  # vor dem Lauf (Invariante §8.8)
        for download in prefetcher:
            ...
            guard.after_file(download.size)  # prueft, wenn faellig
    """

    def __init__(
        self,
        path: Path,
        *,
        min_free_gb: int,
        every_files: int = DEFAULT_EVERY_FILES,
        every_bytes: int = DEFAULT_EVERY_BYTES,
        usage: UsageFn | None = None,
    ) -> None:
        """
        Args:
            path: Zu ueberwachendes Verzeichnis (``MMO_DUMP_DIR``).
            min_free_gb: Geforderte Reserve; 0 schaltet den Guard ab.
            every_files: Erneut pruefen nach so vielen Dateien.
            every_bytes: Erneut pruefen nach so vielen gz-Bytes.
            usage: Messfunktion; ``None`` heisst :func:`shutil.disk_usage`.
                In Tests ersetzbar.

        Raises:
            ValueError: ``min_free_gb`` negativ oder ein Intervall < 1.
        """
        if min_free_gb < 0:
            raise ValueError(f"min_free_gb darf nicht negativ sein, war {min_free_gb}")
        if every_files < 1 or every_bytes < 1:
            raise ValueError("every_files und every_bytes muessen mindestens 1 sein")
        self.path = path
        self.min_free_gb = min_free_gb
        self.every_files = every_files
        self.every_bytes = every_bytes
        self._usage = usage or shutil.disk_usage
        self._files_since = 0
        self._bytes_since = 0
        self._checks = 0
        self._last: DiskSpace | None = None

    @property
    def enabled(self) -> bool:
        """``min_free_gb = 0`` heisst: keine Reserve gefordert (§6)."""
        return self.min_free_gb > 0

    @property
    def last(self) -> DiskSpace | None:
        """Letzter Messwert; ``None``, solange nie gemessen wurde."""
        return self._last

    @property
    def checks(self) -> int:
        """Wie oft tatsaechlich gemessen wurde (fuer den Report)."""
        return self._checks

    def due(self) -> bool:
        """Ist eine Wiederholungspruefung faellig? (pure)"""
        return self._files_since >= self.every_files or self._bytes_since >= self.every_bytes

    def measure(self) -> DiskSpace | None:
        """Misst, ohne abzubrechen — fuer die Abschlusszahl im Report.

        Returns:
            Den Messwert; ``None``, wenn der Guard abgeschaltet oder der Pfad
            nicht messbar ist.
        """
        try:
            return measure(self.path, min_free_gb=self.min_free_gb, usage=self._usage)
        except DiskSpaceError:
            return None

    def check(self, *, what: str = "Import") -> DiskSpace | None:
        """Misst sofort und bricht bei zu wenig Platz ab.

        Returns:
            Den Messwert; ``None``, wenn der Guard abgeschaltet ist.

        Raises:
            DiskSpaceError: Der freie Platz liegt unter der Reserve.
        """
        self._files_since = 0
        self._bytes_since = 0
        if not self.enabled:
            return None
        self._checks += 1
        space = require_free_space(
            self.path, min_free_gb=self.min_free_gb, usage=self._usage, what=what
        )
        self._last = space
        return space

    def after_file(self, size_bytes: int = 0, *, what: str = "Import") -> DiskSpace | None:
        """Zaehlt eine erledigte Datei und prueft, wenn faellig.

        Returns:
            Den Messwert, wenn gemessen wurde, sonst ``None``.

        Raises:
            DiskSpaceError: Der freie Platz liegt unter der Reserve.
        """
        self._files_since += 1
        self._bytes_since += max(0, size_bytes)
        if not self.due():
            return None
        return self.check(what=what)


def _nearest_existing(path: Path) -> Path:
    """Erstes vorhandenes Verzeichnis ab ``path`` aufwaerts.

    ``shutil.disk_usage`` braucht einen existierenden Pfad; das
    Arbeitsverzeichnis entsteht aber erst beim ersten Download.
    """
    current = path
    for candidate in (current, *current.parents):
        if candidate.exists():
            return candidate
    return Path(current.anchor or ".")
