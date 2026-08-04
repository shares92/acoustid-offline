#!/usr/bin/env python3
"""PostToolUse: ruff format + ruff check --fix fuer geaenderte Python-Dateien.

Zwei Fallstricke, die dieser Hook bewusst umgeht:

1. **ruff liegt nur im venv** — es gibt kein globales ``ruff``. Deshalb immer
   ueber ``uv run`` (Dev-Gruppe der Workspace-Wurzel, pyproject.toml).
2. **Agenten arbeiten in ``.claude/worktrees/agent-*``** — die Projektwurzel
   wird deshalb aus dem DATEI-Pfad abgeleitet (naechster Vorfahre mit
   uv.lock + pyproject.toml). Eine feste Wurzel wuerde im falschen Baum
   formatieren.

``--force-exclude`` sorgt dafuer, dass die ruff-Ausschluesse aus pyproject.toml
(z. B. tests/fixtures/acoustid-dumps) auch bei explizit uebergebener Datei
gelten. Rest-Befunde, die ``--fix`` nicht loesen kann, gehen per Exit 2 an
Claude zurueck.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import project_root_for, read_event, run


def main() -> int:
    event = read_event()
    if (event.get("tool_name") or "") not in ("Edit", "Write", "MultiEdit"):
        return 0
    data = event.get("tool_input") or {}
    raw = str(data.get("file_path") or "")
    if not raw.endswith(".py"):
        return 0
    path = Path(raw)
    if not path.is_file():
        return 0

    root = project_root_for(path)
    if root is None:
        return 0  # ausserhalb eines uv-Workspace: nichts tun

    base = ["uv", "run", "--project", str(root), "ruff"]
    fmt_code, fmt_out = run([*base, "format", "--force-exclude", str(path)], cwd=root, timeout=180)
    before = path.read_bytes()
    chk_code, chk_out = run(
        [*base, "check", "--fix", "--force-exclude", str(path)], cwd=root, timeout=180
    )
    # `--fix` kann Zeilen entfernen (z. B. unbenutzte Importe) und dabei die
    # Formatierung wieder aufreissen -> in dem Fall noch einmal formatieren.
    if path.is_file() and path.read_bytes() != before:
        run([*base, "format", "--force-exclude", str(path)], cwd=root, timeout=180)

    if fmt_code not in (0, 1) or chk_code == 127:
        # Werkzeug selbst kaputt/nicht da -> melden, aber nicht blockieren.
        print(f"ruff-Hook uebersprungen ({root.name}): {(fmt_out or chk_out).strip()[:300]}")
        return 0

    if chk_code != 0:
        print(
            "ruff meldet nach --fix verbleibende Befunde in "
            f"{path.name} (Wurzel: {root}):\n{chk_out.strip()[-2000:]}",
            file=sys.stderr,
        )
        return 2

    print(f"ruff format + check --fix ok: {path.name} (Wurzel: {root})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # fail-open
        sys.exit(0)
