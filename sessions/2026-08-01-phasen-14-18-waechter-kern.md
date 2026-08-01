# Session 2026-08-01 — Wächter-Kern: Phasen 14–18

Orchestrator-Session (Fable orchestriert, Opus-Bau-Agenten führen aus).
Fünf Phasen an einem Tag, jede nach demselben Muster: Go per
AskUserQuestion → Opus-Agent im Worktree (mit Stand-Vorprüfung) →
Orchestrator-Verifikation (Code-Review + Suite + ruff + E2E doppelt) →
ff-Merge → CI-Lauf beobachtet → Doku-Update → Pause mit Go-Frage.

## 1. Session-Review

**Angefordert:**
- `/session-start` (Einlesen, Abgleich, Go-Frage).
- Go für Phase 14, danach nacheinander Gos für 15, 16, 17, 18
  (jeweils per AskUserQuestion, Empfehlung angenommen).
- Healthcheck-Mitentscheid (Vormerkung aus Phase 9).

**Fertig (gebaut, verifiziert, gemergt, CI grün, dokumentiert):**
- **Healthcheck-Entscheid** (e4ec5d0): interner API-Endpunkt ja, Bau
  erst Phase 15 — Phase 14 blieb paket-disjunkt.
- **Phase 14** (7ce2cd5): Wächter-Grundgerüst `acoustid_watchdog` —
  SQLite-Zustandsdatenbank mit `PRAGMA user_version`-Migrationsläufer
  (api_key, admin_user, update_run, event_log mit exaktem
  5000er-Ringpuffer), `GET /status` baulich weckfrei, Erststart mit
  argon2-Passwort (Klartext nur einmalig ins Containerlog),
  Reload-Marke `config.yaml.reload` (Sendeseite),
  docker-compose.watchdog.yml. Agent verifizierte zusätzlich am
  echten Container. Danach voller 5-Phasen-Doku-Sweep 10–14 (ce3af57).
- **Phase 15** (3f9daee): Reverse-Proxy `/v2/*` (streamend, alles roh,
  405-Parität bleibt), DockerClient (3 Engine-API-Routen über httpx/
  uds, unversioniert, keine Fremdbibliothek), WakeCoordinator (ein
  Weckvorgang via Task+shield, Timeout → 503+Retry-After 30), interner
  API-Healthcheck `GET /_health` (DB+Index, bewusst ohne MB),
  Reload-Empfangsseite `api/app/reload.py` (10 s, konservative
  Teilmenge, Rest zurückgeschrieben + Warnung), E2E-Wecktest als
  opt-in-Marker `compose` (Weckdauer ~1,3 s lokal). Doku dbac96a.
- **Phase 16** (a42c192): Zustandsmaschine `ALLOWED_TRANSITIONS`
  (alle 25 Paare getestet; streng `to()` vs. nachsichtig `try_to()`),
  IdleStopper (`idle.timeout_min`, §8.5-Job-Blockade via `JobSource`/
  `update_run`, Job setzt Leerlaufuhr zurück), StatePoller (15 s,
  erkennt Hand-Stopp/-Start, skip bei `busy`, `error` bleibt
  sichtbar), Startfehler-Pfad mit Erholung; beide Phase-15-Lücken
  geschlossen (Hand-Stopp; Weck-Frist gehört jetzt dem Vorgang).
  Doku 75938a1.
- **Phase 17** (8c3816c): Lookup-Cache als eigene, selbstheilende
  SQLite (`lookup-cache.sqlite3`), Schlüssel SHA-256 per Sperrliste
  (ohne `client`/`clientversion`; gzip nur für den Schlüssel
  entpackt), nur 200+`status: ok` (Batch/xml/jsonp bewusst nicht),
  Byte-Parität ohne `X-Cache`, LRU über monotone Sequenz (90 %-
  Räumung), Hit ≠ Aktivität, `invalidate_cache(reason)` (submit im
  Proxy-Pfad; delta_import → Phase 19; manual → Phase 25), wirkt auch
  bei `enabled=false`. E2E: Hit 0,01 s bei gestopptem Stack, kein
  Container läuft danach. Doku 5b0b7ef.
