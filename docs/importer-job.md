# Importer-Job: Aufruf, Exit-Codes, Ergebnis-Report

Der Importer ist ein **One-Shot-Job**: Er läuft, spielt die offenen
Tagesdeltas ein, gibt neue Fingerprints an den Suchindex, schreibt ein
maschinenlesbares Ergebnis und beendet sich mit einem definierten Exit-Code.
Er startet und stoppt nie Prozesse und kennt keinen Zeitplan — das macht
der Wächter (ab M2.5), der genau die hier beschriebene Schnittstelle liest
und daraus `update_run` füllt.

Technische Grundlagen: ARCHITECTURE §5.1 (Datenquelle), §5.2 (Schema und
Import-Regeln), §5.3 (Index-Feed), §8.3/§8.4 (Transaktion und Resume), §8.8
(Plattenplatz-Guard).

---

## 1. Aufruf

Seit dem Ein-Container-Umbau (M1b) ist der Importer **kein eigenes Image**
mehr: er steckt im selben Image wie alles andere und läuft als Prozess
darin. Über supervisord läuft er bewusst **nicht** (Entscheid E10:
`[program:*]` kann keine Per-Lauf-Argumente übergeben) — ab M2.5 startet
ihn der Wächter als Subprozess, bis dahin wird er von Hand aufgerufen:

```bash
# Im laufenden Container. Der Stack muss wach sein: Datenbank und
# Suchindex braucht der Job.
docker compose exec app supervisorctl -c /etc/supervisor/supervisord.conf start db index

docker compose exec app /app/.venv/bin/python -m acoustid_importer                    # täglicher Lauf
docker compose exec app /app/.venv/bin/python -m acoustid_importer --mode bootstrap   # Erst-Import

# Probelauf mit Messung und Hochrechnung
docker compose exec app /app/.venv/bin/python -m acoustid_importer \
    --mode bootstrap --end-date 2011-12-31 --report /import/probelauf.json

# Langer Lauf ohne offene Sitzung: -d haengt ihn ab, das Log steht im
# Report und in `docker compose logs`.
docker compose exec -d app /app/.venv/bin/python -m acoustid_importer --mode bootstrap

# Direkt (Entwicklung)
uv run python -m acoustid_importer --help
```

Das Datenbank-Passwort muss dabei **nicht** mitgegeben werden: es steht in
`MMO_DB_PASSWORD_FILE` (Default `/config/db-password`), und die liest jeder
Prozess selbst — auch einer, der per `exec` dazukommt und die Umgebung des
Entrypoints nicht erbt.

Der Job liest seine Zugänge aus den `MMO_`-Variablen (ARCHITECTURE §6) und
aus der `config.yaml` des Wächters — von dort genau zwei Werte:
`disk.min_free_gb` (Plattenplatz-Guard) und `acoustid.index.query_hashes`
(Query-Extrakt für den Suchindex). Fehlt die Datei, gelten die Defaults.
Beide Schlüssel hießen bis M2 `update.min_free_gb` bzw.
`index.query_hashes`; die alten Pfade werden noch eine Release-Runde mit
Warnung gelesen (ARCHITECTURE §6, E9).

### Betriebsarten

| Modus | Wann | Was anders ist |
|---|---|---|
| `update` (Default) | täglicher Lauf | alle Migrationen vorab; kein Bulk-Modus; gzip-Prüfung an |
| `bootstrap` | Erst-Import (Voll-Replay ab 2011-08-19) | Migrationsgruppe `core` vorher, `indexes` **nach** dem Massenimport; Bulk-Modus an; gzip-Prüfung aus |

Ablauf im Bootstrap (Import-Regel 6, ARCHITECTURE §5.2):

1. Plattenplatz prüfen (§8.8) — **vor** allem anderen,
2. Migrationsgruppe `core` (Tabellen, Primärschlüssel, lz4-Kompression),
3. Massenimport im Bulk-Modus, Download läuft per Prefetch voraus,
4. Bulk-Modus verlassen, `CHECKPOINT`,
5. Migrationsgruppe `indexes` (Sekundärindizes),
6. Index-Feed — **erst jetzt**, weil sein Arbeitsvorrat der Partialindex
   `fingerprint_idx_unindexed` ist.

### Wichtige Optionen

