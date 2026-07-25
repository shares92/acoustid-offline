"""Smoke-Test: das API-Paket ist installiert und importierbar."""

import acoustid_api
import shared


def test_api_exposes_version() -> None:
    assert acoustid_api.__version__ == "0.0.1"


def test_api_can_import_shared() -> None:
    assert shared.__version__ == acoustid_api.__version__