- **Phase 18** (2570ea5): Reihenfolge Rate-Limit → Auth → Cache →
  Wecken (Abweisungen wecken nie, Tripwire-getestet); `apikey`-Modus
  gegen `api_key`-Tabelle (ungesalzenes sha256 + compare_digest,
  „zuletzt benutzt" gedrosselt auf 1 Schreibvorgang/60 s je Key),
  Whitelist-Schalter Picard/beets, Fehlercodes belegt (2/400, 4/400,
  14/429 mit gerechnetem Retry-After, 19/413); IP-Limiter als exaktes
  gleitendes 60-s-Fenster (LRU-Deckel 2048 IPs, Abweisungen zählen
  nicht); 503-Text generisch (Original-Wortlaut Code 13); `/status`
  bleibt offen. E2E 6/6 (apikey schützt Cache-Hit bei gestopptem
  Stack). Doku a96f443.

**Verifikationsbefunde des Orchestrators (keine HOCH-Befunde):**
- Phase 15: Weck-Aufgabe erbte die Frist der ersten Anfrage — als
  Hinweis in Phase 16 gegeben und dort geschlossen.
- Phase 16: Poller-Zugriff aus dem Threadpool geprüft — Tracker ist
  mit Lock + Übergangstabelle threadsicher, kein Handlungsbedarf.
- PEP-758-Syntax (`except A, B:` ohne Klammern, Python 3.14) in
  admin.py/cache.py — valide, kein Befund.

**Angefangen, aber offen:**
- Go-Frage zu Phase 19 und XFF-Frage (Rate-Limit hinter TLS-Proxy)
  am Session-Ende gestellt und vom Betreiber **verworfen** — kein Go,
  kein Entscheid. Beides steht in PROGRESS (offene Punkte 1 und 6).

**Stillschweigend fallengelassen:** nichts — alle in der Session
aufgekommenen Punkte sind entweder erledigt oder als offene Punkte /
Phasen-Hinweise in PROGRESS dokumentiert.

**Übernommene Agenten-Klärungspunkte (je Empfehlung, in DECISIONS):**
- P14: Compose-Projektname geteilt (down-v-Falle dokumentiert),
  Reload-Empfang → Phase 15, EVENT_LOG_LIMIT bleibt Konstante.
- P16: doppelte Log-Erzählung bleibt (Quellen wake/stack), kein
  Idle-Stopp aus `error`, kein Lifespan-Warten auf Stopp-Aufgabe.
- P17: xml/jsonp nicht gecacht, Parameterreihenfolge ungefiltert im
  Schlüssel, Submit-Invalidierung am HTTP-Status, MiB-Lesart,
  Cache-Datei nicht ins Phase-21-Backup (Hinweis gesetzt).
- P18: OPTIONS fail-closed, Cache-Hits zählen gegen das Limit;
  XFF-Vertrauensliste = offener Betreiber-Entscheid (Phase-29-Hinweis).

## 2. Technischer Zustand

- **Git:** Arbeitsbaum sauber, main synchron mit origin/main.
  Session-Spanne e372fd9..a96f443: 11 Commits, 55 Dateien,
  +11.974/−150 Zeilen. Alle Phasen-Commits per ff-Merge aus
  Agent-Worktrees; Worktrees und Branches aufgeräumt.
- **Build/CI:** Grün auf HEAD a96f443 (alle drei Jobs: Lint+Unit,
  Integration PG+Index, Bit-Verifikation pg_acoustid). Jeder der 10
  CI-Läufe dieser Session wurde tatsächlich bis zum Abschluss
  beobachtet, kein Flake.
- **Tests:** 1.649 im Bestand (Session-Start: 1.443): Unit 1.391
  passed + 43 skipped, Integration 198, Bit-Verifikation 8; dazu
  opt-in 3 network- und 6 compose-Tests (laufen nie in CI; der
  Compose-E2E lief lokal je Phase doppelt — Agent und Orchestrator).
- **Neue TODO/FIXME-Marker:** keine.

## 3. Steuerungsdateien (in dieser Session gepflegt)

Je Phase direkt nach Merge aktualisiert (Doku-Aktualitäts-Regel):
- DECISIONS.md: 6 neue Einträge (Healthcheck-Mitentscheid;
  Phase-14/15/16/17/18-Details, je mit verworfenen Alternativen).
- LEARNINGS.md: 5 neue Einträge (Compose-Volume/down -v;
  pytest-Hilfsmodul-Kollision; asyncio-Primitiven vs. Threads;
  Cache-Sperrliste/Fehlerrichtung; KDF vs. Maschinen-Keys).
- ARCHITECTURE.md: §5 config.yaml (Reload-Marke + Empfangsseite),
  §5-Tabelle Lookup-Cache konkretisiert, §7 (/_health intern,
  Lookup-Cache-Block, Auth/Limit-Umsetzung). §5.1/§5.2 unberührt.
- PROGRESS.md: Statuskopf/Übergabe/Tabelle je Phase; Phasenblöcke
  14–18 durch Ergebniszeilen ersetzt; neue Hinweise in den Blöcken
  19 (Cache-Invalidierung), 20 (Fehler-Ereignisse), 21 (Cache nicht
  ins Backup), 27 (EVENT_LOG_LIMIT), 29 (XFF-Entscheid).
- Projektgedächtnis (Claude-Memory acoustid-offline-projekt.md):
  Stand nachgeführt.

## 4. Nächster Schritt für eine frische Session

`git log --oneline -5` + PROGRESS-Statuskopf lesen
(Pflicht-Vorprüfung), dann per AskUserQuestion das Go für **Phase 19**
(Scheduler & Update-Zyklus) einholen; bei Go Opus-Bau-Agent mit dem
Phase-19-Block beauftragen (inkl. Stand-Vorprüfung im Auftragstext).
Nach Phase 19: voller 5-Phasen-Doku-Sweep (15–19) mit Diff-Anzeige.
