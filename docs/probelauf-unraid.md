# Probelauf auf der Unraid-Hardware (Phase-8-Rest)

Der Probelauf beantwortet am **echten Datenbestand auf der Zielhardware**,
was bisher nur lokal an einem Fixture-Tag gemessen wurde (~9,2 MB gz/s ⇒
grob 12–13 h reine DB-Zeit für 414 GB):

1. Wie lange dauert der Voll-Bootstrap wirklich — Stunden oder Tage?
2. Wie groß werden Postgres und Suchindex (Grundlage der
   Empfehlungstabelle für `index.query_hashes`)?
3. Funktioniert die Kette Download → Import → Index-Feed auf Unraid
   end-to-end?

Grundlagen: [importer-job.md](importer-job.md) (Aufruf, Exit-Codes,
Report), ARCHITECTURE §5.1–§5.3, §8.8. Der Probelauf ist **kein
Wegwerf-Lauf**: sein Stand ist per `import_state` resumierbar und wird
später einfach zum Voll-Bootstrap verlängert — nichts löschen.

> **Seit dem Ein-Container-Umbau (M1b)** läuft alles in *einem* Container:
> Der Importer ist kein eigenes Compose-Profil mehr, sondern ein Prozess
> darin (`docker compose exec`). Wer einen Probelauf aus der v1-Zeit auf
> der Platte hat, zieht ihn nach [migration-v1-v2.md](migration-v1-v2.md)
> um — der Bestand bleibt erhalten.

---

## 1. Voraussetzungen

- Unraid (amd64 — das Image ist amd64-only, E3) mit
  Docker-Compose-Unterstützung (Plugin „Docker Compose Manager" oder
  `docker compose` im Terminal).
- Das Repo auf dem Server, z. B. unter `/mnt/user/appdata/acoustid-offline/repo`:

  ```bash
  git clone https://github.com/shares92/acoustid-offline.git
  ```

- Plattenplatz für den Probelauf-Zuschnitt (siehe Schritt 4):
  Dump-Arbeitsverzeichnis braucht nur den Prefetch-Vorlauf (einzelne
  Fingerprint-Tagesdateien der Frühzeit haben mehrere GB), die Postgres
  wächst mit dem eingespielten Zeitraum.

## 2. .env anlegen und die Mounts auf Unraid-Pfade legen

```bash
cd /mnt/user/appdata/acoustid-offline/repo
cp .env.example .env
```

`MMO_DB_PASSWORD` bleibt **leer**: der Entrypoint erzeugt das Passwort
beim ersten Start selbst und legt es unter `/config/db-password` ab.

Die fünf Mounts zeigen per Vorgabe auf Verzeichnisse neben der
Compose-Datei; für den Probelauf sollen die Daten dahin, wo sie auch
produktiv liegen (Index auf den SSD-Cache-Pool, Postgres aufs Array) —
nur so misst der Lauf ehrlich. Dafür genügen fünf Zeilen in der `.env`:

```bash
cat >> .env <<'EOF'
# Cache-Pool
MUSICMETA_CONFIG_DIR=/mnt/cache/appdata/acoustid-offline/config
MUSICMETA_INDEX_DIR=/mnt/cache/appdata/acoustid-offline/index
# Array (Hinweis: /mnt/user/... läuft durch den FUSE-Layer shfs — für die
# Postgres besser einen direkten Disk-/Pool-Pfad, sonst misst der
# Probelauf den FUSE-Overhead mit)
MUSICMETA_DB_DIR=/mnt/disk1/appdata/acoustid-offline/db
MUSICMETA_IMPORT_DIR=/mnt/user/appdata/acoustid-offline/import
MUSICMETA_BACKUP_DIR=/mnt/user/appdata/acoustid-offline/backup
EOF

mkdir -p /mnt/cache/appdata/acoustid-offline/{config,index}
mkdir -p /mnt/disk1/appdata/acoustid-offline/db
mkdir -p /mnt/user/appdata/acoustid-offline/{import,backup}
```

Die Pfade sind Beispiele — Pool-/Disk-Namen an das eigene System
anpassen. Die Mountpunkte **im** Container (`/config`, `/index`,
`/data/db`, `/import`, `/backup`) sind fest. Die Eigentümer setzt der
Entrypoint selbst (Postgres 999, Index 6081) — **nicht** 99:100
(nobody/users) verwenden.

## 4. `index.query_hashes` VOR dem Lauf festlegen

Der Wert bestimmt die Index-Größe (Default 120 ⇒ ~40–55 GB beim
Vollbestand; 80 ⇒ ~30–37 GB) und **eine spätere Änderung heißt
Index-Neuaufbau**. Wer nicht mit dem Default 120 laufen will, legt vor
dem Start eine minimale `config.yaml` ins Konfigurationsverzeichnis:

```bash
printf 'index:\n  query_hashes: 120\n' \
  > /mnt/cache/appdata/acoustid-offline/config/config.yaml
```

Fehlt die Datei, gelten die Defaults aus ARCHITECTURE §6 — für den
Probelauf mit 120 völlig in Ordnung.

## 5. Bauen, starten, Probelauf fahren

Solange es keine veröffentlichten Images gibt, wird lokal gebaut — **ein**
Image für alles (dauert beim ersten Mal einige Minuten, der Suchindex wird
aus der Quelle kompiliert):

```bash
docker compose up -d --build
docker compose logs -f app        # beim Erststart steht hier einmalig
                                  # das Admin-Passwort
```

