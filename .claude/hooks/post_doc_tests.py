#!/usr/bin/env python3
"""PostToolUse: doku-gekoppelte Tests nach Edits an ARCHITECTURE.md / .env.example.

Warum: Diese beiden Dateien sind **testgekoppelt**, nicht bloss Prosa.
``shared/tests/test_migrations.py`` liest den SQL-Block aus ARCHITECTURE §5.2
direkt ein und vergleicht ihn anweisungsgleich mit den Migrationen;
``importer/tests/test_streams.py`` liest die Stroeme-Tabelle aus §5.1;
``shared/tests/test_env.py`` haelt den AOFF_-Variablensatz (§6) und
``tests/test_repo_layout.py`` das Verzeichnis-Layout (§10).
Ein freihaendiger Doku-Edit bricht damit sofort die Tests — das soll man
in derselben Sekunde erfahren und nicht drei Phasen spaeter.

Rot => Exit 2 (stderr geht an Claude zurueck).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import project_root_for, read_event, run

WATCHED = {"ARCHITECTURE.md", ".env.example"}

TESTS = (
    "shared/tests/test_migrations.py",
    "importer/tests/test_streams.py",
    "shared/tests/test_env.py",
    "tests/test_repo_layout.py",
)


def main() -> int:
    event = read_event()
    if (event.get("tool_name") or "") not in ("Edit", "Write", "MultiEdit"):
        return 0
    data = event.get("tool_input") or {}
    raw = str(data.get("file_path") or "")
    if not raw:
        return 0
    path = Path(raw)
    if path.name not in WATCHED:
        return 0

    root = project_root_for(path)
    if root is None or path.resolve().parent != root:
        return 0  # nur die Fassungen in der Projektwurzel sind testgekoppelt

    # `--integration=off` (conftest.py): keine Compose-Dienste noetig, Laufzeit ~1 s.
    code, out = run(
        [
            "uv",
            "run",
            "--project",
            str(root),
            "pytest",
            *TESTS,
            "-q",
            "--integration=off",
        ],
        cwd=root,
        timeout=300,
    )
    if code == 0:
        print(f"doku-gekoppelte Tests gruen nach Edit an {path.name}")
        return 0

    tail = "\n".join(out.strip().splitlines()[-40:])
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
