"""Strukturiertes Logging nach stderr."""

import io
import json
import logging
import sys
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from shared.config import Config
from shared.logging_setup import HANDLER_NAME, setup_logging


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Der Root-Logger ist globaler Zustand — Testlauf sauber hinterlassen."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def _log_line(stream: io.StringIO, message: str = "hallo", **kwargs: object) -> dict[str, object]:
    logger = logging.getLogger("shared.tests.demo")
    logger.info(message, extra=dict(kwargs) or None)
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1, stream.getvalue()
    return json.loads(lines[0])


def test_line_is_json_with_the_core_fields() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-watchdog", stream=stream)
    payload = _log_line(stream, "Stack geweckt")

    assert payload["level"] == "INFO"
    assert payload["service"] == "acoustid-watchdog"
    assert payload["logger"] == "shared.tests.demo"
    assert payload["msg"] == "Stack geweckt"
    assert isinstance(payload["ts"], str)
    assert payload["ts"].endswith("Z")


def test_extra_fields_are_grouped() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-importer", stream=stream)
    payload = _log_line(stream, "Datei eingespielt", stream_name="track", rows=1234)
    assert payload["extra"] == {"stream_name": "track", "rows": 1234}


def test_message_arguments_are_rendered() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-api", stream=stream)
    logging.getLogger("shared.tests.demo").info("%s von %s", 3, 7)
    assert json.loads(stream.getvalue())["msg"] == "3 von 7"


def test_extra_cannot_overwrite_core_fields() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-api", stream=stream)
    payload = _log_line(stream, "test", service="fremd", level="TRACE")
    assert payload["service"] == "acoustid-api"
    assert payload["level"] == "INFO"
    assert payload["extra"] == {"service": "fremd", "level": "TRACE"}


def test_exceptions_are_included() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-api", stream=stream)
    logger = logging.getLogger("shared.tests.demo")
    try:
        raise ValueError("kaputt")
    except ValueError:
        logger.exception("Fehler beim Lookup")
    payload = json.loads(stream.getvalue())
    assert payload["level"] == "ERROR"
    assert "ValueError: kaputt" in payload["exc"]


def test_unserialisable_values_fall_back_to_str() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-api", stream=stream)
    payload = _log_line(stream, "objekt", obj=object())
    assert str(payload["extra"]["obj"]).startswith("<object object")


def test_secrets_are_masked_in_the_output() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-watchdog", stream=stream)
    config = Config.model_validate({"mb": {"dsn": "postgresql://ro:geheim@mb/musicbrainz"}})
    payload = _log_line(stream, "MB-Zugang geprueft", dsn=config.mb.dsn, raw=SecretStr("geheim"))

    assert "geheim" not in stream.getvalue()
    assert payload["extra"] == {"dsn": "**********", "raw": "**********"}


def test_whole_config_can_be_logged_without_leaking() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-watchdog", stream=stream)
    config = Config.model_validate(
        {"submit": {"mode": "local+upstream", "upstream_app_key": "geheim-key"}}
    )
    _log_line(stream, "Konfiguration geladen", config=config)
    assert "geheim-key" not in stream.getvalue()


def test_level_is_applied() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-api", level="WARNING", stream=stream)
    logger = logging.getLogger("shared.tests.demo")
    logger.info("wird verschluckt")
    logger.warning("wird geschrieben")
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["msg"] == "wird geschrieben"


def test_numeric_level_is_accepted() -> None:
    stream = io.StringIO()
    setup_logging("acoustid-api", level=logging.DEBUG, stream=stream)
    logging.getLogger("shared.tests.demo").debug("detail")
    assert json.loads(stream.getvalue())["level"] == "DEBUG"


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="LAUT"):
        setup_logging("acoustid-api", level="LAUT")


def test_repeated_setup_replaces_only_its_own_handler() -> None:
    foreign = logging.NullHandler()
    foreign.set_name("fremder-handler")
    root = logging.getLogger()
    root.addHandler(foreign)

    setup_logging("acoustid-api", stream=io.StringIO())
    stream = io.StringIO()
    setup_logging("acoustid-api", stream=stream)

    own = [handler for handler in root.handlers if handler.get_name() == HANDLER_NAME]
    assert len(own) == 1
    assert foreign in root.handlers
    _log_line(stream, "einmal")


def test_default_stream_is_stderr() -> None:
    setup_logging("acoustid-api")
    handler = next(
        handler for handler in logging.getLogger().handlers if handler.get_name() == HANDLER_NAME
    )
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_returned_logger_carries_the_service_name() -> None:
    stream = io.StringIO()
    logger = setup_logging("acoustid-importer", stream=stream)
    logger.info("start")
    assert json.loads(stream.getvalue())["logger"] == "acoustid-importer"
