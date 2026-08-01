"""Paketuebergreifende Tests des Repo-Grundgeruests (ARCHITECTURE.md §10).

Halten die vereinbarte Struktur fest, damit spaetere Phasen sie nicht
unbemerkt verlassen.
"""

import importlib
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
    "docker-compose.yml",
    # Waechter-Compose und -Image (Phase 14): der Dauerlaeufer ist bewusst
    # eine eigene Datei, damit ein `down` des Stacks die Steuerung stehen
    # laesst (ARCHITECTURE §10).
    "docker-compose.watchdog.yml",
    "watchdog/Dockerfile",
    ".github/workflows/ci.yml",
    "tests/fixtures/fetch_fixtures.py",
    "tests/fixtures/acoustid-dumps/README.md",
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


@pytest.mark.parametrize(("source_dir", "module_name"), sorted(PACKAGES.items()))
def test_package_is_importable_from_its_source_dir(source_dir: str, module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__version__ == "0.0.1"
    assert Path(module.__file__).resolve().parent == (REPO_ROOT / source_dir).resolve()


def test_dump_fixtures_are_not_committed() -> None:
    """Der Datenbestand wird nicht weiterverteilt (Lizenz CC BY-SA 3.0)."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tests/fixtures/acoustid-dumps/*.jsonl.gz" in gitignore
