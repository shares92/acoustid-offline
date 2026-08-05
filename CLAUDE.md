# CLAUDE.md — acoustid-offline / musicmeta-offline

Kurzregeln fuer Claude in diesem Repo. Alles Inhaltliche steht in
PROGRESS.md (Statuskopf, Arbeitsregeln, Fallstricke), ARCHITECTURE.md,
DECISIONS.md und LEARNINGS.md — hier wird nichts davon dupliziert.

## Session-Start (Pflicht)

`git log --oneline -5` + Statuskopf aus PROGRESS.md lesen, bevor irgendetwas
gebaut oder ein Bau-Agent beauftragt wird (PROGRESS „Arbeitsregeln"). Der
SessionStart-Hook legt beides zusammen mit `git status`, `git worktree list`
und dem Docker-Zustand automatisch vor. Das ist ein Auszug, **kein Ersatz
fuer die Pflichtlektuere**: ARCHITECTURE.md und PROGRESS.md werden trotzdem
vollstaendig gelesen (PROGRESS „Arbeitsregeln", Session-Start).
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
  kein Lizenztext, reines Test-Image (DECISIONS 2026-07-25). `docker
  tag`/`save` darauf fragen zurueck (Vorstufe einer Veroeffentlichung),
  reine Abfragen (`manifest inspect`, `skopeo inspect`) laufen durch.

Rueckfrage statt Sperre (ask) bei: `pytest --compose`/`--network` — der
Teardown faehrt `down -v` ueber die Volumes (bis 2 TB, E13) und `--network`
laedt echte Dumps; sowie bei Edits, die die Dump-Zeile aus `.gitignore`
nehmen.

Nicht per Hook, aber genauso bindend (PROGRESS „Fallstricke"): §5.1/§5.2
sind testgekoppelt, API-Bug-fuer-Bug-Paritaeten sind Absicht,
`compare2`/`extract_query`/**Chromaprint-Encoder** nur mit Bit-Verifikation
aendern (CI-Job `extension`). Doc-IDs fuer Submissions liegen im Bereich
`[2^31, 2^32-1]` (u32-Grenze, DECISIONS 2026-07-26). In v2 ist `/data` das
**Array** — Waechter-Daten gehoeren nach `/config` (R1); das PG-v1-Volume
mountet `/var/lib/postgresql` (Daten unter `18/docker`), Volume-Migration
nur nach dokumentiertem, geprobtem Rezept (R3).

**Restluecke der Hooks:** Die PostToolUse-Hooks sehen nur Edit/Write/
MultiEdit. Wer ueber Bash schreibt (`sed -i`, `>`, `git checkout`, Skripte),
loest weder ruff noch die doku-gekoppelten Tests aus — nach solchen
Schreibwegen `uv run ruff check .` und die betroffenen Tests von Hand
anstossen.

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
- **Serena-Symboltools NIE in Git-Worktrees:** Serena ist auf das
  Hauptverzeichnis indiziert — `replace_in_files` & Co. schreiben in
  den Haupt-Checkout statt in den Worktree (M2-Befund, LEARNINGS).
  Warnsignal: Dry-Run zeigt veralteten Inhalt. In Worktrees Skript-
  Edits + Diff-Review; dabei Absichts-Altnamen schuetzen
  (`REPORT_SCHEMA`, `acoustid-offline-pg-acoustid`, Uebergangs-
  Tabellen wie `LEGACY_KEYS`, Alt-Zitate in migration-v1-v2.md).
