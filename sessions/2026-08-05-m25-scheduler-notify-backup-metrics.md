# Session 2026-08-05 (abend) — M2.5: Scheduler, Notify, Backup, Metrics

Orchestrierung: Fable (Koordination, Verifikation, Gates, Merge, Doku);
Bau/Review/Verifikation: Opus-Agenten in einem Worktree; Zweitreview:
GPT 5.6 blind via Codex CLI.

## 1. Session-Review

**Angefordert (Betreiber-Entscheide dieser Session):**
1. `/session-start` → Go **M2.5** (Messlauf lief zu dem Zeitpunkt
   noch auf Tower, blockierte nichts).
2. Design-Entscheid Submit ↔ Update-Lauf: **Zurückstellen** (Guard
   bleibt; verworfen: Feed ohne Guard, Abbruch+Retry).
3. Nach der Verifikation: **alle** bestätigten Findings fixen (nicht
   nur HOCH/MITTEL); Docker-Freigabe für die Gates erteilt.
4. **Kaltstart-Sperre bestätigt** (unbegrenzter `update`-Lauf auf
   leerer Historie → Fehler mit Bootstrap-Verweis; verworfen: nur
   Wächter-Pfad, nur Warnung).
5. Zwischendurch Pause/Stopp/Weiter der Agenten auf Zuruf; am Ende:
   **Session beenden**, M3-Go-Frage in frischer Session.

**Fertig (17 Commits auf main, `4952901..0dd9fab` + Doku-Sweep
`725b487`):**
- **Bau (9 Commits, ein Opus-Agent):** Scheduler + Jobs als
  Wächter-Subprozesse (E10), `update_run`-Migration (6 Job-Arten),
  Notify (ntfy/Webhook + SMTP, 5 Ereignisse), Backup-Job +
  docs/backup-restore.md, `GET /metrics`, Log-Rotation, Disk-Guard
  je Schreibpfad (E11), Submit-Zurückstellung (Marke
  `/config/index-feed.busy`), Compose-Zyklus-E2E (5 Tests).
- **Doppel-Review blind + adversariale Verifikation:** Opus 7 +
  GPT 15 Findings → 6 Konsens (HOCH: ewig offene `update_run`-Zeilen
  blockieren Idle-Stopp; `stopwaitsecs=30` vs. 900-s-Frist;
  Cache-Invalidierung vor dem Nachtrag). 10 einseitige durch
  3 Opus-Verifizierer + Fable (HOCH): 7 bestätigt, 2 widerlegt
  (kind-Umschrieb, Migrationskonvention), 1 herabgestuft
  (Marken-TOCTOU). 3 Nebenbefunde (Hand-Läufe ohne Marke;
  IdleStopper-Kopplung — Stack schlief nach Jobs nie ein;
  ungeschützte mkdir/unlink). 5 Fix-Richtungen als schädlich
  widerlegt und NICHT gebaut.
- **Nacharbeit (4 Commits):** try/finally + Startup-Rekonziliation,
  Fristen-Kette 360/300/240 s (testgekoppelt), Invalidierung nach
  `queue-send`, `_catch_up`-Schleife, gave-up nur bei Neuzugang,
  `.part`-Räumung, Marken-TTL 24 h, `_sleep_again` dreifach
  repariert (`ActivityTracker.defer()`), Nachlauf auch nach
  fehlgeschlagenem Lauf, `/index` in den Guard, zustandsgetriebene
  Readiness-Meldung, Feed-Retry bei Version-Mismatch.
- **Gate-Fix-Runden (4 Commits):** Integration — 3 Testfehler
  (CLI-Zugänge im Test-Env, Doc-ID-Bereich, DROP-DATABASE-Flake).
  E2E — Termin lag vor dem ersten Lauf (Fälligkeits-Regel jetzt
  dokumentiert: auch `aborted` verbraucht den Termin); dann der
  Kernfund: **Compose-Tests haben Netz**, der Retry-Test lud real
  echte Deltas ab 2011 → **Kaltstart-Sperre im Produkt**
  (`ColdStartError`, `usage_error` Exit 2) + Quelle im Test per
  `/etc/hosts` abgeklemmt + `defined_state`-Fixture.
- **Gates doppelt:** Unit 1805, Integration 210 (eigenes Image),
  E2E 13/13 (7:05), ruff; ff-Merge `0dd9fab` (+9430/−127),
  CI 2× grün; Doku-Sweep `725b487`; Test-Stack/Worktree/Branch
  abgeräumt; Memory aktualisiert.

**Nebenbefund Tower:** Messlauf um 12:58 UTC abgebrochen
(`import_failed` Exit 6 nach 383/3386 Dateien, 19,6 GB, 8,1 h) — die
Tower-PG beendete die Verbindung („terminated abnormally";
shfs-Vorgeschichte vom Morgen). Resume via `import_state` intakt,
nichts angefasst. → Offener Punkt 1 in PROGRESS.

## 2. Learnings der Session (Prozess)

- **Compose-Container haben Netz** — der pytest-Marker `network`
  wählt Tests ab, nimmt aber keinem Container die Route. Tests, die
  „ohne Quelle" arbeiten wollen, müssen sie im Container abklemmen
  (LEARNINGS-Eintrag der Agenten; Fehlerklasse jetzt als Test).
- **Adversariale Verifikation lohnt weiter messbar:** 2 von 15
  GPT-Findings widerlegt, 1 herabgestuft; 5 plausible
  Fix-Vorschläge hätten neue Fehler eingebaut (u. a. blindes
  Marken-Löschen → Waisen-Importer-Fenster; Report-Pflicht in
  `JobOutcome.ok` → falsche Fehlalarme).
- **Hintergrund-Agenten überleben eine Session-Rotation nicht**
  (Transcript weg) — Aufträge self-contained halten, Worktree-Pfad +
  Branch explizit nennen; die Fortsetzung durch einen frischen
  Agenten im selben Worktree funktionierte reibungslos.
- **Ein Agent, sequenzielle Nachrichten** (Bau → Nacharbeit →
  Gate-Fixes an denselben Agenten) hielt den Kontext beisammen; erst
  die Rotation erzwang den Wechsel.

## 3. Übergabe

Stand und nächste Schritte stehen in PROGRESS.md (Statuskopf +
Session-Übergabe „2026-08-05, M2.5 komplett"): zuerst
Messlauf-Abbruch auf Tower klären (Punkt 1), dann Go-Frage **M3**
(Discogs-Spiegel, beginnt mit dem Recherche-Gate §14.2).