| Option | Bedeutung |
|---|---|
| `--mode {update,bootstrap}` | Betriebsart, siehe oben |
| `--end-date YYYY-MM-DD` | letzter einzuschließender Kalendertag — der Probelauf-Schalter |
| `--max-days N` / `--max-files N` | Lauf zusätzlich begrenzen (`--max-days` schneidet an der Tagesgrenze) |
| `--report PFAD` | Ergebnis-JSON in eine Datei statt auf stdout (`-`) |
| `--min-free-gb N` | überschreibt `disk.min_free_gb`; `0` schaltet den Guard ab |
| `--prefetch N` | wie viele Dateien im Voraus geladen werden (Default 2; kostet Plattenplatz) |
| `--batch-rows N` | Zeilen je `executemany` (Default 1000; wirkt auf den Speicher, nicht auf den Durchsatz) |
| `--keep-dumps` | eingespielte Tagesdateien behalten (Default: löschen) |
| `--no-index-feed` | keine Übergabe an den Suchindex |
| `--index-dir PFAD` | Datenverzeichnis des acoustid-index, falls read-only gemountet — nur dann ist seine Bytegröße messbar |
| `--total-bytes N` | Bezugsgröße der Hochrechnung (Default 414 GB gz, Stand 2026-07-25) |
| `--no-migrate`, `--no-checkpoint`, `--verify-gzip` | Feinsteuerung für Sonderfälle |

Lücken in der Historie (Import-Regel 5) lassen sich bewusst **nicht** per
Kommandozeile übergehen: ein nachträglich eingespielter alter Tag würde
neuere Upsert-Stände überschreiben.

---

## 2. Exit-Codes

| Code | Ergebnis (`result`) | Bedeutung | Sinnvolle Reaktion |
|---:|---|---|---|
| 0 | `ok` | Alles eingespielt (auch: nichts zu tun) | — |
| 1 | `failed` | Unerwarteter Fehler | Log ansehen; Wiederholung meist zwecklos |
| 2 | `usage_error` | Aufruf, `MMO_`-Umgebung oder `config.yaml` fehlerhaft | Konfiguration korrigieren |
| 3 | `disk_guard` | Freier Platz unter `disk.min_free_gb` (§8.8) | Platz schaffen, Lauf wiederholen; Benachrichtigung |
| 4 | `download_failed` | Tagesdatei kam nicht sauber vom Server | Nächster Zyklus wiederholt automatisch (§8.4) |
| 5 | `gaps` | Fehlende Tagesdatei in der Vergangenheit (Regel 5) | **Nicht** automatisch reparieren — Betreiber entscheidet |
| 6 | `import_failed` | Postgres-Import gescheitert (inkl. Parse-Fehler) | Log ansehen; nächster Zyklus wiederholt |
| 7 | `index_feed_failed` | Übergabe an den Suchindex gescheitert | Nächster Lauf holt den Feed nach |
| 8 | `aborted` | Auf Wunsch beendet (SIGTERM/SIGINT) | Stand ist resumierbar; Lauf einfach neu starten |

In **jedem** Fall entsteht ein vollständiger Report — auch bei Abbruch und
Fehler. Und in jedem Fall gilt: weil eine Tagesdatei genau eine Transaktion
ist (§8.3), steht `import_state` danach exakt auf der letzten vollständig
eingespielten Datei, und der nächste Lauf setzt dort fort (§8.4).

**Signale.** `SIGTERM` (`docker stop`) und `SIGINT` beenden den Lauf
geordnet: die laufende Tagesdatei wird fertig eingespielt, danach endet der
Job mit Code 8. Ein zweites Signal wirkt sofort; dann rollt die offene
Datei-Transaktion zurück und wird beim nächsten Lauf wiederholt.

---

## 3. Ergebnis-Report

JSON auf stdout (Default) oder in die Datei aus `--report`. Das
strukturierte Log geht wie überall auf **stderr** — beide Ströme lassen sich
also getrennt weiterverarbeiten. Die Datei wird atomar geschrieben
(`.part` + Rename), damit der Wächter nie ein halbes Dokument liest.

