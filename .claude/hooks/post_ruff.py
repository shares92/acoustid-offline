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
Claude zurueck — **auch ein Syntaxfehler**: ``ruff format`` bricht dann mit
rc 2 ab, und das ist ein Befund, kein Werkzeugausfall. Still bleibt der Hook
nur, wenn ruff/uv gar nicht erst startet.

Der abgeleitete Wurzelpfad muss im eigenen Projekt liegen (``within_project``)
— nebenan liegt „AcoustID Instanz GPT", dort haette der Hook sonst fremd
formatiert und ``uv sync`` angestossen.

Zeitbudget: drei innere Aufrufe à 55 s bleiben unter dem Hook-Timeout (200 s)
in settings.json — sonst schlaegt der aeussere Abbruch zu, bevor der innere
greift.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import project_root_for, read_event, run, tool_broken, within_project

#: Summe unter dem aeusseren Timeout (200 s, settings.json).
STEP_TIMEOUT = 55


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
    if not within_project(root):
        return 0  # fremdes Nachbar-Repo: nie anfassen

    base = ["uv", "run", "--project", str(root), "ruff"]
    fmt_code, fmt_out = run(
        [*base, "format", "--force-exclude", str(path)], cwd=root, timeout=STEP_TIMEOUT
    )
    before = path.read_bytes()
    chk_code, chk_out = run(
        [*base, "check", "--fix", "--force-exclude", str(path)], cwd=root, timeout=STEP_TIMEOUT
    )
    # `--fix` kann Zeilen entfernen (z. B. unbenutzte Importe) und dabei die
    # Formatierung wieder aufreissen -> in dem Fall noch einmal formatieren.
    if path.is_file() and path.read_bytes() != before:
        run([*base, "format", "--force-exclude", str(path)], cwd=root, timeout=STEP_TIMEOUT)

    if tool_broken(fmt_code, fmt_out) or tool_broken(chk_code, chk_out):
        # Werkzeug selbst kaputt/nicht da -> melden, aber nicht blockieren.
        print(f"ruff-Hook uebersprungen ({root.name}): {(fmt_out or chk_out).strip()[:300]}")
        return 0

    if fmt_code not in (0, 1):
        # rc 2 = `ruff format` kann die Datei nicht parsen: Syntaxfehler.
        print(
            f"Syntaxfehler in {path.name} — `ruff format` bricht ab (Wurzel: {root}). "
            "Der Edit ist so nicht lauffaehig:\n"
            f"{(chk_out or fmt_out).strip()[-2000:]}",
            file=sys.stderr,
        )
        return 2

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
