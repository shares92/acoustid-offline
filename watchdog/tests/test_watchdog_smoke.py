"""Smoke-Test: das Waechter-Paket ist installiert und importierbar."""

from pathlib import Path

import acoustid_watchdog
import shared


def test_watchdog_exposes_version() -> None:
    assert acoustid_watchdog.__version__ == "0.0.1"


def test_watchdog_can_import_shared() -> None:
    assert shared.__version__ == acoustid_watchdog.__version__


def test_admin_ui_asset_dirs_exist() -> None:
    """Admin-UI ohne Build-Schritt: Templates und statische Dateien liegen im Paket."""
    package_dir = Path(acoustid_watchdog.__file__).parent
    assert (package_dir / "templates").is_dir()
    assert (package_dir / "static").is_dir()
