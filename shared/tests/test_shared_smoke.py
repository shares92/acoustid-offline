"""Smoke-Test: das shared-Paket ist installiert und importierbar."""

import shared


def test_shared_exposes_version() -> None:
    assert shared.__version__ == "0.0.1"


def test_public_names_are_reexported() -> None:
    """`from shared import ...` genuegt fuer den Alltag der drei Services."""
    for name in shared.__all__:
        assert hasattr(shared, name), name


def test_config_and_logging_are_reachable_from_the_package_root() -> None:
    assert shared.Config().acoustid.index.query_hashes == 120
    assert shared.EnvSettings.from_env({}).port == 8080
    assert callable(shared.setup_logging)
