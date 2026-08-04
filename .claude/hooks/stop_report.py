#!/usr/bin/env python3
"""Stop: drei stille Nachlaessigkeiten dieses Repos melden — nie ausfuehren.

1. Code geaendert, aber PROGRESS.md/DECISIONS.md unangetastet
   (Betreiber-Vorgabe: Doku vor der naechsten Phase auf Stand bringen).
2. Agent-Worktrees unter .claude/worktrees/ bzw. verwaiste
   worktree-agent-*-Branches (Aufraeumen nach dem ff-Merge).
3. Unpushed Commits (CI laeuft erst nach dem Push).

Der Hook meldet ausschliesslich (systemMessage) und blockiert nie:
`stop_hook_active` wird beachtet, der Exit-Code ist immer 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import read_event, run, session_root

DOC_FILES = {"PROGRESS.md", "DECISIONS.md"}
DOC_SUFFIXES = (".md",)
CODE_SUFFIXES = (".py", ".sql", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".lock")


def changed_paths(root: Path) -> tuple[set[str], set[str]]:
    """(unversioniert/geaendert im Arbeitsbaum, in unpushed Commits beruehrt)."""
    _, porcelain = run(["git", "status", "--porcelain"], cwd=root)
    working = set()
    for line in porcelain.splitlines():
        if len(line) > 3:
            entry = line[3:].strip()
            entry = entry.split(" -> ")[-1]  # Umbenennungen
            working.add(entry.strip('"'))

    code, rng = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=root)
    upstream = rng.strip() if code == 0 else ""
    committed: set[str] = set()
    if upstream:
        _, files = run(["git", "diff", "--name-only", f"{upstream}..HEAD"], cwd=root)
        committed = {f.strip() for f in files.splitlines() if f.strip()}
    return working, committed


def is_code(path: str) -> bool:
    name = Path(path).name
    if name in DOC_FILES:
        return False
    # Doku, Sitzungsarchiv und die Claude-Konfiguration sind kein Projektcode.
    if path.startswith(("docs/", "sessions/", ".claude/")):
        return False
    return not path.endswith(DOC_SUFFIXES) and (
        path.endswith(CODE_SUFFIXES) or "Dockerfile" in name
    )


def main() -> int:
    event = read_event()
    if event.get("stop_hook_active"):
        return 0
    root = session_root(event)
    if not (root / ".git").exists():
        return 0

    notes: list[str] = []

    # (1) Doku-Sweep vergessen? ------------------------------------------------
    working, committed = changed_paths(root)
    touched = working | committed
    if (
        touched
        and any(is_code(p) for p in touched)
        and not any(Path(p).name in DOC_FILES for p in touched)
    ):
        where = []
        if any(is_code(p) for p in working):
            where.append("Arbeitsbaum")
        if any(is_code(p) for p in committed):
            where.append("unpushed Commits")
        notes.append(
            f"Doku-Sweep fehlt: Code geaendert ({', '.join(where)}), aber weder "
            "PROGRESS.md noch DECISIONS.md angefasst — Betreiber-Vorgabe: Doku vor "
            "der naechsten Phase aktualisieren."
        )

    # (2) Worktrees / verwaiste Branches ---------------------------------------
    # --porcelain, weil der Repo-Pfad Leerzeichen enthaelt ("AcoustID Instanz").
    _, wt = run(["git", "worktree", "list", "--porcelain"], cwd=root)
    paths = [ln[9:] for ln in wt.splitlines() if ln.startswith("worktree ")]
    in_use = {ln[18:] for ln in wt.splitlines() if ln.startswith("branch refs/heads/")}
    live = [p for p in paths if "/.claude/worktrees/" in p]
    if live:
        notes.append(
            f"{len(live)} Agent-Worktree(s) offen unter .claude/worktrees/ — nach dem "
            "ff-Merge aufraeumen (`git worktree remove`, dann `git worktree prune`):\n  "
            + "\n  ".join(live)
        )
    _, branches = run(
        ["git", "branch", "--list", "worktree-agent-*", "--format=%(refname:short)"], cwd=root
    )
    orphan = [b.strip() for b in branches.splitlines() if b.strip() and b.strip() not in in_use]
    if orphan:
        notes.append("Verwaiste worktree-agent-*-Branches ohne Worktree: " + ", ".join(orphan))

    # (3) Unpushed Commits ------------------------------------------------------
    code, ahead = run(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=root)
    if code == 0 and ahead.strip().isdigit() and int(ahead.strip()) > 0:
        notes.append(f"{ahead.strip()} Commit(s) nicht gepusht — CI laeuft erst nach dem Push.")

    if notes:
        print(json.dumps({"systemMessage": "Sitzungsende — Hinweise:\n- " + "\n- ".join(notes)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # fail-open
        sys.exit(0)
