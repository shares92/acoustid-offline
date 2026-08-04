#!/usr/bin/env python3
"""PreToolUse-Wache: projektspezifische Sperrzonen (Bash, Edit, Write).

Drei Zonen — jede belegt in der Repo-Doku:

a) **Migrations-Drift-Sperre** — angewendete Migrations-SQL unter
   ``shared/shared/db/sql/{core,indexes}/`` nie editieren
   (PROGRESS „Fallstricke", DECISIONS E8, ARCHITECTURE §5.2:
   Checksummen-Drift-Erkennung). Neue Dateien anlegen bleibt erlaubt.
b) **Fixtures nie committen** — ``tests/fixtures/acoustid-dumps/*.jsonl.gz``
   (DECISIONS 2026-07-25 „Echte Dump-Fixtures nicht im oeffentlichen Repo").
c) **pg_acoustid nie veroeffentlichen** — das Test-Image
   ``acoustid-offline-pg-acoustid`` traegt fremden Code ohne Lizenztext
   (DECISIONS 2026-07-25 „Rescoring per Python-Nachbau …").

Global bereits gesperrt und deshalb hier NICHT dupliziert:
``docker compose down -v`` und ``rsync --delete``.

Fail-open: alles Unerwartete => Exit 0 (kein Urteil).
"""

from __future__ import annotations

import contextlib
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    FIXTURE_DUMPS,
    MIGRATION_SQL,
    as_posix,
    deny,
    read_event,
)

# Zerlegt eine Bash-Zeile in einzelne Kommandos (grob, aber ausreichend).
SPLIT = re.compile(r"&&|\|\||;|\n|\|")

DUMP_TOKEN = re.compile(r"acoustid-dumps|\.jsonl\.gz")
PG_ACOUSTID = re.compile(r"pg[-_]acoustid")
MUTATORS = {"rm", "mv", "cp", "tee", "truncate", "patch", "ed", "shred", "install"}


def tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except Exception:
        return segment.split()


def strip_messages(toks: list[str]) -> list[str]:
    """Entfernt Commit-Nachrichten, damit ein Text ueber Fixtures nicht sperrt."""
    out: list[str] = []
    skip = False
    for tok in toks:
        if skip:
            skip = False
            continue
        if tok in ("-m", "--message", "-F", "--file"):
            skip = True
            continue
        if tok.startswith(("--message=", "-m=")):
            continue
        out.append(tok)
    return out


def check_bash(command: str) -> None:
    for segment in SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        toks = tokens(segment)
        if not toks:
            continue
        head = Path(toks[0]).name
        rest = toks[1:]

        # --- (b) Fixtures nie in den Index / ins Repo -----------------------
        if head == "git" and rest:
            sub = rest[0]
            args = strip_messages(rest[1:]) if sub == "commit" else rest[1:]
            joined = " ".join(args)
            if sub in ("add", "commit", "stage", "restore", "rm") and DUMP_TOKEN.search(joined):
                deny(
                    "Gesperrt: Dump-Fixtures (tests/fixtures/acoustid-dumps/*.jsonl.gz) "
                    "gehoeren nie ins oeffentliche Repo — Betreiber-Entscheid "
                    "DECISIONS 2026-07-25 ('Echte Dump-Fixtures nicht im oeffentlichen "
                    "Repo'), sie sind bewusst in .gitignore. Beschaffung stattdessen ueber "
                    "tests/fixtures/fetch_fixtures.py."
                )
            if sub == "add" and any(a in ("-f", "--force") for a in args):
                deny(
                    "Gesperrt: `git add -f` umgeht .gitignore. Genau dort liegen die "
                    "Dump-Fixtures (DECISIONS 2026-07-25) und .claude/worktrees/ — beides "
                    "darf nie ins oeffentliche Repo. Datei einzeln und ohne -f hinzufuegen; "
                    "wenn sie wirklich gehoeren soll, erst .gitignore aendern."
                )

        # --- (c) pg_acoustid-Image nie veroeffentlichen ---------------------
        if head in ("docker", "podman", "nerdctl") and PG_ACOUSTID.search(segment):
            pushes = "push" in rest or ("--push" in rest) or "manifest" in rest[:1]
            if pushes:
                deny(
                    "Gesperrt: Das Image acoustid-offline-pg-acoustid wird nie "
                    "veroeffentlicht — pg_acoustid hat keinen Lizenztext, "
                    "Weiterverbreitung waere unlizenziert (DECISIONS 2026-07-25, "
                    "ci.yml-Vermerk, docs/api-lookup.md). Es ist ausschliesslich ein "
                    "lokales/CI-Test-Image (`docker build -t ...:test tests/pg_acoustid`)."
                )
        if head == "skopeo" and PG_ACOUSTID.search(segment):
            deny(
                "Gesperrt: pg_acoustid-Image nicht kopieren/veroeffentlichen "
                "(kein Lizenztext, DECISIONS 2026-07-25)."
            )

        # --- (a) Migrations-Drift-Sperre ------------------------------------
        if "shared/shared/db/sql/" in as_posix(segment):
            writes = (
                ">" in segment
                or head in MUTATORS
                or (head in ("sed", "perl", "ruby") and any(a.startswith("-i") for a in rest))
                or (head == "git" and rest[:1] in (["checkout"], ["restore"], ["apply"]))
            )
            if writes:
                deny(
                    "Gesperrt: Angewendete Migrations-SQL unter shared/shared/db/sql/ "
                    "wird nie veraendert (Drift-Sperre — der Runner prueft Checksummen, "
                    "ARCHITECTURE §5.2 / DECISIONS E8; PROGRESS 'Fallstricke'). "
                    "Schema-Aenderungen ausschliesslich als NEUE Migrationsdatei."
                )


def check_file(tool: str, file_path: str) -> None:
    path = as_posix(file_path)
    if MIGRATION_SQL.search(path):
        exists = Path(file_path).exists()
        if tool == "Edit" or (tool in ("Write", "NotebookEdit") and exists):
            deny(
                "Gesperrt: Angewendete Migrations-SQL wird nie editiert (Drift-Sperre — "
                "der Migrations-Runner haelt Checksummen, ARCHITECTURE §5.2 / "
                "DECISIONS E8; PROGRESS 'Fallstricke'). Schema-Aenderungen kommen "
                "ausschliesslich als NEUE Migrationsdatei in dieselbe Gruppe "
                "(core/ bzw. indexes/) — dann greift diese Sperre nicht."
            )
    if FIXTURE_DUMPS.search(path) and path.endswith(".jsonl.gz"):
        deny(
            "Gesperrt: Dump-Fixtures werden nicht im Repo erzeugt/veraendert — sie "
            "kommen reproduzierbar aus tests/fixtures/fetch_fixtures.py und bleiben "
            "ungetrackt (DECISIONS 2026-07-25)."
        )


def main() -> None:
    event = read_event()
    tool = event.get("tool_name") or ""
    data = event.get("tool_input") or {}
    if tool == "Bash":
        check_bash(str(data.get("command") or ""))
    elif tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        check_file(tool, str(data.get("file_path") or data.get("notebook_path") or ""))


if __name__ == "__main__":
    # fail-open: eine kaputte Wache blockiert nie
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
