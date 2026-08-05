#!/usr/bin/env python3
"""Laedt die AcoustID-Dump-Fixtures fuer die Testsuite nach.

Die Dateien liegen bewusst nicht im Repo (Lizenz CC BY-SA 3.0, Groesse) —
siehe ``acoustid-dumps/README.md``. Dieses Skript beschafft genau die zehn
Referenzdateien reproduzierbar von data.acoustid.org.

Nur Python-Stdlib, keine Abhaengigkeiten:

    python3 tests/fixtures/fetch_fixtures.py [--force] [--dest DIR] [--list]

Bereits vorhandene Dateien werden uebersprungen; jede geladene Datei wird auf
Groesse und gzip-Integritaet geprueft. Exit-Code 0 = alles vorhanden/geladen,
1 = mindestens eine Datei fehlt oder ist unbrauchbar.
"""

from __future__ import annotations

import argparse
import gzip
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://data.acoustid.org/"

DEFAULT_DEST = Path(__file__).resolve().parent / "acoustid-dumps"

USER_AGENT = "musicmeta-offline-fixture-fetch/1.0 (+https://data.acoustid.org)"

TIMEOUT_S = 120

# Kleinste gueltige gzip-Datei: leerer Inhalt (kommt upstream regulaer vor).
EMPTY_GZ_SIZE = 23

# Dateiname -> erwartete Groesse in Byte (Referenzstand 2026-07-25; die
# Tagesdateien sind upstream unveraenderlich). Die URL ergibt sich aus dem
# Namen: <BASE_URL>/<YYYY>/<YYYY-MM>/<name>.
FIXTURES: dict[str, int] = {
    # Alt-Epoche: bis 2024-12-04 liegt ueber dem JSON zusaetzlich das
    # Text-Escaping von COPY (Phase 6). 85 der 11.677 Zeilen sind ohne
    # Unescape kein gueltiges JSON — der einzige Fixture-Beleg dafuer.
    "2011-08-19-meta-update.jsonl.gz": 423_795,
    "2026-07-22-fingerprint-update.jsonl.gz": 8_925_088,
    "2026-07-22-meta-update.jsonl.gz": 169_027,
    "2026-07-22-track-update.jsonl.gz": 53_159,
    "2026-07-22-track_fingerprint-update.jsonl.gz": 35_820,
    "2026-07-22-track_mbid-update.jsonl.gz": 37_387,
    "2026-07-22-track_meta-update.jsonl.gz": 122_663,
    "2026-07-22-track_puid-update.jsonl.gz": EMPTY_GZ_SIZE,
    "2026-07-23-fingerprint-update.jsonl.gz": EMPTY_GZ_SIZE,
    "2026-07-23-track_mbid-update.jsonl.gz": 316,
}


def check_gzip(path: Path) -> str | None:
    """Prueft Groesse und gzip-Struktur. Gibt eine Fehlermeldung zurueck oder None."""
    size = path.stat().st_size
    if size < EMPTY_GZ_SIZE:
        return f"nur {size} Byte — kleiner als eine leere gzip-Datei"
    with path.open("rb") as handle:
        if handle.read(2) != b"\x1f\x8b":
            return "kein gzip-Header (1f 8b)"
    try:
        with gzip.open(path, "rb") as gz:
            while gz.read(1 << 20):
                pass
    except (OSError, EOFError) as exc:
        return f"gzip-Stream defekt: {exc}"
    return None


def download(url: str, target: Path) -> None:
    """Laedt ``url`` atomar nach ``target`` (erst .part, dann umbenennen)."""
    part = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        declared = response.headers.get("Content-Length")
        with part.open("wb") as handle:
            while chunk := response.read(1 << 16):
                handle.write(chunk)
    written = part.stat().st_size
    if declared is not None and written != int(declared):
        part.unlink(missing_ok=True)
        raise OSError(f"unvollstaendig: {written} statt {declared} Byte")
    part.replace(target)


def fixture_url(name: str) -> str:
    """URL einer Fixture: ``<BASE_URL>/<YYYY>/<YYYY-MM>/<name>``."""
    year, month = name[:4], name[:7]
    return f"{BASE_URL.rstrip('/')}/{year}/{month}/{name}"


def fetch_one(name: str, expected: int, dest: Path, force: bool) -> tuple[bool, str]:
    """Beschafft eine Fixture. Rueckgabe: (Erfolg, Statuszeile)."""
    target = dest / name
    if target.exists() and not force:
        problem = check_gzip(target)
        if problem:
            return False, f"vorhanden, aber unbrauchbar ({problem}) — mit --force neu laden"
        return True, f"vorhanden ({target.stat().st_size:,} Byte)"

    url = fixture_url(name)
    try:
        download(url, target)
    except (urllib.error.URLError, OSError) as exc:
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            hint = (
                " — der Interpreter kennt keine CA-Zertifikate; auf macOS einmalig"
                ' "Install Certificates.command" der Python-Installation ausfuehren'
                " oder einen Interpreter mit System-CA-Bundle verwenden"
            )
        return False, f"Download fehlgeschlagen: {exc}{hint}"

    problem = check_gzip(target)
    if problem:
        return False, f"geladen, aber unbrauchbar ({problem})"

    size = target.stat().st_size
    if size != expected:
        return True, f"geladen ({size:,} Byte) — WARNUNG: erwartet waren {expected:,} Byte"
    return True, f"geladen ({size:,} Byte)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Laedt die AcoustID-Dump-Fixtures von data.acoustid.org nach.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"Zielverzeichnis (Default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="vorhandene Dateien erneut laden",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="nur auflisten, was geladen wuerde",
    )
    args = parser.parse_args(argv)

    if args.list:
        for name, expected in FIXTURES.items():
            print(f"{fixture_url(name)}  ({expected:,} Byte)")
        return 0

    dest: Path = args.dest
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Ziel: {dest}")
    print(f"Quelle: {BASE_URL}\n")

    failures = 0
    for name, expected in FIXTURES.items():
        ok, status = fetch_one(name, expected, dest, args.force)
        marker = "ok  " if ok else "FEHL"
        print(f"[{marker}] {name}: {status}")
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"{failures} von {len(FIXTURES)} Fixtures fehlen oder sind unbrauchbar.")
        return 1
    print(f"Alle {len(FIXTURES)} Fixtures liegen bereit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
