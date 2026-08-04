"""Paketuebergreifende Tests des Repo-Grundgeruests (ARCHITECTURE.md §10).

Halten die vereinbarte Struktur fest, damit spaetere Phasen sie nicht
unbemerkt verlassen.

Seit M1b haelt diese Datei zusaetzlich zwei Zusagen des
Ein-Container-Umbaus fest, die sonst niemand pruefen wuerde:

* **Ein Image, eine Compose-Datei** — die drei alten Dockerfiles und die
  zweite Compose-Datei sind weg und duerfen nicht zurueckkommen.
* **Nichts schreibt nach ``/data``** (Risiko R13): Logs, Sockets und das
  Datenverzeichnis des Waechters gehoeren auf den Cache-Mount. Ein Pfad
  unter ``/data`` haette die Invariante §10.2 lautlos aufgehoben — das
  Array wuerde nie schlafen, und keine Testsuite haette es gemerkt.
"""

import importlib
import logging
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Verzeichnisse aus ARCHITECTURE §10, die schon in Phase 2 stehen muessen.
REQUIRED_DIRS = [
    "shared/shared",
    "api/app",
    "importer/app",
    "watchdog/app",
    "watchdog/app/templates",
    "watchdog/app/static",
    "unraid",
    "docs",
    "tests/fixtures/acoustid-dumps",
    ".github/workflows",
]

REQUIRED_FILES = [
    ".env.example",
    ".gitignore",
    "LICENSE",
    "README.md",
    "pyproject.toml",
    # Ein Image, eine Compose-Datei (HANDOFF v2 §3, M1b).
    "Dockerfile",
    ".dockerignore",
    "docker-compose.yml",
    # Prozessdefinitionen und Vorlauf des Containers (v2 §11).
    "supervisor/supervisord.conf",
    "supervisor/supervisord.dev.conf",
    "supervisor/entrypoint.sh",
    "supervisor/mmo-postgres",
    "supervisor/mmo-fpindex",
    # GPL-Pflichten des eingebackenen acoustid-index (E7).
    "THIRD-PARTY-NOTICES.md",
    ".github/workflows/ci.yml",
    "tests/fixtures/fetch_fixtures.py",
    "tests/fixtures/acoustid-dumps/README.md",
    # Volume-Migration v1 -> v2 (Risiko R3).
    "docs/migration-v1-v2.md",
]

#: Was der Ein-Container-Umbau ersatzlos entfernt hat. Ein Test darauf ist
#: kein Selbstzweck: `docker-compose.watchdog.yml` trug den docker.sock-Mount,
#: und die drei Dockerfiles beschrieben drei Images — beides sind Zusagen,
#: die man versehentlich zurueckbringt, indem man eine alte Anleitung
#: befolgt.
REMOVED_FILES = [
    "docker-compose.watchdog.yml",
    "watchdog/Dockerfile",
    "api/Dockerfile",
    "importer/Dockerfile",
]

# Quellverzeichnis -> Import-Name (siehe Kommentar in der Wurzel-pyproject.toml).
PACKAGES = {
    "shared/shared": "shared",
    "api/app": "acoustid_api",
    "importer/app": "acoustid_importer",
    "watchdog/app": "acoustid_watchdog",
}


@pytest.mark.parametrize("relative_path", REQUIRED_DIRS)
def test_required_directory_exists(relative_path: str) -> None:
    assert (REPO_ROOT / relative_path).is_dir()


@pytest.mark.parametrize("relative_path", REQUIRED_FILES)
def test_required_file_exists(relative_path: str) -> None:
    assert (REPO_ROOT / relative_path).is_file()


@pytest.mark.parametrize("relative_path", REMOVED_FILES)
def test_removed_file_stays_removed(relative_path: str) -> None:
    """Ein Image, ein Container — die alten Bauplaene kommen nicht zurueck."""
    assert not (REPO_ROOT / relative_path).exists()


def test_no_docker_socket_anywhere_in_the_app() -> None:
    """Der docker.sock-Mount ist ersatzlos entfallen (v2 §3, E1).

    Die Mitigation des alten Risikos war „minimaler Codepfad"; jetzt gibt es
    den Pfad gar nicht mehr. Der Test haelt das fest, weil ein
    zurueckkehrender Mount die groesste Angriffsflaeche des Projekts
    wiederherstellen wuerde.
    """
    for package in ("shared/shared", "api/app", "importer/app", "watchdog/app"):
        for source in (REPO_ROOT / package).rglob("*.py"):
            assert "docker.sock" not in source.read_text(encoding="utf-8"), source


#: Standardfelder eines ``LogRecord``. Ein ``extra``-Feld mit einem dieser
#: Namen laesst ``logging`` mit ``KeyError`` abbrechen — und zwar erst zur
#: Laufzeit, an genau der Stelle, an der eigentlich eine Meldung stehen
#: sollte (LEARNINGS „reservierte LogRecord-Feldnamen in extra"; in M1b
#: einmal mehr passiert, mit ``extra={"process": …}``).
RESERVED_LOG_FIELDS = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}

#: ``extra={…}`` samt Inhalt; die Schluessel holt der Test daraus.
_EXTRA_BLOCK = re.compile(r"extra=\{([^}]*)\}", re.DOTALL)
_EXTRA_KEY = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')


def test_no_log_extra_uses_a_reserved_record_field() -> None:
    """Kein ``extra``-Feld heisst wie ein Standardfeld des LogRecords.

    Der Fehler ist besonders unangenehm, weil er nur den Pfad trifft, den
    er beschreibt: eine Warnung ueber einen unbekannten Zustand wird selbst
    zur Ausnahme. Deshalb ein Tripwire ueber alle vier Pakete.
    """
    offenders: list[str] = []
    for package in ("shared/shared", "api/app", "importer/app", "watchdog/app"):
        for source in (REPO_ROOT / package).rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            for match in _EXTRA_BLOCK.finditer(text):
                for key in _EXTRA_KEY.findall(match.group(1)):
                    if key in RESERVED_LOG_FIELDS:
                        offenders.append(f"{source.relative_to(REPO_ROOT)}: extra[{key!r}]")
    assert offenders == []


@pytest.mark.parametrize(("source_dir", "module_name"), sorted(PACKAGES.items()))
def test_package_is_importable_from_its_source_dir(source_dir: str, module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__version__ == "0.0.1"
    assert Path(module.__file__).resolve().parent == (REPO_ROOT / source_dir).resolve()


def test_dump_fixtures_are_not_committed() -> None:
    """Der Datenbestand wird nicht weiterverteilt (Lizenz CC BY-SA 3.0)."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tests/fixtures/acoustid-dumps/*.jsonl.gz" in gitignore
