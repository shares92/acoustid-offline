#!/usr/bin/env python3
"""SessionStart: die Pflicht-Vorpruefung dieses Repos automatisch vorlegen.

PROGRESS „Arbeitsregeln" verlangt vor jedem Bau-Agenten-Start `git log` +
Statuskopf. Dieser Hook legt beides (plus Arbeitsbaum, Worktrees und
Docker-Zustand) unaufgefordert in den Kontext — stdout wird Kontext.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import read_event, run, session_root


def status_block(root: Path) -> str:
    """Statuskopf aus PROGRESS.md: ab '**Status (' bis zur ersten Leerzeile."""
    progress = root / "PROGRESS.md"
    if not progress.is_file():
        return "(PROGRESS.md nicht gefunden)"
    lines = progress.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        if not out:
            if line.lstrip().startswith("**Status ("):
                out.append(line)
            continue
        if not line.strip():
            break
        out.append(line)
    return "\n".join(out) if out else "(kein Statuskopf in PROGRESS.md gefunden)"


def main() -> int:
    root = session_root(read_event())
    print(f"=== AcoustID-Instanz — Vorpruefung ({root}) ===\n")

    _, log = run(["git", "log", "--oneline", "-5"], cwd=root)
    print("--- git log -5 ---")
    print(log.strip() or "(leer)")

    _, status = run(["git", "status", "--short"], cwd=root)
    print("\n--- git status --short ---")
    print(status.rstrip() or "(sauber)")

    print("\n--- PROGRESS.md, Statuskopf ---")
    print(status_block(root))

    _, worktrees = run(["git", "worktree", "list"], cwd=root)
    print("\n--- git worktree list ---")
    print(worktrees.strip() or "(keine)")

    code, _ = run(["docker", "info"], cwd=root, timeout=15)
    print("\n--- docker ---")
    if code == 0:
        print("docker: ok")
    else:
        print("docker: AUS → colima start --vz-rosetta")
        print("(Rosetta ist Pflicht: acoustid-index ist amd64-only und haengt sonst")
        print(" still unter qemu — LEARNINGS 'amd64-only-Images hängen unter qemu'.)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # fail-open
        sys.exit(0)