Der Container startet mit **schlafendem** Stack. Für den Importer müssen
Datenbank und Suchindex laufen:

```bash
docker compose exec app supervisorctl -c /etc/supervisor/supervisord.conf start db index
docker compose exec app supervisorctl -c /etc/supervisor/supervisord.conf status
```

Dann in einer `screen`-/`tmux`-Sitzung starten (das JSON-Ergebnis geht in
die Report-Datei, das Log auf stderr):

**Schritt 1 — Smoke-Lauf** (Minuten; verifiziert nur die Kette):

```bash
docker compose exec app /app/.venv/bin/python -m acoustid_importer \
    --mode bootstrap --end-date 2011-08-31 --index-dir /index \
    --report /import/probelauf-smoke.json
```

**Schritt 2 — Messlauf** (derselbe Befehl, späteres `--end-date`; der
Lauf setzt dank `import_state` automatisch dort fort, wo der Smoke-Lauf
aufgehört hat):

```bash
docker compose exec app /app/.venv/bin/python -m acoustid_importer \
    --mode bootstrap --end-date 2012-12-31 --index-dir /index \
    --report /import/probelauf.json
```

`--index-dir /index` misst die Index-Größe für die
`query_hashes`-Empfehlung — im Ein-Container-Betrieb liegt das
Verzeichnis ohnehin schon da, ein zusätzlicher Mount entfällt.

Als Messlauf ist ein Zeitraum gut, der ein paar Stunden läuft. Jeder
Lauf misst nur die **selbst** verarbeiteten Bytes und rechnet daraus
hoch — bei zu kurzer Laufzeit einfach mit späterem `--end-date` erneut
starten; es wird nie etwas doppelt eingespielt.

**Abbrechen ist jederzeit sicher:** einmal Ctrl-C/SIGTERM beendet
geordnet nach der laufenden Tagesdatei (Exit-Code 8, vollständiger
Report); ein zweites Signal bricht sofort ab, die offene
Datei-Transaktion rollt zurück und wird beim nächsten Lauf wiederholt.

## 6. Ergebnis lesen und zurückmelden

Die Antwort auf die Kernfragen steht im Report unter `projection`
(Details: [importer-job.md §3](importer-job.md)):

| Feld | Frage |
|---|---|
| `projection.estimated_total_hours` | Dauer des Voll-Bootstraps |
| `projection.estimated_db_bytes` | Postgres-Endgröße (Array-Platz) |
| `projection.estimated_index_bytes` | Index-Endgröße (Cache-Platz; nur mit `--index-dir` gefüllt) |
| `projection.throughput_gz_bytes_s` | gemessener Durchsatz (Vergleich: lokal ~9,2 MB gz/s) |
| `measurements.db_after` / `index_after` | tatsächliche Größen nach dem Lauf |
| `warnings`, `escaping_fallbacks`, `unknown_fields` | Auffälligkeiten — sollten leer/0 sein |

Bitte **die Report-JSON(s) komplett** ins Projekt zurückgeben (Datei
liegt im Import-Verzeichnis, s. `MUSICMETA_IMPORT_DIR`), dazu kurz:
welcher Pfadtyp für die Postgres verwendet wurde (FUSE `/mnt/user` oder
direkt) und ob das System nebenher belastet war. Daraus entstehen die
realistische Zeitangabe fürs README (Phase 29) und die
`query_hashes`-Empfehlungstabelle.

## 7. Stolpersteine

- **`/status` meldet vor dem ersten Import nie „bereit"** — normal: der
  interne Healthcheck der API prüft `/<name>/_health` des Suchindex, und
  den legt erst der Importer an. Der Container-Healthcheck (`GET /status`)
  ist davon unberührt und wird sofort grün.
- **Der Container heißt `musicmeta-offline-app-1`** (Projektname + Dienst;
  einen festen Namen vergibt die Compose-Datei bewusst nicht, sonst könnte
  eine zweite Zusammenstellung auf demselben Host ihn ersetzen).
  Angesprochen wird er über den Dienstnamen: `docker compose exec app
  supervisorctl -c /etc/supervisor/supervisord.conf status` zeigt die vier
  Prozesse.
- **Exit-Code 3 (Plattenplatz-Guard):** gemessen wird das
  Dump-Verzeichnis; Reserve ist `update.min_free_gb` (Default 50 GiB,
  `--min-free-gb` überschreibt). Platz schaffen und denselben Befehl
  erneut starten.
- **Exit-Code 5 (`gaps`):** fehlende Tagesdatei in der Historie — nicht
  überbrückbar und absichtlich nicht per CLI übergehbar. Bitte melden;
  der Betreiber entscheidet.
- **Exit-Code 4 (`download_failed`):** Netz-/Serverproblem; einfach
  erneut starten, der Lauf setzt fort.
- Eingespielte Tagesdateien werden gelöscht (Default) — im
  Dump-Verzeichnis liegen nur Prefetch-Vorlauf und aktuelle Datei.
- Nach dem Probelauf **nichts wegräumen**: die Bind-Mounts und
  `import_state` sind der Anfang des echten Bootstraps. (`docker compose
  down -v` kann Bind-Mounts nicht löschen — das ist der Grund für E13.) Nur wenn die Auswertung ein
  anderes `index.query_hashes` ergibt, braucht der Index einen
  Neuaufbau (Vorgehen wird dann nachgereicht).
