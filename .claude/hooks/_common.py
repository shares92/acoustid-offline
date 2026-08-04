"""Gemeinsame Helfer der Projekt-Hooks (nur Python-3-stdlib).

Grundregel aller Hooks hier: **fail-open**. Ein Hook, der selbst kaputtgeht,
darf die Sitzung nie blockieren — Ausnahmen fuehren zu Exit 0.

Wichtig fuer Worktrees: Bau-Agenten arbeiten in
``.claude/worktrees/agent-*/``. Jeder Pfad-bezogene Hook leitet die
Projektwurzel deshalb aus dem *Datei*-Pfad ab (naechster Vorfahre mit
``uv.lock`` + ``pyproject.toml``) und nie aus einer fest verdrahteten Wurzel —
sonst formatiert/testet er im falschen Baum.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# --- Ein-/Ausgabe -----------------------------------------------------------


def read_event() -> dict:
    """Liest das Hook-JSON von stdin; unlesbar => leeres Ereignis (fail-open)."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def deny(reason: str) -> None:
    """PreToolUse-Sperre mit Begruendung; beendet den Prozess."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


# --- Projektwurzel ----------------------------------------------------------


def project_root_for(path: str | os.PathLike[str] | None) -> Path | None:
    """Naechste uv-Workspace-Wurzel oberhalb von ``path`` (worktree-treu)."""
    if not path:
        return None
    try:
        start = Path(path).expanduser().resolve()
    except Exception:
        return None
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        if (candidate / "uv.lock").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def session_root(event: dict) -> Path:
    """Wurzel fuer sitzungsweite Hooks: cwd des Ereignisses, sonst CLAUDE_PROJECT_DIR."""
    for candidate in (
        event.get("cwd"),
        os.environ.get("CLAUDE_PROJECT_DIR"),
        str(Path.cwd()),
    ):
        root = project_root_for(candidate)
        if root:
            return root
    return Path(event.get("cwd") or Path.cwd())


# --- Pfad-Muster der Sperrzonen --------------------------------------------

#: Angewendete Migrations-SQL (Drift-Sperre, PROGRESS „Fallstricke" / E8).
MIGRATION_SQL = re.compile(r"(^|/)shared/shared/db/sql/(core|indexes)/[^/]+\.sql$")

#: Echte Dump-Fixtures (DECISIONS 2026-07-25, .gitignore).
FIXTURE_DUMPS = re.compile(r"(^|/)tests/fixtures/acoustid-dumps/")


def as_posix(path: str) -> str:
    return str(path).replace("\\", "/")


# --- Prozessaufrufe ---------------------------------------------------------


def run(args: list[str], cwd: Path | str | None = None, timeout: int = 30) -> tuple[int, str]:
    """Fuehrt ein Kommando aus und liefert (returncode, stdout+stderr)."""
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # fail-open
        return 127, f"{type(exc).__name__}: {exc}"
