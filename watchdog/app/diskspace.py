"""Plattenplatz-Guard des Waechters — **jeder** Schreibpfad (E11, §8.8).

Invariante §8.8 lautet: *„Vor jedem Import-/Crawl-Segment: freier Platz ≥
``disk.min_free_gb``, sonst Abbruch + Notification."* Gebaut war davon
bisher die halbe Zusage: :mod:`acoustid_importer.diskguard` misst das
Arbeitsverzeichnis der Tagesdateien (``MMO_DUMP_DIR``) — und nur das. Auf
dem Referenz-Deployment liegt die Postgres im selben Array-Pool, dort
trifft das denselben Bestand; die Mounts aus ARCHITECTURE §3 sind aber
**mehrere Dateisysteme**, und ein freies ``/import`` sagt nichts ueber
``/data/db``.

Der Waechter ist die richtige Stelle fuer die Ausweitung: er startet die
Jobs (E10) und sieht als einziger Prozess alle Mounts. Gemessen wird
**vor** dem Start eines Jobs, nicht waehrenddessen — die laufende Pruefung
im Sekundentakt bleibt Sache des Jobs selbst.

**Ein Grenzwert, mehrere Dateisysteme.** ``disk.min_free_gb`` gilt fuer
jeden gepruefen Pfad einzeln; dedupliziert wird ueber die Geraetenummer
(``st_dev``), damit drei Mounts auf einem Dateisystem nicht dreimal
dieselbe Zahl melden.

**Einheit: GiB.** ``disk.min_free_gb`` wird wie im Importer als 1024³ Byte
gelesen — die strengere Lesart (LEARNINGS „Einheiten-Falle GB vs. GiB").
Die Konstante steht hier bewusst noch einmal und nicht als Import aus dem
Importer-Paket: der Waechter haengt nicht von ihm ab (er brauchte sonst
psycopg), und ein Test haelt beide Werte aneinander.

**Der Waechter bricht hier nichts ab.** Diese Funktionen messen und
antworten; was aus einer Unterschreitung folgt — Lauf abbrechen,
Benachrichtigung, Eintrag in der Historie —, entscheidet der Aufrufer
(:mod:`acoustid_watchdog.scheduler`). Ein nicht messbarer Pfad ist dabei
kein Fehler: ein Mount, den es (noch) nicht gibt, darf den Betrieb nicht
anhalten.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from shared.config import Config
from shared.env import EnvSettings

__all__ = [
    "BYTES_PER_GB",
    "DiskSpace",
    "measure",
    "shortfalls",
    "survey",
    "write_paths",
]

_LOG = logging.getLogger(__name__)

#: 1 „GB" im Sinne von ``disk.min_free_gb`` — bewusst binaer (GiB), wie in
#: :data:`acoustid_importer.diskguard.BYTES_PER_GB`.
BYTES_PER_GB: Final = 1 << 30

#: Messfunktion in der Signatur von :func:`shutil.disk_usage`; Tests setzen
#: eine Attrappe ein, damit die Randfaelle ohne volle Platte pruefbar sind.
UsageFn = Callable[[Path], object]


@dataclass(frozen=True, slots=True)
class DiskSpace:
    """Ein Messwert an einem Schreibpfad."""

    #: Der Pfad, wie er in der Konfiguration steht (nicht das gemessene
    #: Elternverzeichnis) — er ist die Auskunft, mit der der Betreiber
    #: etwas anfangen kann.
    path: Path
    total_bytes: int
    free_bytes: int
    #: Geforderte Reserve in Byte (``min_free_gb`` * :data:`BYTES_PER_GB`).
    min_free_bytes: int

    @property
    def ok(self) -> bool:
        return self.free_bytes >= self.min_free_bytes

    @property
    def free_gb(self) -> float:
        return self.free_bytes / BYTES_PER_GB

    @property
    def min_free_gb(self) -> float:
        return self.min_free_bytes / BYTES_PER_GB

    @property
    def shortfall_bytes(self) -> int:
        return max(0, self.min_free_bytes - self.free_bytes)

    def as_dict(self) -> dict[str, object]:
        """Maschinenlesbare Form fuer Ereignis-Log und Lauf-Report."""
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


def write_paths(settings: EnvSettings, config: Config) -> tuple[Path, ...]:
    """Alle Pfade, auf die ein Job schreibt (ARCHITECTURE §3).

    ==============  =========  =================================================
    ``/import``     Array      Dump-Downloads und Staging (``MMO_DUMP_DIR``)
    ``/data/db``    Array      PostgreSQL (``MMO_DB_DATA_ROOT``)
    ``/config``     Cache      Zustand, Lookup-Cache, Logs (``MMO_DATA_DIR``)
    ``backup.dir``  Array      Sicherungen — nur wenn eingerichtet
    ==============  =========  =================================================

    Der Suchindex-Mount (``/index``) fehlt bewusst: er waechst nur ueber
    den Index-Feed, und dessen Schreibvorgang gehoert dem residenten
    ``fpindex``-Prozess — der Waechter kennt sein Datenverzeichnis nicht
    (es ist ein Container-Wert ohne ``MMO_``-Praefix, .env.example).
    Ab M4 kommen ``/data/covers`` und ``/data/tadb`` dazu.
    """
    paths = [settings.dump_dir, settings.db_data_root, settings.data_dir]
    backup_dir = config.backup.directory
    if backup_dir is not None:
        paths.append(backup_dir)
    return tuple(paths)


def _nearest_existing(path: Path) -> Path:
    """Erstes vorhandenes Verzeichnis ab ``path`` aufwaerts.

    ``shutil.disk_usage`` braucht einen existierenden Pfad; ein
    Backup-Verzeichnis entsteht aber erst beim ersten Lauf.
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or ".")


