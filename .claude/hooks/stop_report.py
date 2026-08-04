#!/usr/bin/env python3
"""Stop: drei stille Nachlaessigkeiten dieses Repos melden — nie ausfuehren.

1. Code geaendert, aber PROGRESS.md/DECISIONS.md unangetastet
   (Betreiber-Vorgabe: Doku vor der naechsten Phase auf Stand bringen).
   Gemeldet wird, WELCHE der beiden Steuerdateien unberuehrt blieb.
2. Agent-Worktrees unter .claude/worktrees/ bzw. verwaiste
   worktree-agent-*-Branches (Aufraeumen nach dem ff-Merge).
3. Unpushed Commits (CI laeuft erst nach dem Push). Ohne gesetzten Upstream —
   der Normalfall in einem Agent-Worktree — wird gegen `origin/main`
   verglichen und als „noch nicht in main integriert" gemeldet; ein detached
   HEAD wird ausdruecklich benannt.

Der Hook meldet ausschliesslich (systemMessage) und blockiert nie:
`stop_hook_active` wird beachtet, der Exit-Code ist immer 0.

Zeitbudget: alle Aufrufe sind reine git-Metadaten-Abfragen mit 3 s Limit —
in Summe unter dem Hook-Timeout (30 s) in settings.json.
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

#: Kandidaten fuer den Vergleich, wenn kein Upstream gesetzt ist.
FALLBACK_REFS = ("origin/main", "origin/master", "main", "master")

GIT_TIMEOUT = 3


def git(root: Path, *args: str) -> tuple[int, str]:
    return run(["git", *args], cwd=root, timeout=GIT_TIMEOUT)


def head_branch(root: Path) -> str | None:
    """Aktueller Branch, oder None bei detached HEAD."""
    code, out = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out.strip() if code == 0 and out.strip() else None


def compare_ref(root: Path, branch: str | None) -> tuple[str | None, bool]:
    """(Ref, ob es der echte Upstream ist) — Fallback: origin/main & Co."""
    code, out = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code == 0 and out.strip() and "@{u}" not in out:
        return out.strip(), True
    for ref in FALLBACK_REFS:
        if ref == branch:
            continue  # der eigene Branch taugt nicht als Messlatte
        if git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] == 0:
            return ref, False
    return None, False


def changed_paths(root: Path, ref: str | None) -> tuple[set[str], set[str]]:
    """(unversioniert/geaendert im Arbeitsbaum, in nicht integrierten Commits beruehrt)."""
    _, porcelain = git(root, "status", "--porcelain")
    working = set()
    for line in porcelain.splitlines():
        if len(line) > 3:
            entry = line[3:].strip()
            entry = entry.split(" -> ")[-1]  # Umbenennungen
            working.add(entry.strip('"'))

    committed: set[str] = set()
    if ref:
        _, files = git(root, "diff", "--name-only", f"{ref}..HEAD")
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


def doc_note(touched: set[str], working: set[str], committed: set[str]) -> str | None:
    """(1) Welche Steuerdatei fehlt im Doku-Sweep?"""
    if not any(is_code(p) for p in touched):
        return None
    missing = sorted(DOC_FILES - {Path(p).name for p in touched})
    if not missing:
        return None
    where = []
    if any(is_code(p) for p in working):
        where.append("Arbeitsbaum")
    if any(is_code(p) for p in committed):
        where.append("nicht integrierte Commits")
    scope = ", ".join(where) or "unbekannt"
    if len(missing) == 2:
        return (
            f"Doku-Sweep fehlt: Code geaendert ({scope}), aber weder PROGRESS.md noch "
            "DECISIONS.md angefasst — Betreiber-Vorgabe: Doku vor der naechsten Phase "
            "aktualisieren."
        )
    done = sorted(DOC_FILES - set(missing))[0]
    hint = (
        "dort gehoert der Statusvermerk hin"
        if missing[0] == "PROGRESS.md"
        else "fiel dabei ein Entscheid, gehoert er dorthin"
    )
    return (
        f"Doku-Sweep halb: Code geaendert ({scope}), {done} ist nachgezogen, aber "
        f"{missing[0]} blieb unberuehrt — {hint}."
    )


def integration_note(
    root: Path, branch: str | None, ref: str | None, is_upstream: bool
) -> str | None:
    """(3) Nicht gepushte bzw. nicht integrierte Commits."""
    prefix = "" if branch else "HEAD ist detached (kein Branch — vor dem Push einen setzen). "

    if ref is None:
        return (
            f"{prefix}Kein Upstream und kein origin/main als Messlatte — ob hier Commits "
            "ungepusht liegen, laesst sich nicht feststellen; vor dem Sitzungsende selbst "
            "pruefen."
        )
    code, ahead = git(root, "rev-list", "--count", f"{ref}..HEAD")
    if code != 0 or not ahead.strip().isdigit():
        return f"{prefix}Vergleich gegen {ref} nicht moeglich — Push-Stand unbekannt."
    count = int(ahead.strip())
    if count == 0:
        return prefix.strip() or None
    if is_upstream:
        return f"{prefix}{count} Commit(s) nicht gepusht — CI laeuft erst nach dem Push."
    return (
        f"{prefix}{count} Commit(s) noch nicht in main integriert (kein Upstream gesetzt, "
        f"Vergleich gegen {ref}) — typisch im Agent-Worktree: erst ff-Merge, dann Push, "
        "sonst laeuft keine CI."
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
    branch = head_branch(root)
    ref, is_upstream = compare_ref(root, branch)
    working, committed = changed_paths(root, ref)
    note = doc_note(working | committed, working, committed)
    if note:
        notes.append(note)

    # (2) Worktrees / verwaiste Branches ---------------------------------------
    # --porcelain, weil der Repo-Pfad Leerzeichen enthaelt ("AcoustID Instanz").
    _, wt = git(root, "worktree", "list", "--porcelain")
    paths = [ln[9:] for ln in wt.splitlines() if ln.startswith("worktree ")]
    in_use = {ln[18:] for ln in wt.splitlines() if ln.startswith("branch refs/heads/")}
    live = [p for p in paths if "/.claude/worktrees/" in p]
    if live:
        notes.append(
            f"{len(live)} Agent-Worktree(s) offen unter .claude/worktrees/ — nach dem "
            "ff-Merge aufraeumen (`git worktree remove`, dann `git worktree prune`):\n  "
            + "\n  ".join(live)
        )
    _, branches = git(root, "branch", "--list", "worktree-agent-*", "--format=%(refname:short)")
    orphan = [b.strip() for b in branches.splitlines() if b.strip() and b.strip() not in in_use]
    if orphan:
        notes.append("Verwaiste worktree-agent-*-Branches ohne Worktree: " + ", ".join(orphan))

    # (3) Unpushed / nicht integrierte Commits ----------------------------------
    note = integration_note(root, branch, ref, is_upstream)
    if note:
        notes.append(note)

    if notes:
        print(json.dumps({"systemMessage": "Sitzungsende — Hinweise:\n- " + "\n- ".join(notes)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # fail-open
        sys.exit(0)