```json
{
  "schema": "acoustid-offline/importer-run/1",
  "mode": "bootstrap",
  "result": "ok",
  "exit_code": 0,
  "started_at": "2026-07-25T04:00:00+00:00",
  "finished_at": "2026-07-25T04:03:21+00:00",
  "duration_s": 201.4,
  "import_duration_s": 187.2,
  "files": {
    "planned": 7, "imported": 7, "skipped": 0,
    "downloaded": 7, "resumed": 0, "empty": 1
  },
  "rows": 19423,
  "rows_by_stream": {"track": 1693, "meta": 5122, "fingerprint": 2214, "…": 0},
  "files_by_stream": {"track": 1, "meta": 1, "…": 1},
  "gz_bytes": 9343167,
  "downloaded_bytes": 9343167,
  "days": {"first": "2026-07-22", "last": "2026-07-22"},
  "gaps": [],
  "escaping_fallbacks": 0,
  "unknown_fields": [],
  "index_feed": {
    "documents": 2214, "batches": 3, "scanned": 2214,
    "incomplete": 0, "empty_queries": 0,
    "last_id": 123456789, "version": 3, "duration_s": 4.1, "exhausted": true
  },
  "measurements": {
    "disk_before": {"path": "/import", "total_bytes": 0, "free_bytes": 0,
                    "min_free_bytes": 107374182400, "ok": true},
    "disk_after": {"…": 0},
    "disk_checks": 1,
    "db_before": {"total_bytes": 8250000, "tables": {"fingerprint": 8192}},
    "db_after": {"total_bytes": 41500000, "tables": {"fingerprint": 25000000}},
    "index_before": {"documents": 0, "version": null, "bytes": null},
    "index_after": {"documents": 2214, "version": 3, "bytes": null}
  },
  "projection": {
    "total_gz_bytes": 414000000000,
    "measured_gz_bytes": 9343167,
    "measured_duration_s": 187.2,
    "coverage": 0.0000225,
    "throughput_gz_bytes_s": 49900.0,
    "estimated_total_duration_s": 8296000,
    "estimated_total_hours": 2304.4,
    "measured_db_bytes": 33250000,
    "estimated_db_bytes": 1473000000000,
    "measured_index_documents": 2214,
    "estimated_index_documents": 98000000,
    "measured_index_bytes": null,
    "estimated_index_bytes": null
  },
  "error": null,
  "warnings": []
}
```

### Feldbedeutungen

| Feld | Bedeutung |
|---|---|
| `schema` | Formatversion. Ändert sich das Dokument unverträglich, steigt die Zahl |
| `mode`, `result`, `exit_code` | siehe Abschnitt 2 |
| `duration_s` / `import_duration_s` | Gesamtdauer / Summe der reinen Datei-Transaktionen (ohne Migrationen, Download und Feed) |
| `files.planned` | Dateien laut Arbeitsliste nach allen Begrenzungen |
| `files.skipped` | laut `import_state` schon erledigt (Resume-Fall) |
| `files.empty` | leere 23-Byte-Tagesdateien — regulär, **keine** Lücke (§5.1) |
| `rows`, `rows_by_stream` | eingespielte Records; `rows_by_stream` nutzt die Stromnamen aus §5.1 |
| `gz_bytes` / `downloaded_bytes` | verarbeitete Byte gesamt / davon tatsächlich über die Leitung |
| `days` | erster und letzter berührter Kalendertag |
| `gaps` | fehlende Tagesdateien (Dateinamen); bei `result: gaps` der Grund des Abbruchs |
| `escaping_fallbacks` | Zeilen, die erst in der anderen COPY-Escaping-Lesart lesbar waren (§5.1) |
| `unknown_fields` | unbekannte Felder aus dem Sanity-Check (§12 Punkt 8) — Hinweis auf ein geändertes Upstream-Format |
| `index_feed` | Zahlen des Feeds; `null`, wenn er nicht lief |
| `measurements.disk_*` | Plattenplatz vor und nach dem Lauf; `null`, wenn der Guard aus ist |
| `measurements.db_*` | `pg_database_size` und Tabellengrößen inkl. Indizes/TOAST |
| `measurements.index_*` | Dokumentzahl und Version des Suchindex; `bytes` nur mit `--index-dir` |
| `projection` | Hochrechnung auf den Vollbestand (siehe unten) |
| `error` | Typ und Meldung der Ausnahme, die den Lauf beendet hat |
| `warnings` | Auffälligkeiten, die den Lauf nicht gestoppt haben |

