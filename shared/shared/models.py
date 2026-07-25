"""Gemeinsame Modelle und Typen von acoustid-offline.

Hier stehen ausschliesslich Typen, die schon jetzt verbindlich feststehen
(ARCHITECTURE §6, §5.2, §9). Kein Vorgriff auf spaetere Phasen: Tabellen-
und API-Modelle entstehen dort, wo sie gebraucht werden.

**Sprachkonvention (Phase 3):** Bezeichner UND Wire-/Speicherwerte sind
englisch, weil sie in Config-Dateien, JSON-Antworten, der SQLite und der
Postgres landen. Rein anzeigende deutsche Texte haengen als
`display_name` am jeweiligen Enum-Wert (ARCHITECTURE §9: UI-Sprache
Deutsch). Damit gibt es genau eine maschinenlesbare Schreibweise pro
Zustand und trotzdem deutsche Beschriftungen.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AuthMode",
    "StackState",
    "SubmissionStatus",
    "SubmitMode",
]


class AuthMode(StrEnum):
    """Werte von `auth.mode` (ARCHITECTURE §6).

    `none`   — `client`-Parameter wird ignoriert, aber akzeptiert.
    `apikey` — der Waechter prueft den Key am Proxy (ARCHITECTURE §7).
    """

    NONE = "none"
    APIKEY = "apikey"

    @property
    def display_name(self) -> str:
        return _AUTH_MODE_DISPLAY[self]


class SubmitMode(StrEnum):
    """Werte von `submit.mode` (ARCHITECTURE §6).

    `off`            — `/v2/submit` nimmt nichts an.
    `local`          — Einreichungen landen nur im eigenen Bestand.
    `local+upstream` — zusaetzlich Weiterleitung an api.acoustid.org.
    """

    OFF = "off"
    LOCAL = "local"
    LOCAL_UPSTREAM = "local+upstream"

    @property
    def display_name(self) -> str:
        return _SUBMIT_MODE_DISPLAY[self]


class SubmissionStatus(StrEnum):
    """Statusmaschine von `local_submission` (ARCHITECTURE §5.2, §8.9).

    Uebergaenge: `new` -> `indexed` -> `forwarded` | `forward_failed`;
    `forward_failed` wird beim naechsten Update-Lauf erneut versucht.
    Die Werte werden so in der Postgres gespeichert.
    """

    NEW = "new"
    INDEXED = "indexed"
    FORWARDED = "forwarded"
    FORWARD_FAILED = "forward_failed"

    @property
    def display_name(self) -> str:
        return _SUBMISSION_STATUS_DISPLAY[self]


class StackState(StrEnum):
    """Zustaende des schlafenden Stacks (ARCHITECTURE §7 `/status`, §9).

    `sleeping` ist der Normal- und Gutzustand, kein Fehler.
    Die deutschen Beschriftungen aus §9 liefert `display_name`.
    """

    SLEEPING = "sleeping"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    ERROR = "error"

    @property
    def display_name(self) -> str:
        return _STACK_STATE_DISPLAY[self]


_AUTH_MODE_DISPLAY: dict[AuthMode, str] = {
    AuthMode.NONE: "keine",
    AuthMode.APIKEY: "API-Key",
}

_SUBMIT_MODE_DISPLAY: dict[SubmitMode, str] = {
    SubmitMode.OFF: "aus",
    SubmitMode.LOCAL: "lokal",
    SubmitMode.LOCAL_UPSTREAM: "lokal + Upstream",
}

_SUBMISSION_STATUS_DISPLAY: dict[SubmissionStatus, str] = {
    SubmissionStatus.NEW: "neu",
    SubmissionStatus.INDEXED: "indiziert",
    SubmissionStatus.FORWARDED: "weitergeleitet",
    SubmissionStatus.FORWARD_FAILED: "Weiterleitung fehlgeschlagen",
}

# Wortlaut exakt wie ARCHITECTURE §9 (Kleinschreibung dort ist Absicht).
_STACK_STATE_DISPLAY: dict[StackState, str] = {
    StackState.SLEEPING: "schlafend",
    StackState.STARTING: "startet",
    StackState.READY: "bereit",
    StackState.STOPPING: "stoppt",
    StackState.ERROR: "fehler",
}
