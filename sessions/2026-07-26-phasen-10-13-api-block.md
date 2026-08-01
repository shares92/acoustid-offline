# Session-Archiv: 2026-07-25/26 — Probelauf-Anleitung + Phasen 10–13 (API-Block)

Dies ist die „parallele Bau-Session", auf deren Ergebnis die
Projektstart-Session am 01.08. traf (deren Archiv:
`2026-08-01-phase7-nachverifikation.md`). Session-Ende ausgeführt am
2026-07-28. Arbeitsmodus durchgehend: je Phase ein Opus-Bau-Agent im
isolierten Worktree, Fable-Verifikation (HOCH-Befunde doppelt,
paritätskritisches am Code belegt), ff-Merge, CI-Beobachtung,
Doku-Sweep, dann Pause mit Go-Frage.

## 1. Session-Review

### Angefordert
- `/session-start`; Go des Betreibers: **„Phase 10 + Probelauf-Anleitung
  parallel"**; danach sequenzielle Gos für Phase 11, 12 und 13 (je per
  AskUserQuestion mit Optionen + Empfehlung).
- Zwischendrin ein Serverfehler mitten in der Phase-10-Verifikation;
  Fortsetzung auf Zuruf („mach weiter").
- Abschließend `/session-end`.

### Fertig
1. **Unraid-Probelauf-Anleitung** `docs/probelauf-unraid.md`
   (Commit 8fad234): Override mit Unraid-Pfaden (PG 18-Mountpunkt,
   Index-chown 6081, Index read-only in den Importer für die
   Byte-Messung), `index.query_hashes` VOR dem Lauf festlegen,
   Smoke- + Messlauf mit `--end-date`, Report-Auswertung,
   Stolpersteine. Verlinkt aus README + PROGRESS.
2. **Phase 10 — API MB-Resolver & meta** (Commits a467064/ea008a6,
   Sweep a29e3c2): `shared/shared/mb/` (psycopg3 + Pool,
   Circuit-Breaker, Selfcheck, einzige MB-Datei `queries.py` als Test
   verankert), volle meta-Grammatik bug-für-bug (Präzedenz am
   Original-Quelltext belegt), Online-Redirect-Auflösung
   (`mb.keep_submitted_mbid`), degradierter Betrieb §8.7.
   Zuschnitt-Entscheide von Fable: psycopg3 statt SQLAlchemy,
   Platzierung in shared (Wächter-Verbindungstest Phase 25).
3. **Phase 11 — /v2/submit off/local** (ead4790 + b15c60b, Sweep
   e3419c4): `local_submission` (eine Zeile je MBID, Gruppierung
   `local_track_id`), Statusmaschine new→indexed synchron,
   reservierter Doc-ID-Bereich [2^31, 2^32-1].
   **HOCH-Finding: Index-Doc-IDs sind u32** (empirisch; Client
   korrigiert, Index-Bericht Addendum 14); Disjunktheit typbedingt
   (fingerprint.id ist integer) — von Fable doppelt geprüft.
   Vormerkung Phase 19: Submit↔Feed-`expected_version`-Konflikt (am
   Code belegt). Sweep zog die DDL anweisungsgleich in §5.2 (der
   Wort-für-Wort-Schematest zieht wieder ohne Ausnahme).
4. **Phase 12 — Upstream-Forwarding & Queue** (657ee14, Sweep
   6829ef3): Erstversuch in der Submit-Anfrage (wirft nie),
   `drain_queue`/`retry_forward` als Phase-19-Aufrufpunkte, Drossel
   ≤ 3 req/s prozessweit, Backoff 1→30 s, attempts zählt Läufe,
   7-Grenze ⇒ `upstream_forward_gave_up` (Phase 20), Key maskiert,
   nur https, `user`-Key unverändert. Fable verifizierte Maskierung,
   https-Zwang, Fehlerschlucken im Anfragepfad, Array-Casts.
   MITTEL-Risiko dokumentiert: nie gegen den echten Dienst gelaufen
   (Echtlauf-Vormerkung Phase 28).
5. **Phase 13 — /v2/lookup/batch & /v2/submission_status** (1d8874a,
   Sweep ebdbdcc): Objekt-Hülle `queries`, `responses` mit `index`,
   Teilfehler je Eintrag bei HTTP 200, Limit 100 ⇒ 19/413,
   meta-Bündelung je MetaPlan; Status-Mapping new ⇒ pending, ab
   indexed ⇒ imported + result.id, nie 404. §7 im Sweep an den
   tatsächlichen Vertrag angepasst. **Damit API-Block 9–13 komplett.**
- Testbestand 894 → **1349** (+455 über die vier Phasen); jede Phase
  lokal (Fast-Suite + teils Voll-Integration) und in CI grün.

### Angefangen, aber nicht abgeschlossen
- **Go-Frage für Phase 14**: gestellt, aber die Übermittlung brach ab
  (Tool-Fehler/Abbruch) — **kein Go erteilt**. Von der
  01.08.-Session als offener Punkt 1 übernommen.

### Offen (nicht Teil dieser Session)
- Unraid-Probelauf (Ausführung Betreiber-seitig; Anleitung liegt vor).
- Stillschweigend fallengelassen wurde nichts.

## 2. Technischer Zustand (bei Session-Ende 2026-07-28)

- `git status`: sauber; main == origin/main. HEAD `6a36f28` — auf die
  Session-Commits (…ebdbdcc) folgten zwei Commits der
  Projektstart-Session (abd1225 LEARNINGS-Nachtrag, 6a36f28 deren
  Session-Ende mit PROGRESS-Neuformat).
- CI: alle drei Jobs grün auf ebdbdcc, abd1225 und 6a36f28.
- Keine uncommitteten Änderungen, keine übrigen Worktrees/Branches,
  keine neuen TODO/FIXME-Marker aus dieser Session.
- CI-Episoden dieser Session (beide aufgeklärt, LEARNINGS-Folgefund):
  Docker-Hub-Registry-Timeout beim Container-Init (Flake, `gh run
  rerun --failed` genügte) und ein „cancelled" durch die
  `cancel-in-progress`-Concurrency beim Push-auf-Push (kein Fehler —
  der Nachfolgelauf zählt).
