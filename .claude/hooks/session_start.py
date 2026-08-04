#!/usr/bin/env python3
"""SessionStart: die Pflicht-Vorpruefung dieses Repos automatisch vorlegen.

PROGRESS „Arbeitsregeln" verlangt vor jedem Bau-Agenten-Start `git log` +
Statuskopf. Dieser Hook legt beides (plus Arbeitsbaum, Worktrees und
Docker-Zustand) unaufgefordert in den Kontext — stdout wird Kontext.
Er laeuft nur bei `startup|resume` (matcher in settings.json), nicht erneut
nach /compact oder /clear.

Er ist eine Vorlage, **kein Ersatz fuer die Pflichtlektuere**: ARCHITECTURE.md
und PROGRESS.md werden trotzdem vollstaendig gelesen (CLAUDE.md).

Zeitbudget: 3x5 s git + 10 s docker bleiben unter dem Hook-Timeout (30 s).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import read_event, run, session_root

GIT_TIMEOUT = 5


def status_block(root: Path) -> str:
    """Statuskopf aus PROGRESS.md: ab der '**Status'-Zeile bis zur Leerzeile.

    Bewusst ohne die Klammer aus '**Status (2026-…)': das Datumsformat hat
    sich schon geaendert, und ein stiller Fehlschlag wuerde die Pflicht-
    Vorpruefung aushoehlen — deshalb meldet der Fallback deutlich.
    """
    progress = root / "PROGRESS.md"
    if not progress.is_file():
        return f"!!! PROGRESS.md nicht gefunden unter {root} — Statuskopf VON HAND lesen."
    lines = progress.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        if not out:
            if line.lstrip().startswith("**Status"):
                out.append(line)
            continue
        if not line.strip():
            break
        out.append(line)
    if out:
        return "\n".join(out)
    return (
        "!!! Kein Statuskopf in PROGRESS.md gefunden (gesucht: Zeile beginnt mit "
        "'**Status'). Die Pflicht-Vorpruefung ist damit UNVOLLSTAENDIG — PROGRESS.md "
        "jetzt selbst oeffnen und den Statuskopf lesen; danach das Muster hier "
        "nachziehen (.claude/hooks/session_start.py)."
    )


def main() -> int:
    root = session_root(read_event())
    print(f"=== AcoustID-Instanz — Vorpruefung ({root}) ===\n")

    _, log = run(["git", "log", "--oneline", "-5"], cwd=root, timeout=GIT_TIMEOUT)
    print("--- git log -5 ---")
    print(log.strip() or "(leer)")

    _, status = run(["git", "status", "--short"], cwd=root, timeout=GIT_TIMEOUT)
    print("\n--- git status --short ---")
    print(status.rstrip() or "(sauber)")

    print("\n--- PROGRESS.md, Statuskopf ---")
    print(status_block(root))

    _, worktrees = run(["git", "worktree", "list"], cwd=root, timeout=GIT_TIMEOUT)
    print("\n--- git worktree list ---")
    print(worktrees.strip() or "(keine)")

    code, _ = run(["docker", "info"], cwd=root, timeout=10)
    print("\n--- docker ---")
    if code == 0:
        print("docker: ok")
    else:
        print("docker: AUS → colima start --vz-rosetta")
        print("(Rosetta ist Pflicht: acoustid-index ist amd64-only und haengt sonst")
        print(" still unter qemu — LEARNINGS 'amd64-only-Images hängen unter qemu'.)")

    print("\n(Diese Vorlage ersetzt die Pflichtlektuere nicht: ARCHITECTURE.md und")
    print(" PROGRESS.md vollstaendig lesen, bevor gebaut oder beauftragt wird.)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # fail-open
        sys.exit(0)