def measure(
    path: Path, *, min_free_gb: int, usage: UsageFn = shutil.disk_usage
) -> DiskSpace | None:
    """Misst den freien Platz am Dateisystem von ``path``.

    Returns:
        Den Messwert, oder ``None``, wenn der Pfad nicht messbar ist
        (nicht gemountet, keine Rechte). **Kein Fehler:** ein Mount, den
        es nicht gibt, darf den Betrieb nicht anhalten — er faellt beim
        ersten Schreibversuch des Jobs ohnehin auf, und zwar mit einer
        genaueren Meldung als „nicht messbar".
    """
    target = _nearest_existing(path)
    try:
        stats = usage(target)
    except OSError as error:
        _LOG.warning(
            "Freier Platz nicht messbar", extra={"guard_path": str(target), "error": str(error)}
        )
        return None
    return DiskSpace(
        path=path,
        total_bytes=stats.total,  # type: ignore[attr-defined]
        free_bytes=stats.free,  # type: ignore[attr-defined]
        min_free_bytes=min_free_gb * BYTES_PER_GB,
    )


def _device_of(path: Path) -> object | None:
    """Geraetenummer des Dateisystems — der Schluessel der Deduplizierung."""
    try:
        return _nearest_existing(path).stat().st_dev
    except OSError:  # pragma: no cover - derselbe Fall wie in `measure`
        return None


def survey(
    paths: Iterable[Path],
    *,
    min_free_gb: int,
    usage: UsageFn = shutil.disk_usage,
) -> list[DiskSpace]:
    """Misst alle Pfade — je **Dateisystem** einmal.

    Args:
        paths: Die zu pruefenden Schreibpfade (:func:`write_paths`).
        min_free_gb: ``disk.min_free_gb``; ``0`` schaltet den Guard ab und
            liefert eine leere Liste.
        usage: Messfunktion; in Tests ersetzbar.

    Returns:
        Einen Messwert je Dateisystem, in der Reihenfolge der Eingabe.
        Liegen ``/import`` und ``/data/db`` auf demselben Pool (der
        Regelfall auf Unraid), steht dort genau ein Eintrag — dreimal
        dieselbe Zahl zu melden waere keine dreifache Auskunft.
    """
    if min_free_gb <= 0:
        return []
    seen: set[object] = set()
    results: list[DiskSpace] = []
    for path in paths:
        device = _device_of(path)
        if device is not None and device in seen:
            continue
        space = measure(path, min_free_gb=min_free_gb, usage=usage)
        if space is None:
            continue
        if device is not None:
            seen.add(device)
        results.append(space)
    return results


def shortfalls(spaces: Sequence[DiskSpace]) -> list[DiskSpace]:
    """Die Messwerte, die die Reserve unterschreiten — leer heisst „genug Platz"."""
    return [space for space in spaces if not space.ok]
