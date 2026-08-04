# CLAUDE.md — acoustid-offline / musicmeta-offline

Kurzregeln fuer Claude in diesem Repo. Alles Inhaltliche steht in
PROGRESS.md (Statuskopf, Arbeitsregeln, Fallstricke), ARCHITECTURE.md,
DECISIONS.md und LEARNINGS.md — hier wird nichts davon dupliziert.

## Session-Start (Pflicht)

`git log --oneline -5` + Statuskopf aus PROGRESS.md lesen, bevor irgendetwas
gebaut oder ein Bau-Agent beauftragt wird (PROGRESS „Arbeitsregeln"). Der
SessionStart-Hook legt beides zusammen mit `git status`, `git worktree list`
und dem Docker-Zustand automatisch vor — trotzdem lesen, nicht ueberfliegen.
Implementierung einer Phase erst nach explizitem Go des Betreibers.

## Sperrzonen (Hooks sperren hart, mit Begruendung)

- **Angewendete Migrations-SQL** (`shared/shared/db/sql/{core,indexes}/`) nie
  editieren — Drift-Sperre, der Runner haelt Checksummen (ARCHITECTURE §5.2,
  DECISIONS E8). Schema-Aenderungen nur als **neue** Migrationsdatei.
- **Dump-Fixtures** (`tests/fixtures/acoustid-dumps/*.jsonl.gz`) nie
  committen/stagen — Lizenz-/Scope-Entscheid DECISIONS 2026-07-25;
  Beschaffung ueber `tests/fixtures/fetch_fixtures.py`. Auch `git add -f`
  ist gesperrt (umgeht .gitignore).
- **pg_acoustid-Image** (`acoustid-offline-pg-acoustid`) nie pushen —
  kein Lizenztext, reines Test-Image (DECISIONS 2026-07-25).

Nicht per Hook, aber genauso bindend (PROGRESS „Fallstricke"): §5.1/§5.2
sind testgekoppelt, API-Bug-fuer-Bug-Paritaeten sind Absicht,
`compare2`/`extract_query` nur mit Bit-Verifikation aendern.

## Fallen im Alltag

- **Namespace-/CWD-Falle:** „Skripte/Container nie mit CWD=Repo-Root starten
  (Namespace-Falle); psycopg in shared bewusst lazy." (`import shared`
  scheitert sonst — das Member-Verzeichnis gewinnt als Namespace-Paket gegen
  den Editable-Finder, LEARNINGS.) pytest ist die Ausnahme: der
  Root-`conftest.py` raeumt den Wurzelpfad wieder aus `sys.path`.
- **Docker tot / Integrationstests haengen:** `colima start --vz-rosetta` —
  acoustid-index ist amd64-only und haengt unter qemu still (LEARNINGS).
- **CI rot ohne Testlauf:** Docker-Hub-Registry-Timeouts beim „Initialize
  containers" sind Infrastruktur-Flakes → `gh run rerun --failed`.
  „cancelled" bei Push-auf-Push ist Abloesung, kein Bruch.
- **Tests ohne Dienste:** `uv run pytest … --integration=off`
  (ruff/pytest liegen nur im venv — immer `uv run`).
- **Massen-Renames** (M2: `AOFF_` → `MMO_`): Serena-Symboltools statt
  Volltext-Edits nutzen — Ersetzen ueber alle Pakete hinweg, ohne
  Doku-Zitate und Uebergangs-Aliase zu zerschiessen.
