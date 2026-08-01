"""Admin-Login und der Erststart-Pfad (ARCHITECTURE §6, Phase 14)."""

from __future__ import annotations

import logging

import pytest

from acoustid_watchdog.admin import (
    ADMIN_LOGIN,
    ensure_admin_user,
    generate_password,
    hash_password,
    load_admin_user,
    set_password,
    verify_password,
)
from acoustid_watchdog.store import Database


def test_no_admin_before_the_first_start(db: Database) -> None:
    assert load_admin_user(db) is None


def test_first_start_creates_an_admin_with_a_generated_password(db: Database) -> None:
    password = ensure_admin_user(db)
    assert password is not None

    user = load_admin_user(db)
    assert user is not None
    assert user.login == ADMIN_LOGIN
    assert verify_password(user.password_hash, password).ok is True


def test_first_start_logs_the_password_in_clear_text(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """ARCHITECTURE §6: „Erst-Passwort beim ersten Start generiert und geloggt"."""
    with caplog.at_level(logging.WARNING):
        password = ensure_admin_user(db)

    assert password is not None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert password in logged
    assert ADMIN_LOGIN in logged


def test_the_stored_value_is_a_hash_not_the_password(db: Database) -> None:
    password = ensure_admin_user(db)
    user = load_admin_user(db)
    assert user is not None and password is not None
    assert password not in user.password_hash
    assert user.password_hash.startswith("$argon2")


def test_second_start_changes_nothing_and_logs_nothing(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Ein Neustart darf weder ein neues Passwort erzeugen noch eines loggen."""
    first_password = ensure_admin_user(db)
    before = load_admin_user(db)

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert ensure_admin_user(db) is None

    after = load_admin_user(db)
    assert before is not None and after is not None
    assert after.password_hash == before.password_hash
    assert after.updated_at == before.updated_at
    assert first_password is not None
    assert first_password not in "\n".join(record.getMessage() for record in caplog.records)


def test_wrong_password_is_rejected(db: Database) -> None:
    ensure_admin_user(db)
    user = load_admin_user(db)
    assert user is not None
    assert verify_password(user.password_hash, "falsch").ok is False


def test_broken_hash_is_rejected_without_raising() -> None:
    """Kaputter Hash und falsches Passwort sind beides schlicht „nicht angemeldet"."""
    assert verify_password("kein argon2-hash", "egal").ok is False
    assert verify_password("", "egal").ok is False


def test_set_password_replaces_the_hash_and_keeps_created_at(db: Database) -> None:
    ensure_admin_user(db)
    before = load_admin_user(db)
    assert before is not None

    after = set_password(db, "neues-passwort")
    assert after.created_at == before.created_at
    assert after.password_hash != before.password_hash
    assert verify_password(after.password_hash, "neues-passwort").ok is True
    assert verify_password(after.password_hash, "altes-passwort").ok is False


def test_hashes_are_salted(db: Database) -> None:
    assert hash_password("gleiches-passwort") != hash_password("gleiches-passwort")


def test_generated_passwords_are_long_and_unique() -> None:
    passwords = {generate_password() for _ in range(50)}
    assert len(passwords) == 50
    assert all(len(password) >= 20 for password in passwords)


def test_fresh_hash_does_not_need_a_rehash(db: Database) -> None:
    password = ensure_admin_user(db)
    user = load_admin_user(db)
    assert user is not None and password is not None
    check = verify_password(user.password_hash, password)
    assert check.ok is True
    assert check.needs_rehash is False
