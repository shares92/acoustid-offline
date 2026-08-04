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

import contextlib
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


def _decide(decision: str, reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def deny(reason: str) -> None:
    """PreToolUse-Sperre mit Begruendung; beendet den Prozess."""
    _decide("deny", reason)


def ask(reason: str) -> None:
    """PreToolUse-Rueckfrage: nicht verboten, aber nie unbemerkt (beendet den Prozess)."""
    _decide("ask", reason)


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


def hook_project_dir() -> Path | None:
    """Das Projekt, zu dem DIESE Hooks gehoeren (nie der Nachbar-Ordner)."""
    raw = os.environ.get("CLAUDE_PROJECT_DIR")
    if raw:
        with contextlib.suppress(Exception):
            return Path(raw).expanduser().resolve()
    with contextlib.suppress(Exception):  # .claude/hooks/_common.py -> Projektwurzel
        return Path(__file__).resolve().parents[2]
    return None


def git_common_dir(path: Path | str) -> str | None:
    """``git rev-parse --git-common-dir`` als absoluter Pfad (Worktrees teilen ihn)."""
    code, out = run(["git", "rev-parse", "--git-common-dir"], cwd=path, timeout=10)
    if code != 0 or not out.strip():
        return None
    with contextlib.suppress(Exception):
        found = Path(out.strip().splitlines()[0])
        if not found.is_absolute():
            found = Path(path) / found
        return str(found.resolve())
    return None


def within_project(root: Path) -> bool:
    """Liegt ``root`` im eigenen Projekt (oder in einem seiner Worktrees)?

    Schutz vor dem Nachbar-Repo: neben „AcoustID Instanz" liegt „AcoustID
    Instanz GPT" — ohne diese Pruefung wuerde ein aus dem Dateipfad
    abgeleiteter Wurzelpfad dort fremd formatieren und `uv sync` anstossen.
    """
    project = hook_project_dir()
    if project is None:
        return True  # keine Zuordnung moeglich => nicht im Weg stehen
    try:
        root = Path(root).resolve()
    except Exception:
        return False
    if root == project or project in root.parents:
        return True
    mine = git_common_dir(project)
    theirs = git_common_dir(root)
    return bool(mine and theirs and mine == theirs)


# --- Pfad-Muster der Sperrzonen --------------------------------------------

#: Angewendete Migrations-SQL (Drift-Sperre, PROGRESS „Fallstricke" / E8).
MIGRATION_SQL = re.compile(r"(^|/)shared/shared/db/sql/(core|indexes)/[^/]+\.sql$")

#: Echte Dump-Fixtures (DECISIONS 2026-07-25, .gitignore).
FIXTURE_DUMPS = re.compile(r"(^|/)tests/fixtures/acoustid-dumps/")


def as_posix(path: str) -> str:
    return str(path).replace("\\", "/")


# --- Prozessaufrufe ---------------------------------------------------------


#: Meldungen, die belegen: nicht der Code ist kaputt, sondern das Werkzeug.
#: Bewusst NICHT „Failed to parse" — das ist der Syntaxfehler-Befund von ruff.
TOOL_BROKEN = (
    "Failed to spawn",
    "failed to spawn",
    "command not found",
    "No such file or directory",
    "No `pyproject.toml` found",
    "Failed to prepare environment",
    "Failed to install",
    "Failed to create virtualenv",
    "No interpreter found",
)


def tool_broken(code: int, out: str) -> bool:
    """uv/ruff/pytest gar nicht startbar? Nur dann bleibt ein Hook still (fail-open).

    Die Marker werden nur in Werkzeug-Meldungszeilen gesucht (``error:`` …),
    nicht im echten Diagnosetext — ruff und pytest drucken den fehlerhaften
    Quelltext mit aus, und der darf den Hook nicht taeuschen.
    """
    if code == 127:  # run() konnte den Prozess nicht starten
        return True
    return any(
        line.lstrip().startswith(("error:", "uv:", "Traceback"))
        and any(marker in line for marker in TOOL_BROKEN)
        for line in out.splitlines()
    )


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
