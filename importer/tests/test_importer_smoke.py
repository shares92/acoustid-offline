"""Smoke-Test: das Importer-Paket ist installiert und importierbar."""

import acoustid_importer
import shared


def test_importer_exposes_version() -> None:
    assert acoustid_importer.__version__ == "0.0.1"


def test_importer_can_import_shared() -> None:
    assert shared.__version__ == acoustid_importer.__version__
