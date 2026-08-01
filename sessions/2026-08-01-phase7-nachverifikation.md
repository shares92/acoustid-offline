# Session-Archiv: 2026-07-25 → 2026-08-01 — Projektstart bis Phase-7-Nachverifikation

Session-ID-Kontext: Start am 25.07.2026 (Steuerungsdateien + Phasen
0–6), Fortsetzung und Abschluss am 01.08.2026 (Phase-7-Nachverifikation
+ Session-Ende). Die Phasen 7–13 wurden NICHT in dieser Session gebaut,
sondern in parallelen Sessions zwischen dem 25.07. und 26.07.

## 1. Session-Review

### Angefordert
- 25.07.: Aus `docs/HANDOFF.md` die Steuerungsdateien erzeugen
  (ARCHITECTURE/PROGRESS/DECISIONS/LEARNINGS), Phasenplan schneiden,
  auf Freigabe warten; danach phasenweise Go-Erteilungen (Phase 0 bis
  Phase 6) mit jeweils vier Klärungs-/Entscheidungsrunden.
- 01.08.: „wach weiter mit phase 7" (Go für Phase 7), anschließend
  `/session-end`.

### Fertig (in dieser Session)
- Vier Steuerungsdateien erzeugt; 30-Phasen-Plan; beide Handoffs nach
  docs/ übernommen.
- **Phase 0–1 (Recherche):** 6 parallele Opus-Agenten (2 Wellen, ein
  Limit-Ausfall mit Resume); Ergebnisse: kein Voll-Snapshot → Replay
  414 GB, 7 Ströme, exaktes Schema, API-Vertrag inkl.
  Handoff-Korrektur `/v2/submission_status`, acoustid-index-Kennzahlen
  („muss in den RAM"), MB-Query-Schicht-Entwurf. 3 Berichte unter
  docs/research/.
- **Phase 2:** Repo-Grundgerüst, uv-Workspace Python 3.14, öffentliches
  GitHub-Repo shares92/acoustid-offline, CI (ein Fix: setup-uv v9 →
  v9.0.0 — Tag existierte nicht).
- **Phase 3:** Shared-Paket (Config alle §6-Schlüssel, Secrets,
  Env-Bootstrap, JSON-Logging, Enums); Funde: YAML-Sexagesimalfalle,
  LogRecord-Feldnamen.
- **Phase 4:** DB-Migrationen (eigener Runner, Gruppen core/indexes,
  lz4-in-core-Entscheid), postgres:18 (Volume-Layout-Falle),
  Integrationstests lokal + CI.
- **Phase 5:** Index-Client (msgpack, Digest-Pin, AOFF_INDEX_NAME),
  12 empirische API-Befunde als Addendum; Rosetta-Erfordernis auf
  Apple Silicon entdeckt.
- **Phase 6:** Downloader (Range-Resume, iter_raw-Bugfund) + Parser;
  **HOCH-Befund: COPY-Escaping epochenabhängig bis 2024-12-04** —
  korrigierte die eigene Phase-0-„Widerlegung"; 10. Fixture (2011).
- **01.08. Phase-7-Auftrag:** Bau-Agent erkannte, dass Phase 7 (und
  8–13) bereits existieren, und lieferte statt Doppelbau eine
  vollständige Nachverifikation: 1338 Tests grün gegen echte Container,
  Batchgrößen-Messreihe (Blockgröße wirkungslos, Default 1000 optimal),
  manueller Entescape-Durchstich bis in die DB (id=153238 korrekt).
  Fable-Doppelverifikation der 2011er-Fixture (85/85 per Unescape).
- **01.08. Ertrag dokumentiert:** LEARNINGS-Eintrag
  „Zähler-Semantik escaping_fallbacks" (Commit abd1225, gepusht, CI
  grün); DECISIONS-Prozessentscheid „Stand-Vorprüfung vor Bau-Agenten"
  (2026-08-01); Projektgedächtnis abgeglichen (Design-Abnahme,
  Vorfall + Konsequenz); Task-Chip für DB-Assertion erzeugt.

### Angefangen / offen
- **Go für Phase 14 nicht erteilt** — die Go-Frage (Phase 14 /
  Unraid-Probelauf / beides) wurde vom Betreiber am 01.08. verworfen
  („warten auf nächste Anweisung").
- **Task-Chip offen** (task_e5db0b72): DB-Assertion für die entescapte
  2011er-Meta-Zeile in den dbimport-Integrationstests.
- **Unraid-Probelauf** (Phase-8-DoD-Rest) wartet auf den Betreiber
  (docs/probelauf-unraid.md).

### Begonnen, aber stillschweigend fallengelassen
- Nichts Substanzielles. Einzelne kleine Zusagen wurden durch die
  Parallel-Sessions überholt statt fallengelassen (z. B. war der am
  25.07. dieser Session angekündigte „nächste Schritt Phase 7" durch
  fremde Sessions bereits erledigt). Der einzige bewusst offen
  gelassene Kleinpunkt aus der Nachverifikation (fehlende
  DB-Assertion) wurde als Task-Chip festgehalten, nicht verworfen.

## 2. Technischer Zustand (Session-Ende 01.08.2026)

- `git status`: Arbeitsbaum sauber (vor den /session-end-Doku-Edits);
  `git diff --stat`: leer. Alles committet und gepusht bis HEAD
  `abd1225`.
- Build-/CI-Status: **grün** — letzter CI-Lauf auf `abd1225` success;
  1349 Tests in drei Jobs (Lint+Unit, Integration PG 18 + Index-Image,
  Bit-Verifikation pg_acoustid).
- Neue TODO/FIXME-Marker aus dieser Session: **keine** (Code-Grep über
  shared/api/importer/watchdog: 0 Treffer).
- Uncommittete Änderungen NACH den /session-end-Edits: PROGRESS.md
  (Neuschrieb als Übergabedatei), DECISIONS.md (+1 Eintrag 2026-08-01),
  sessions/2026-08-01-phase7-nachverifikation.md (neu). Commit-Vorschlag
  siehe Session-Ende-Ausgabe; nicht ohne Bestätigung committet.

### Kennzahlen-Verlauf dieser Session
31 Tests (Phase 2) → 153 (P3) → 202 (P4) → 347 (P5) → 453 (P6);
Repo-Stand bei Session-Ende (inkl. Fremd-Sessions): 1349.

### Wichtigste dauerhafte Erkenntnisse dieser Session
(vollständig in LEARNINGS.md)
1. COPY-Escaping epochenabhängig — Stichproben müssen über die
   Zeitachse streuen.
2. Suchindex: nur Query-Extrakte, Index muss in den RAM (50 ms vs 50 s).
3. Doku/README ≠ Vertrag; auch offizieller Referenzcode kann defekt
   sein; Wertebereiche messen.
4. Stale-Kontext-Gefahr bei Multi-Session-Projekten → Stand-Vorprüfung
   (DECISIONS 2026-08-01).