Fehlende Messwerte bleiben `null` — sie werden nie geschätzt.

### Hochrechnung

`projection` rechnet linear: gemessene Zeit und gemessener Platzbedarf je
verarbeitetem gz-Byte, hochgerechnet auf `total_gz_bytes` (Default 414 GB,
ARCHITECTURE §5.1). Für den Fingerprint-Strom — 94 % des Volumens — ist das
gut genug für die Frage, um die es geht: Stunden oder Tage? Die Rohwerte
stehen daneben, die Rechnung lässt sich also jederzeit mit einer anderen
Bezugsgröße wiederholen (die Historie wächst um ~58 MB/Tag).

---

## 4. Plattenplatz-Guard (§8.8)

Geprüft wird das Arbeitsverzeichnis der Tagesdateien (`MMO_DUMP_DIR`) —
einmal **vor** dem Lauf (vor der ersten Migration und dem ersten Byte) und
danach in Abständen: nach je 25 Dateien oder 2 GiB, je nachdem was zuerst
eintritt. `disk.min_free_gb` wird als **GiB** gelesen (1024³ Byte), also
die strengere Lesart; `0` schaltet den Guard ab. Default seit M2: **100**
(vorher 50 unter dem Namen `update.min_free_gb` — die Bestände wachsen von
einem AcoustID-Spiegel auf vier Quellen, E11).

Unterschreitet der freie Platz die Reserve, endet der Lauf zwischen zwei
Tagesdateien mit Code 3. Der Stand bleibt vollständig resumierbar.

Gemessen wird bislang nur das Dump-Verzeichnis. Auf dem
Referenz-Deployment (Unraid) liegt die Postgres im selben Array-Pool, der
Guard trifft also denselben Bestand. Die Ausweitung auf **jeden**
Schreib-/Staging-Pfad — `/data/db`, `/import`, ab M4 `/data/covers` — ist
mit M2.5 vorgesehen (E11); die Mounts aus ARCHITECTURE §3 sind mehrere
Dateisysteme, und ein freies `/import` sagt nichts über `/data/db`.

---

## 5. Bulk-Modus (Import-Regel 6)

Während des Bootstraps läuft die Importer-Sitzung mit
`synchronous_commit = off`. Das ist der einzige unsichere Schalter, und er
ist begründet vertretbar: er erlaubt **keine Korruption**, sondern höchstens
den Verlust der zuletzt bestätigten Transaktionen bei einem Strom- oder
Betriebssystemausfall. Da eine Tagesdatei *inklusive* ihrer
`import_state`-Zeile eine Transaktion ist, geht dabei immer eine ganze Datei
verloren — und die spielt der nächste Lauf einfach noch einmal ein.

Zwei Zusicherungen dazu:

* **Nur Sitzungseinstellungen.** Gesetzt wird per `SET` in genau einer
  Verbindung und beim Verlassen wieder auf den vorherigen Wert gesetzt —
  auch im Fehlerfall. Stirbt der Prozess, stirbt die Sitzung und mit ihr die
  Einstellung. Es gibt kein `ALTER SYSTEM` und kein `ALTER DATABASE`.
* **`fsync=off` und `full_page_writes=off` bleiben draußen.** Sie sind nicht
  sitzungsweit setzbar und hinterlassen bei einem Absturz ein korruptes
  Cluster, das kein Resume repariert.

Nach dem Massenimport erzwingt der Job einen `CHECKPOINT` (`--no-checkpoint`
schaltet ihn ab) und baut dann die Sekundärindizes mit erhöhtem
`maintenance_work_mem` — ebenfalls nur in dieser Sitzung.

---

## 6. Download-Prefetch

Ein Hintergrund-Thread lädt die nächsten `--prefetch` Tagesdateien, während
die aktuelle importiert wird. Genau ein Ladethread, damit die Reihenfolge
aus Import-Regel 1 unangetastet bleibt; der Engpass ist ohnehin die
Datenbank. Ladefehler kommen unverändert im Hauptablauf an, ein Abbruch
stoppt den Thread nach der laufenden Übertragung.

Eingespielte Dateien werden gelöscht (`--keep-dumps` hält sie): Sie sind
committet, und 414 GB aufzuheben wäre teuer und nutzlos.
