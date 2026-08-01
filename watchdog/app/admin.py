"""Admin-Login des Waechters — ``admin_user`` und der Erststart (§6).

ARCHITECTURE §6 „Feste Werte": *ein* Benutzer, Passwort-Hash (argon2) in der
SQLite, **Erst-Passwort beim ersten Start generiert und geloggt**. Genau das
steht hier; die Login-Maske und das Session-Cookie kommen in Phase 23.

**Wohin das Klartext-Passwort geht — und wohin nicht.** Es wird genau
einmal geschrieben: als Warnung ins Containerlog, wo der Betreiber es mit
`docker logs acoustid-watchdog` abholt. Nicht ins ``event_log``: das ist
persistent und wird ueber `/admin/logs` angezeigt — also hinter genau der
Anmeldung, fuer die das Passwort gilt. Ein dauerhaft gespeichertes
Klartext-Passwort waere dort kein Komfort, sondern ein Leck. Das
Ereignis-Log bekommt deshalb nur den Vermerk, *dass* ein Passwort erzeugt
wurde (:mod:`acoustid_watchdog.service`).

**Warum argon2 und nicht bcrypt/PBKDF2.** So steht es in §6, und es ist die
speicherharte Wahl. Die Parameter bleiben die Vorgaben von ``argon2-cffi``
(RFC-9106-nah): ein Login je Sitzung rechtfertigt keine eigene
Kalibrierung, und die Vorgaben wandern mit der Bibliothek nach oben.
:func:`verify_password` meldet ueber
:meth:`~argon2.PasswordHasher.check_needs_rehash` mit, wenn ein
gespeicherter Hash aelteren Parametern folgt — dann schreibt der
Login-Pfad (Phase 23) ihn neu.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from acoustid_watchdog.store import Database, utc_now

__all__ = [
    "ADMIN_LOGIN",
    "PASSWORD_ENTROPY_BYTES",
    "AdminUser",
    "PasswordCheck",
    "ensure_admin_user",
    "generate_password",
    "hash_password",
    "load_admin_user",
    "set_password",
    "verify_password",
]

_LOG = logging.getLogger(__name__)

#: Der einzige Login (ARCHITECTURE §11: keine Rollenverwaltung). Er steht
#: trotzdem als Spalte in der Tabelle — die Anmeldemaske zeigt ihn an, und
#: eine spaetere Umbenennung braucht dann keine Migration.
ADMIN_LOGIN: Final = "admin"

#: Zufallsbytes des Erst-Passworts. 18 Bytes ergeben ueber
#: ``secrets.token_urlsafe`` 24 Zeichen aus [A-Za-z0-9_-] — 144 Bit, per
#: Doppelklick markierbar und ohne Zeichen, die in einem Terminal-Log
#: mehrdeutig aussehen.
PASSWORD_ENTROPY_BYTES: Final = 18

_HASHER: Final = PasswordHasher()


@dataclass(frozen=True, slots=True)
class AdminUser:
    """Der Datensatz aus ``admin_user`` — ohne Klartext, versteht sich."""

    login: str
    password_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    """Ergebnis einer Passwortpruefung.

    ``needs_rehash`` heisst: das Passwort stimmt, der gespeicherte Hash
    folgt aber aelteren argon2-Parametern und sollte beim naechsten
    erfolgreichen Login neu geschrieben werden.
    """

    ok: bool
    needs_rehash: bool = False


def generate_password() -> str:
    """Ein neues Zufallspasswort (URL-sicher, 24 Zeichen)."""
    return secrets.token_urlsafe(PASSWORD_ENTROPY_BYTES)


def hash_password(password: str) -> str:
    """argon2-Hash inklusive Salt und Parametern (PHC-String)."""
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> PasswordCheck:
    """Prueft ein Passwort gegen seinen Hash — ohne je zu werfen.

    Ein falsches Passwort und ein kaputter Hash sind beides schlicht „nicht
    angemeldet"; eine Ausnahme wuerde den Unterschied ueber die
    Fehlermeldung nach aussen tragen.
    """
    try:
        _HASHER.verify(password_hash, password)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return PasswordCheck(ok=False)
    try:
        needs_rehash = _HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:  # pragma: no cover - verify() haette schon geworfen
        needs_rehash = False
    return PasswordCheck(ok=True, needs_rehash=needs_rehash)


def load_admin_user(db: Database) -> AdminUser | None:
    """Der Admin-Datensatz, oder ``None`` vor dem Erststart."""
    with db.transaction() as tx:
        row = tx.execute(
            "SELECT login, password_hash, created_at, updated_at FROM admin_user WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    return AdminUser(
        login=str(row["login"]),
        password_hash=str(row["password_hash"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def set_password(db: Database, password: str, *, login: str = ADMIN_LOGIN) -> AdminUser:
    """Setzt (oder ersetzt) das Admin-Passwort.

    Genutzt vom Erststart und spaeter von `/admin/config` („Admin-Passwort",
    Phase 25). Der Klartext verlaesst diese Funktion nicht.
    """
    password_hash = hash_password(password)
    now = utc_now()
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO admin_user (id, login, password_hash, created_at, updated_at)"
            " VALUES (1, ?, ?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET"
            " login = excluded.login,"
            " password_hash = excluded.password_hash,"
            " updated_at = excluded.updated_at",
            (login, password_hash, now, now),
        )
        row = tx.execute(
            "SELECT login, password_hash, created_at, updated_at FROM admin_user WHERE id = 1"
        ).fetchone()
    return AdminUser(
        login=str(row["login"]),
        password_hash=str(row["password_hash"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def ensure_admin_user(db: Database, *, login: str = ADMIN_LOGIN) -> str | None:
    """Legt beim Erststart einen Admin an und protokolliert sein Passwort.

    Idempotent: existiert der Datensatz schon, passiert nichts und es wird
    nichts geloggt — ein Neustart des Containers darf das Passwort weder
    aendern noch erneut in die Logs schreiben.

    Returns:
        Das erzeugte Klartext-Passwort beim Erststart, sonst ``None``.
    """
    # Pruefen und Anlegen in **einer** Transaktion, damit zwei gleichzeitig
    # startende Prozesse nicht zwei Passwoerter erzeugen, von denen nur
    # eines gilt.
    with db.transaction():
        if load_admin_user(db) is not None:
            return None
        password = generate_password()
        set_password(db, password, login=login)

    _LOG.warning(
        "Erststart: Admin-Passwort erzeugt — jetzt notieren, es wird nie wieder "
        "angezeigt. Login '%s', Passwort: %s",
        login,
        password,
    )
    return password
