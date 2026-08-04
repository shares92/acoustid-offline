#!/usr/bin/env python3
"""PostToolUse: doku-gekoppelte Tests nach Edits an ARCHITECTURE.md / .env.example.

Warum: Diese Dateien sind **testgekoppelt**, nicht bloss Prosa.
``shared/tests/test_migrations.py`` liest den SQL-Block aus ARCHITECTURE §5.2
direkt ein und vergleicht ihn anweisungsgleich mit den Migrationen;
``importer/tests/test_streams.py`` liest die Stroeme-Tabelle aus §5.1;
``shared/tests/test_env.py`` haelt den AOFF_-Variablensatz (§6) und
``tests/test_repo_layout.py`` das Verzeichnis-Layout (§10) — derselbe Test
prueft auch die .gitignore-Zeile, die die Dump-Fixtures aussperrt, weshalb
``.gitignore`` hier mit haengt (Laufzeit ~0,4 s).
Ein freihaendiger Doku-Edit bricht damit sofort die Tests — das soll man
in derselben Sekunde erfahren und nicht drei Phasen spaeter.

Rot => Exit 2 (stderr geht an Claude zurueck). **Nur echte Testfehler**
(pytest-Exit 1) gelten als rot; ein kaputtes venv oder ein nicht startbares
uv/pytest ist Infrastruktur und darf die Arbeit nicht blockieren
(Warnung + Exit 0).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import project_root_for, read_event, run, tool_broken, within_project

DOC_TESTS = (
    "shared/tests/test_migrations.py",
    "importer/tests/test_streams.py",
    "shared/tests/test_env.py",
    "tests/test_repo_layout.py",
)

#: Welche Datei welche Tests zieht.
WATCHED: dict[str, tuple[str, ...]] = {
    "ARCHITECTURE.md": DOC_TESTS,
    ".env.example": DOC_TESTS,
    ".gitignore": ("tests/test_repo_layout.py",),
}

#: Unter dem aeusseren Hook-Timeout (300 s, settings.json).
STEP_TIMEOUT = 240


def main() -> int:
    event = read_event()
    if (event.get("tool_name") or "") not in ("Edit", "Write", "MultiEdit"):
        return 0
    data = event.get("tool_input") or {}
    raw = str(data.get("file_path") or "")
    if not raw:
        return 0
    path = Path(raw)
    tests = WATCHED.get(path.name)
    if not tests:
        return 0

    root = project_root_for(path)
    if root is None or path.resolve().parent != root:
        return 0  # nur die Fassungen in der Projektwurzel sind testgekoppelt
    if not within_project(root):
        return 0  # fremdes Nachbar-Repo: nie testen

    # `--integration=off` (conftest.py): keine Compose-Dienste noetig, Laufzeit ~1 s.
    code, out = run(
        [
            "uv",
            "run",
            "--project",
            str(root),
            "pytest",
            *tests,
            "-q",
            "--integration=off",
        ],
        cwd=root,
        timeout=STEP_TIMEOUT,
    )
    tail = "\n".join(out.strip().splitlines()[-40:])
    if code == 0:
        print(f"doku-gekoppelte Tests gruen nach Edit an {path.name}")
        return 0

    if code != 1 or tool_broken(code, out):
        # 127 (nicht startbar), 2/3 (Abbruch, Sammelfehler), 4 (Aufruffehler),
        # 5 (nichts gesammelt) — Infrastruktur, kein Testrot: fail-open.
        print(
            f"doku-gekoppelte Tests konnten nach Edit an {path.name} nicht laufen "
            f"(pytest-Exit {code}) — Befund unbekannt, bitte selbst pruefen:\n{tail[-1500:]}"
        )
        return 0

    print(
        f"ROT: {path.name} ist testgekoppelt (ARCHITECTURE §5.1/§5.2/§6/§10) und die "
        f"gekoppelten Tests schlagen jetzt fehl. Doku-Edit zurueckdrehen oder Code und "
        f"Doku gemeinsam nachziehen — §5.1/§5.2 gelten laut PROGRESS als unantastbar.\n\n"
        f"{tail}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # fail-open
        sys.exit(0)
