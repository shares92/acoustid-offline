"""Laden und atomares Schreiben der config.yaml."""

import logging
import os
import stat
from pathlib import Path

import pytest
import yaml

from shared.config import (
    Config,
    ConfigFileError,
    ConfigValidationError,
    config_to_dict,
    load_config,
    save_config,
)

FULL_CONFIG = {
    "auth": {"mode": "apikey", "allow_known_client_keys": True},
    "submit": {"mode": "local+upstream", "upstream_app_key": "app-key"},
    "wake": {"hold_timeout_s": 45},
    "idle": {"timeout_min": 30},
    "update": {"time": "03:15", "min_free_gb": 100},
    "cache": {"enabled": False, "max_size_mb": 1024},
    "ratelimit": {"per_ip_per_min": 60},
    "metrics": {"enabled": True},
    "notify": {
        "ntfy": {"url": "https://ntfy.example/acoustid"},
        "smtp": {
            "host": "mail.example",
            "port": 465,
            "user": "acoustid",
            "pass": "smtp-geheim",
            "from": "acoustid@example",
            "to": "admin@example",
        },
    },
    "backup": {"dir": "/mnt/backup", "time": "05:30"},
    "mb": {"dsn": "postgresql://acoustid_ro:dsn-geheim@mb/musicbrainz"},
    "index": {"query_hashes": 80},
}


def _write(path: Path, data: object) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# --- Laden -----------------------------------------------------------------


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    assert load_config(path) == Config()
    assert not path.exists()


def test_missing_file_can_be_created(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.yaml"
    config = load_config(path, create_if_missing=True)
    assert config == Config()
    assert path.is_file()
    assert load_config(path) == Config()


def test_load_and_save_emit_usable_log_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Extra-Felder duerfen nicht mit LogRecord-Attributen kollidieren.

    `logging` bricht sonst mit KeyError ab — und zwar erst, wenn der Level
    das Ereignis wirklich durchlaesst.
    """
    path = tmp_path / "config.yaml"
    with caplog.at_level(logging.INFO, logger="shared.config"):
        load_config(path, create_if_missing=True)
        save_config(Config(), path)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Defaults" in message for message in messages)
    assert any("geschrieben" in message for message in messages)
    assert all(record.config_path == str(path) for record in caplog.records)


def test_empty_file_yields_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert load_config(path) == Config()


def test_partial_file_is_filled_with_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", {"index": {"query_hashes": 80}})
    config = load_config(path)
    assert config.index.query_hashes == 80
    assert config.wake.hold_timeout_s == 90
    assert config.update.time == "04:00"


def test_full_file_is_read_completely(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", FULL_CONFIG)
    config = load_config(path)
    assert config_to_dict(config, reveal_secrets=True) == FULL_CONFIG


def test_unknown_keys_are_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "config.yaml",
        {
            "index": {"query_hashes": 80, "shard_count": 4},
            "kaffee": {"sorte": "filter"},
            "notify": {"smtp": {"pasword": "tippfehler"}},
        },
    )
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        config = load_config(path)

    assert config.index.query_hashes == 80
    reported = {record.config_key for record in caplog.records}
    assert reported == {"index.shard_count", "kaffee", "notify.smtp.pasword"}


def test_validation_error_names_the_key_path(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", {"notify": {"smtp": {"port": 0, "host": "m"}}})
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    paths = [path for path, _ in excinfo.value.errors]
    assert "notify.smtp.port" in paths
    assert "notify.smtp.port" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_broken_yaml_raises_config_file_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("auth: {mode: none\n", encoding="utf-8")
    with pytest.raises(ConfigFileError, match="kein gueltiges YAML"):
        load_config(path)


def test_non_mapping_yaml_raises_config_file_error(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", ["auth", "submit"])
    with pytest.raises(ConfigFileError, match="YAML-Mapping"):
        load_config(path)


def test_directory_instead_of_file_yields_defaults(tmp_path: Path) -> None:
    """`is_file()` statt `exists()`: ein Verzeichnis ist keine Config."""
    path = tmp_path / "config.yaml"
    path.mkdir()
    assert load_config(path) == Config()


# --- Schreiben -------------------------------------------------------------


def test_roundtrip_is_lossless(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = Config.model_validate(FULL_CONFIG)
    save_config(original, path)
    assert load_config(path) == original
    assert config_to_dict(load_config(path), reveal_secrets=True) == FULL_CONFIG


def test_roundtrip_of_defaults_is_lossless(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    assert load_config(path) == Config()


def test_written_file_has_no_unknown_keys(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Was save_config schreibt, laedt load_config ohne Warnung."""
    path = tmp_path / "config.yaml"
    save_config(Config.model_validate(FULL_CONFIG), path)
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        load_config(path)
    assert caplog.records == []


def test_written_file_is_readable_yaml_with_header(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# config.yaml")
    assert yaml.safe_load(text)["index"]["query_hashes"] == 120


def test_secrets_are_written_in_clear_text(tmp_path: Path) -> None:
    """Die Datei ist die Quelle der Wahrheit — maskiert waere sie kaputt."""
    path = tmp_path / "config.yaml"
    save_config(Config.model_validate(FULL_CONFIG), path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["notify"]["smtp"]["pass"] == "smtp-geheim"
    assert "**********" not in path.read_text(encoding="utf-8")


def test_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "neu" / "tief" / "config.yaml"
    save_config(Config(), path)
    assert path.is_file()


def test_file_mode_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    assert [entry.name for entry in tmp_path.iterdir()] == ["config.yaml"]


def test_write_uses_rename_within_the_target_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomar heisst: dieselbe Partition, ein `os.replace` am Ende."""
    path = tmp_path / "config.yaml"
    seen: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((Path(src), Path(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    save_config(Config(), path)

    assert len(seen) == 1
    src, dst = seen[0]
    assert src.parent == path.parent
    assert dst == path


def test_failed_write_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    before = path.read_text(encoding="utf-8")

    def boom(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("kein Platz mehr")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="kein Platz mehr"):
        save_config(Config.model_validate(FULL_CONFIG), path)

    assert path.read_text(encoding="utf-8") == before
    assert [entry.name for entry in tmp_path.iterdir()] == ["config.yaml"]


def test_overwriting_replaces_the_content(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    save_config(Config.model_validate({"index": {"query_hashes": 80}}), path)
    assert load_config(path).index.query_hashes == 80
