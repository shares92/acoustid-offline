"""Smoke-Test: das shared-Paket ist installiert und importierbar."""

import shared


def test_shared_exposes_version() -> None:
    assert shared.__version__ == "0.0.1"
