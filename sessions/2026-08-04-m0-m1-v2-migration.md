# Session 2026-08-04/05 — v2-Migration: M0, M1a, M1b, Recherche-Gates, Probelauf

Orchestrator: Fable (Konto B). Bau/Analyse: Opus-Agenten (7 Stück:
4× M0-Analyse, 1× M1a, 1× M1b, 4× Recherche — M1a/M1b je mit
Nacharbeits-Runde). Zweit-Reviews: GPT 5.6 über Codex (blind), 3 Läufe
(M0-Synthese, M1a-Diff, M1b-Diff). Alle Merges ff nach Verifikation,
alle CI-Läufe beobachtet und grün.

## Ablauf

1. **Session-Start:** HANDOFF v2 („musicmeta-offline", 03.08.) lag
   unversioniert im Root — Ein-Container-Modell, Scope-Erweiterung
   Discogs/CAA/TheAudioDB, §16-Migrationsanhang mit Pflicht-M0.
   Betreiber-Go für die Impact-Analyse.
2. **M0** (`5a38d2f`): Vier parallele Analysen (Wächter;
   API/Importer/Shared; Infra/CI/Doku; Supervisor-Recherche),
   Fable-Synthese docs/research/m0-impact-analyse.md, Doppel-Review
   Opus (13) + GPT 5.6 (12 Findings) — alle verifiziert und
   eingearbeitet (u. a. E10 neu entschieden: Jobs als
   Wächter-Subprozesse; E12 als bewusste v2-Abweichung; fehlende
   Kante ready→error). Betreiber entschied E1–E16 wie empfohlen
   (AskUserQuestion: supervisord+tini / Index Cache+resident /
   amd64+Spike / Rest pauschal). Doku-Sweep: HANDOFF-Umzug (v1 →
   docs/archive/), DECISIONS (9 gekippte v1-Entscheide + E1–E16),
   PROGRESS-Phasenplan M1a–M9 inkl. M2.5, ARCHITECTURE-Kopfvermerk,
   LEARNINGS-Rubriken.
3. **M1a** (`51e3541…ff018d1`): Naht-Phase ohne Verhaltensänderung
   (control.py, Adressen in EnvSettings, FakeSupervisor,
   dbimport-Assertion = Task-Chip task_e5db0b72 erledigt).
   GPT-Review: 4 Findings gefixt (Compose-Durchreichung `AOFF_API_*`
   mit Leerstring-Ableitungs-Schalter; 3 supervisord-Treue-Fehler der
   Attrappe). 1701 Tests, E2E 6/6 doppelt.
4. **Parallel — Betreiber-Freigabe Tower** (`ssh Tower`, Memory
   `unraid-tower-zugang`): Unraid-Probelauf aufgesetzt (PG direkt auf
   /mnt/disk11, Index NVMe-Cache 6081, .env generiert). **Smoke-Lauf
   ok:** 13 Tage 2011, 17,3 GB gz, 2,94 h, 0 Lücken/Warnungen;
   Projektion: **Vollimport ~35,4 h, PG ~442 GB, Index ~48 GB**
   (Report: docs/research/probelauf/probelauf-smoke.json).
   Daten-Flaute-Check: Deltas wieder bis 2026-07-27 (Export ~8 Tage
   Verzug).
5. **Recherche-Gates M3–M7** (`a4c284e`, vier Opus-Agenten parallel):
   m3-discogs-dumps.md (42 empirische Befunde; Dumps ohne `<images>`
   seit 12/2024; kein Range-Resume, 429/1 h; CHECKSUM.txt-Marker;
   CC0 bestätigt; Bilder-ToS-Konflikt = Betreiber-Entscheid Ⓞ8),
   m4-caa-bezug.md (kein Bild-Bulk; direkte archive.org-URLs;
   30–50 % Schein-500er; 3,76 Mio. Cover ≈ 0,8 TB; 2/s ok),
   m5-mb-spiegel-befund.md (Empirie auf Tower: cover_art_archive +
   Discogs-URL-Relationships vorhanden/stündlich repliziert — R19
   entkräftet; Rolle acoustid_ro fehlt → GRANT-Skript; MB-Netz ohne
   Host-Port; filesize 100 % NULL), m6-theaudiodb.md (Cache laut ToS
   erlaubt; Free-Key = 1 Datensatz/Antwort → Empfehlung
   Single-Developer 8 €/Mon.; album-mb.php braucht Release-GROUP-MBID;
   JSON-Proxy allein macht Artwork nicht offline).
6. **Vorfall Tower:** Unraid-shfs (`/mnt/user`) stürzte nachts ab
   (Transport endpoint not connected; Syslog zeigte schon vorher
   Instabilität; keine Segfault-/OOM-Spur). Kein Datenverlust:
   Messlauf brach sicher ab (Datei=Transaktion). Betreiber startete
   das Array neu; Messlauf mit Direktpfad-Dumps (/mnt/cache/…, an
   FUSE vorbei) neu gestartet — Resume übersprang Eingespieltes.
7. **M1b** (`6c4a184`, 9 Commits): Ein-Container-Umbau komplett —
   Details im PROGRESS-Ergebniseintrag. Kernpunkte: supervisord 4.3.0
   läuft unter Py 3.14 (Erst-Prüfpunkt, doppelt verifiziert);
   `GroupStatus` statt bool; hartes pg_isready-Gate; Kante
   ready→error; E15-Politik; initdb-Entrypoint (App-Rolle ohne
   Superuser, Passwort nur als Datei); Migrationsrezept (ungeprobt).
   GPT-Review: **6 Findings, alle bestätigt und gefixt** — API lief
   als root (jetzt User `api` 6082 + setgid-/config, _FILE_MODE
   0640-Entscheid); drei Passwort-Leak-Wege (inkl. psql-cmdline);
   Gate-Lücke bei ALREADY_STARTED; observe() maskierte Teilzustände
   als Schlaf (ein Alt-Test hatte den Fehler festgeschrieben — schlug
   beim Fix fehl); E2E konnte eine echte Instanz ersetzen (jetzt
   eigener Projekt-Namespace, container_name entfernt);
   Rollback-Doku-Variablen. Verifikation doppelt (Agent + Fable):
   ruff, 1547 Unit, 199 Integration gegen das eigene Image, 8/8 E2E,
   12 Kontrakt-Tests gegen echtes supervisord.
8. **Parallel-Session** (Konto A) härtete die Repo-Hooks
   (`d4a0f30`, `f1336d7`): Pipe-Exit-Code-Wache (hat in dieser
   Session sofort einen unsauberen pytest-Aufruf von Fable gefangen),
   ask-Regeln für `pytest --compose/--network`. Merkregel: ask kann
   nur die Hauptsession beantworten → E2E beim Orchestrator fahren.

## Erkenntnisse/Zahlen

- Testbestand: 1649 (Session-Start) → **1754** (1547 Unit + 199
  Integration + 8 E2E); alle Gates doppelt gefahren.
- Cross-Vendor-Review hat sich dreifach bezahlt gemacht: 25 Findings
  M0, 5 M1a (davon 3 Attrappen-Treue), 6 M1b (davon 2 HOCH-Klassen,
  eine davon durch einen Alt-Test zementiert). LEARNINGS ergänzt:
  Attrappen sind Paritäts-Code; Merkeintrag ersetzt keinen
  Tripwire-Test; AF_UNIX-Pfadgrenze.
- Umgebungs-Eingriffe des M1b-Agenten (gemeldet): docker-buildx per
  Homebrew installiert (~/.docker/cli-plugins), ungenutzte
  Docker-Volumes/Builder-Caches der colima-VM aufgeräumt (VM war
  voll).
- Kontext-Verbrauch Orchestrator: ~50 % bei Session-Ende (1-M-Fenster).

## Übergabe

Operativ: PROGRESS.md-Statuskopf + Session-Übergabe (dort auch die
offenen Punkte 1–8, priorisiert). Nächster Schritt: Go M2; vorher
Messlauf-Report auswerten und Volume-Migrationsrezept auf Tower
proben.
