#!/usr/bin/env python3
"""PreToolUse-Wache: projektspezifische Sperrzonen (Bash, Edit, Write).

Vier Zonen — jede belegt in der Repo-Doku:

a) **Migrations-Drift-Sperre** — angewendete Migrations-SQL unter
   ``shared/shared/db/sql/{core,indexes}/`` nie editieren
   (PROGRESS „Fallstricke", DECISIONS E8, ARCHITECTURE §5.2:
   Checksummen-Drift-Erkennung). Neue Dateien anlegen bleibt erlaubt.
b) **Fixtures nie committen** — ``tests/fixtures/acoustid-dumps/*.jsonl.gz``
   (DECISIONS 2026-07-25 „Echte Dump-Fixtures nicht im oeffentlichen Repo")
   und die .gitignore-Zeile, die sie draussen haelt.
c) **pg_acoustid nie veroeffentlichen** — das Test-Image
   ``acoustid-offline-pg-acoustid`` traegt fremden Code ohne Lizenztext
   (DECISIONS 2026-07-25 „Rescoring per Python-Nachbau …").
d) **Teure Test-Schalter** — ``pytest --compose`` faehrt den echten Stack und
   raeumt im Teardown mit ``docker compose down -v`` auf; ``--network`` laedt
   echte Dumps. Beides nur nach Rueckfrage (ask), nie stillschweigend.

**Pfadlogik (a).** Die Zone wird pfad- und nicht stringbasiert geprueft:
Tokens eines Segments werden zu Pfaden aufgeloest — relativ zu einem
literalen ``cd`` in derselben Kette bzw. zu ``git -C``, sonst zum ``cwd`` des
Ereignisses — und bewertet wird ausschliesslich das **Schreibziel**
(Redirect-Ziel, letztes Argument von ``cp``/``mv``, Dateiargumente von
``sed -i`` usw.). Lesen (``cat … > /tmp/x``, ``diff a.sql b.sql``) bleibt
frei; eine NEUE Migrationsnummer anzulegen ebenfalls (Schreibziel existiert
noch nicht).

Global bereits gesperrt und deshalb hier NICHT dupliziert:
``docker compose down -v`` und ``rsync --delete``.

Fail-open: alles Unerwartete => Exit 0 (kein Urteil).
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (
    FIXTURE_DUMPS,
    MIGRATION_SQL,
    as_posix,
    ask,
    deny,
    read_event,
)

# Zerlegt eine Bash-Zeile in einzelne Kommandos (grob, aber ausreichend).
SPLIT = re.compile(r"&&|\|\||;|\n|\|")
#: Anfuehrungszeichen-Inhalte vor der Redirect-Suche ausblenden.
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
#: `>`/`>>`/`&>` samt Ziel; `2>` und `2>&1` liefern absichtlich kein Ziel.
REDIRECT = re.compile(r"(?P<fd>\d*)(?P<op>&?>>?)\s*(?P<target>[^\s;|&<>]+)")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

DUMP_TOKEN = re.compile(r"acoustid-dumps|\.jsonl\.gz")
PG_ACOUSTID = re.compile(r"pg[-_]acoustid")
#: Die .gitignore-Zeile, die die Dump-Fixtures draussen haelt (test_repo_layout).
GITIGNORE_DUMPS = "tests/fixtures/acoustid-dumps/*.jsonl.gz"
#: `ACOUSTID_COMPOSE_TESTS=1` / `ACOUSTID_NETWORK_TESTS=1` (conftest.py).
TEST_ENV_SWITCH = re.compile(r"ACOUSTID_(COMPOSE|NETWORK)_TESTS\s*=\s*[\"']?(1|true|yes)", re.I)
#: Kombinierte Kurzflags mit `f` (`-Af`, `-fA`, `-fd` …).
FORCE_FLAG = re.compile(r"^-[A-Za-z]*f[A-Za-z]*$")

WRAPPERS = {"sudo", "doas", "env", "xargs", "command", "nohup", "nice", "time", "stdbuf"}
WRAPPER_ARG_FLAGS = {
    "-u",
    "-g",
    "-I",
    "-n",
    "-P",
    "-L",
    "-a",
    "-d",
    "-s",
    "-S",
    "--user",
    "--group",
}

#: Kommandos, deren LETZTES Argument das Schreibziel ist.
LAST_ARG_WRITERS = {"cp", "mv", "install", "ln"}
#: Kommandos, die JEDES Dateiargument veraendern.
ALL_ARG_WRITERS = {"rm", "tee", "truncate", "shred", "patch", "ed", "dd"}
#: In-place-Editoren — `-i`, `-i.bak` und `--in-place`.
INPLACE_EDITORS = {"sed", "perl", "ruby", "gawk", "awk"}
#: git-Unterkommandos, die Dateien im Arbeitsbaum ueberschreiben.
GIT_WRITERS = {"checkout", "restore", "apply", "rm", "mv", "clean"}
#: Globale git-Optionen mit eigenem Argument (vor dem Unterkommando).
GIT_GLOBAL_ARG = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "--config-env",
}


def tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except Exception:
        return segment.split()


def strip_wrappers(toks: list[str]) -> list[str]:
    """Entfernt `sudo`/`env`/`xargs`/`command` und `VAR=wert`-Praefixe."""
    toks = list(toks)
    if toks:
        toks[0] = toks[0].lstrip("({")
    for _ in range(5):  # Endlosschleifen ausschliessen
        if not toks:
            return toks
        if ENV_ASSIGN.match(toks[0]):
            toks = toks[1:]
            continue
        if Path(toks[0]).name not in WRAPPERS:
            return toks
        rest = toks[1:]
        index = 0
        while index < len(rest) and (rest[index].startswith("-") or ENV_ASSIGN.match(rest[index])):
            index += 2 if rest[index] in WRAPPER_ARG_FLAGS else 1
        toks = rest[index:]
    return toks


def strip_messages(toks: list[str]) -> list[str]:
    """Entfernt Commit-Nachrichten, damit ein Text ueber Fixtures nicht sperrt.

    Kennt neben `-m`/`--message` auch die git-ueblichen Kurzformen: `-am`
    (Nachricht folgt als eigenes Token) und `-ma`/`-mtext` (Nachricht klebt
    am Flag).
    """
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
        if re.match(r"^-[A-Za-z]*m$", tok):  # -m, -am, -aim …: Nachricht folgt
            skip = True
            continue
        if re.match(r"^-[A-Za-z]*m.+$", tok) and not tok.startswith("--"):  # -ma, -mtext
            continue
        out.append(tok)
    return out


# --- Pfade ------------------------------------------------------------------


def join_path(base: str | None, token: str) -> str | None:
    """Loest ``token`` gegen ``base`` auf; `..` wird rein textlich gekuerzt."""
    if not token or token.startswith("-"):
        return None
    raw = as_posix(token)
    if "$" in raw or "`" in raw or raw.startswith("~"):
        return None
    if Path(raw).is_absolute():
        return os.path.normpath(raw)
    if not base:
        return None
    # normpath (nicht resolve): `..` rein textlich kuerzen, ohne das Dateisystem
    # zu befragen — `core/../core/x.sql` muss auch dann als Treffer gelten.
    return os.path.normpath(str(Path(base) / raw))


def cd_target(base: str | None, args: list[str]) -> str | None:
    """Neue Basis nach einem `cd`; alles Nicht-Literale => Basis unbekannt."""
    literal = [a for a in args if not a.startswith("-")]
    if not literal or literal[0] == "-":
        return None
    return join_path(base, literal[0])


def positionals(args: list[str], arg_flags: frozenset[str] = frozenset()) -> list[str]:
    """Dateiargumente eines Kommandos (Flags und deren Werte fallen raus)."""
    out: list[str] = []
    index = 0
    while index < len(args):
        tok = args[index]
        if tok == "--":
            out.extend(args[index + 1 :])
            break
        if tok.startswith("-") and tok != "-":
            index += 2 if tok in arg_flags else 1
            continue
        out.append(tok)
        index += 1
    return out


def redirect_targets(segment: str) -> list[str]:
    """Ziele von `>`/`>>`/`&>`; `2>`/`2>&1` bleiben aussen vor (kein Schreiben)."""
    scan = QUOTED.sub(" ", segment)
    out = []
    for match in REDIRECT.finditer(scan):
        if match.group("fd") == "2":
            continue
        target = match.group("target")
        if target:
            out.append(target)
    return out


def normalize_git(rest: list[str], base: str | None) -> tuple[str | None, str, list[str]]:
    """(Basis nach `-C`, Unterkommando, restliche Argumente)."""
    index = 0
    while index < len(rest):
        tok = rest[index]
        if tok in GIT_GLOBAL_ARG:
            if tok == "-C" and index + 1 < len(rest):
                base = join_path(base, rest[index + 1]) or base
            index += 2
            continue
        if tok.startswith("-"):
            index += 1
            continue
        break
    sub = rest[index] if index < len(rest) else ""
    return base, sub, rest[index + 1 :]


def write_targets(
    head: str, rest: list[str], segment: str, base: str | None
) -> tuple[str | None, list[str]]:
    """(wirksame Basis, Tokens des Segments, die tatsaechlich BESCHRIEBEN werden)."""
    out: list[str] = list(redirect_targets(segment))
    if head in LAST_ARG_WRITERS:
        args = positionals(rest, frozenset({"-t", "--target-directory", "-S", "--suffix"}))
        if args:
            out.append(args[-1])
    elif head in ALL_ARG_WRITERS:
        out.extend(positionals(rest, frozenset({"-s", "--size", "-i", "--input", "-p"})))
    elif head in INPLACE_EDITORS and any(
        a == "--in-place" or a.startswith(("-i", "--in-place=")) for a in rest
    ):
        out.extend(positionals(rest, frozenset({"-e", "-f", "--expression", "--file"})))
    elif head == "git":
        base, sub, args = normalize_git(rest, base)
        if sub in GIT_WRITERS and not (
            # `restore --staged` (ohne --worktree) fasst nur den Index an.
            sub == "restore" and "--staged" in args and "--worktree" not in args
        ):
            flags = frozenset({"--source", "-b", "-B", "--pathspec-from-file"})
            out.extend(positionals(args, flags))
            if sub == "apply":
                # Die Ziele stehen IM Patch — deshalb hier alle Tokens pruefen.
                out.extend(args)
    return base, out


# --- Zonen ------------------------------------------------------------------


def check_test_switches(segment: str, toks: list[str]) -> None:
    """(d) `pytest --compose` / `--network` nie ohne Rueckfrage."""
    if not any(Path(tok).name == "pytest" for tok in toks):
        return
    hits = [flag for flag in ("--compose", "--network") if flag in toks]
    if TEST_ENV_SWITCH.search(segment):
        hits.append("ACOUSTID_*_TESTS=1")
    if not hits:
        return
    ask(
        f"Rueckfrage ({', '.join(hits)}): `--compose` faehrt den ECHTEN Stack hoch und "
        "raeumt im Teardown mit `docker compose down -v` auf — das trifft die Volumes "
        "db-data/index-data/dump-data/watchdog-data (tests/test_wake_e2e.py: 'stoppt und "
        "entfernt am Ende alles, was so heisst, inklusive der Volumes'). Bei einer echten "
        "Instanz haengen daran bis zu 2 TB (PROGRESS 'Fallstricke', DECISIONS E13 — genau "
        "deshalb faehrt die Release-Compose Bind-Mounts). `--network` laedt ausserdem "
        "echte Dumps von data.acoustid.org. Nur bestaetigen, wenn hier nachweislich keine "
        "echte Instanz laeuft."
    )


def check_fixtures(head: str, rest: list[str], base: str | None) -> None:
    """(b) Dump-Fixtures nie in den Index / ins Repo."""
    if head != "git" or not rest:
        return
    _, sub, args = normalize_git(rest, base)
    if sub == "commit":
        args = strip_messages(args)
    joined = " ".join(args)
    repair = (sub == "rm" and "--cached" in args) or (
        sub == "restore" and "--staged" in args and "--worktree" not in args
    )
    if (
        sub in ("add", "commit", "stage", "restore", "rm")
        and DUMP_TOKEN.search(joined)
        and not repair
    ):
        deny(
            "Gesperrt: Dump-Fixtures (tests/fixtures/acoustid-dumps/*.jsonl.gz) "
            "gehoeren nie ins oeffentliche Repo — Betreiber-Entscheid "
            "DECISIONS 2026-07-25 ('Echte Dump-Fixtures nicht im oeffentlichen "
            "Repo'), sie sind bewusst in .gitignore. Beschaffung stattdessen ueber "
            "tests/fixtures/fetch_fixtures.py. Zum Zuruecknehmen eines Versehens sind "
            "`git rm --cached` und `git restore --staged` erlaubt."
        )
    if sub == "add" and any(FORCE_FLAG.match(a) or a == "--force" for a in args):
        deny(
            "Gesperrt: `git add -f` umgeht .gitignore. Genau dort liegen die "
            "Dump-Fixtures (DECISIONS 2026-07-25) und .claude/worktrees/ — beides "
            "darf nie ins oeffentliche Repo. Datei einzeln und ohne -f hinzufuegen; "
            "wenn sie wirklich gehoeren soll, erst .gitignore aendern."
        )


def check_pg_acoustid(head: str, rest: list[str], segment: str) -> None:
    """(c) Das pg_acoustid-Image nie veroeffentlichen."""
    if not PG_ACOUSTID.search(segment):
        return
    if head in ("docker", "podman", "nerdctl"):
        if "push" in rest or "--push" in rest:
            deny(
                "Gesperrt: Das Image acoustid-offline-pg-acoustid wird nie "
                "veroeffentlicht — pg_acoustid hat keinen Lizenztext, "
                "Weiterverbreitung waere unlizenziert (DECISIONS 2026-07-25, "
                "ci.yml-Vermerk, docs/api-lookup.md). Es ist ausschliesslich ein "
                "lokales/CI-Test-Image (`docker build -t ...:test tests/pg_acoustid`)."
            )
        if rest[:1] in (["tag"], ["save"]):
            ask(
                f"Rueckfrage: `docker {rest[0]}` auf ein pg_acoustid-Image. Umbenennen "
                "oder als Tarball ausleiten ist der uebliche erste Schritt einer "
                "Veroeffentlichung — und die ist gesperrt (kein Lizenztext, "
                "DECISIONS 2026-07-25). Nur bestaetigen, wenn das Ergebnis lokal/CI "
                "bleibt; ein anschliessendes `push` bleibt in jedem Fall verboten."
            )
    if head == "skopeo" and rest[:1] not in (["inspect"], ["list-tags"]):
        deny(
            "Gesperrt: pg_acoustid-Image nicht kopieren/veroeffentlichen "
            "(kein Lizenztext, DECISIONS 2026-07-25). Reine Abfragen "
            "(`skopeo inspect`, `docker manifest inspect`) sind erlaubt."
        )


def check_migrations(head: str, rest: list[str], segment: str, base: str | None) -> None:
    """(a) Angewendete Migrations-SQL nie ueberschreiben."""
    base, targets = write_targets(head, rest, segment, base)
    for target in targets:
        resolved = join_path(base, target)
        if resolved is None:
            hit = bool(MIGRATION_SQL.search(as_posix(os.path.normpath(as_posix(target)))))
        else:
            hit = bool(MIGRATION_SQL.search(as_posix(resolved))) and Path(resolved).exists()
        if hit:
            deny(
                "Gesperrt: Angewendete Migrations-SQL unter shared/shared/db/sql/ "
                "wird nie veraendert (Drift-Sperre — der Runner prueft Checksummen, "
                "ARCHITECTURE §5.2 / DECISIONS E8; PROGRESS 'Fallstricke'). "
                "Schema-Aenderungen ausschliesslich als NEUE Migrationsdatei. "
                f"Erkanntes Schreibziel: {target}"
            )


def check_bash(command: str, cwd: str | None) -> None:
    base = cwd
    for segment in SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        toks = strip_wrappers(tokens(segment))
        if not toks:
            continue
        head = Path(toks[0]).name
        rest = toks[1:]

        if head == "cd":
            base = cd_target(base, rest)
            continue

        check_test_switches(segment, toks)
        check_fixtures(head, rest, base)
        check_pg_acoustid(head, rest, segment)
        check_migrations(head, rest, segment, base)


# --- Werkzeuge mit Dateipfad ------------------------------------------------


def removes_dump_line(data: dict, path: Path) -> bool:
    """Nimmt der Edit die .gitignore-Zeile heraus, die die Fixtures aussperrt?"""
    edits = data.get("edits")
    if isinstance(edits, list):
        return any(
            GITIGNORE_DUMPS in str(edit.get("old_string") or "")
            and GITIGNORE_DUMPS not in str(edit.get("new_string") or "")
            for edit in edits
            if isinstance(edit, dict)
        )
    if data.get("old_string") is not None:
        return GITIGNORE_DUMPS in str(data.get("old_string") or "") and GITIGNORE_DUMPS not in str(
            data.get("new_string") or ""
        )
    if data.get("content") is not None and path.is_file():
        old = path.read_text(encoding="utf-8", errors="replace")
        return GITIGNORE_DUMPS in old and GITIGNORE_DUMPS not in str(data.get("content") or "")
    return False


def check_file(tool: str, file_path: str, data: dict) -> None:
    path = as_posix(file_path)
    if MIGRATION_SQL.search(path):
        exists = Path(file_path).exists()
        if tool in ("Edit", "MultiEdit") or (tool in ("Write", "NotebookEdit") and exists):
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
    if Path(path).name == ".gitignore" and removes_dump_line(data, Path(file_path)):
        ask(
            f"Rueckfrage: Dieser Edit nimmt `{GITIGNORE_DUMPS}` aus .gitignore heraus — "
            "genau die Zeile, die die echten Dump-Fixtures aus dem oeffentlichen Repo "
            "haelt (DECISIONS 2026-07-25). tests/test_repo_layout.py prueft sie, der "
            "Test wird danach rot. Nur bestaetigen, wenn der Betreiber den Entscheid "
            "wirklich zurueckgenommen hat."
        )


def main() -> None:
    event = read_event()
    tool = event.get("tool_name") or ""
    data = event.get("tool_input") or {}
    if tool == "Bash":
        cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
        check_bash(str(data.get("command") or ""), cwd)
    elif tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        check_file(tool, str(data.get("file_path") or data.get("notebook_path") or ""), data)


if __name__ == "__main__":
    # fail-open: eine kaputte Wache blockiert nie
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
