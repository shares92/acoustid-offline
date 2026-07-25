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

---

## 1. Voraussetzungen

- Unraid (amd64 — das Index-Image existiert nur für linux/amd64) mit
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

## 2. .env anlegen

```bash
cd /mnt/user/appdata/acoustid-offline/repo
cp .env.example .env
# AOFF_DB_PASSWORD setzen — einziger Pflichtwert
```

## 3. Volumes auf Unraid-Pfade legen (docker-compose.override.yml)

Ohne Override landen die benannten Volumes im Docker-Image-Pfad von
Unraid. Für den Probelauf sollen die Daten dahin, wo sie auch produktiv
liegen (DECISIONS 2026-07-25: Index auf den SSD-Cache-Pool, Postgres aufs
Array) — nur so misst der Lauf ehrlich. Datei
`docker-compose.override.yml` neben die `docker-compose.yml` legen:

```yaml
# docker-compose.override.yml — Unraid-Pfade für den Probelauf.
# Hinweis: /mnt/user/... läuft durch den FUSE-Layer (shfs). Für die
# Postgres besser einen exklusiven Share oder direkten Disk-/Pool-Pfad
# verwenden (z. B. /mnt/disk1/..., /mnt/cache/...), sonst misst der
# Probelauf den FUSE-Overhead mit.
services:
  db:
    volumes:
      # Achtung PG 18: Mountpunkt ist das Elternverzeichnis
      # /var/lib/postgresql, NICHT .../data (siehe docker-compose.yml).
      - /mnt/disk1/appdata/acoustid-offline/db:/var/lib/postgresql
  index:
    volumes:
      # SSD-Cache-Pool; Verzeichnis vorher anlegen und chownen:
      #   mkdir -p /mnt/cache/appdata/acoustid-offline/index
      #   chown -R 6081:6081 /mnt/cache/appdata/acoustid-offline/index
      # (NICHT 99:100 — das Image läuft als UID/GID 6081.)
      - /mnt/cache/appdata/acoustid-offline/index:/var/lib/acoustid-index
  importer:
    volumes:
      - /mnt/user/appdata/acoustid-offline/dumps:/data/dumps
      # Index-Verzeichnis zusätzlich read-only in den Importer mounten:
      # nur dann kann der Report die Index-Bytegröße messen
      # (--index-dir unten) — wichtig für die query_hashes-Empfehlung.
      - /mnt/cache/appdata/acoustid-offline/index:/index:ro
```

Die Pfade sind Beispiele — Pool-/Disk-Namen an das eigene System
anpassen. Die Mountpunkte im Container (`:/var/lib/postgresql`,
`:/var/lib/acoustid-index`, `:/data/dumps`) sind fest.

## 4. `index.query_hashes` VOR dem Lauf festlegen

Der Wert bestimmt die Index-Größe (Default 120 ⇒ ~40–55 GB beim
Vollbestand; 80 ⇒ ~30–37 GB) und **eine spätere Änderung heißt
Index-Neuaufbau**. Wer nicht mit dem Default 120 laufen will, legt vor
dem Start eine minimale `config.yaml` an und hängt sie über
`ACOUSTID_WATCHDOG_DATA` ein:

```bash
mkdir -p /mnt/user/appdata/acoustid-offline/watchdog
printf 'index:\n  query_hashes: 120\n' > /mnt/user/appdata/acoustid-offline/watchdog/config.yaml
echo 'ACOUSTID_WATCHDOG_DATA=/mnt/user/appdata/acoustid-offline/watchdog' >> .env
```

Fehlt die Datei, gelten die Defaults aus ARCHITECTURE §6 — für den
Probelauf mit 120 völlig in Ordnung.

## 5. Bauen und Probelauf starten

Solange es keine veröffentlichten Images gibt (Phase 29), wird der
Importer lokal gebaut:

```bash
docker compose --profile job build importer
```

Dann in einer `screen`-/`tmux`-Sitzung starten (der Lauf ist ein
Vordergrund-Prozess; das JSON-Ergebnis geht in die Report-Datei, das
Log auf stderr):

**Schritt 1 — Smoke-Lauf** (Minuten; verifiziert nur die Kette):

```bash
docker compose --profile job run --rm importer \
    --mode bootstrap --end-date 2011-08-31 --index-dir /index \
    --report /data/dumps/probelauf-smoke.json
```

**Schritt 2 — Messlauf** (derselbe Befehl, späteres `--end-date`; der
Lauf setzt dank `import_state` automatisch dort fort, wo der Smoke-Lauf
aufgehört hat):

```bash
docker compose --profile job run --rm importer \
    --mode bootstrap --end-date 2012-12-31 --index-dir /index \
    --report /data/dumps/probelauf.json
```

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
liegt unter `/mnt/user/appdata/acoustid-offline/dumps/`), dazu kurz:
welcher Pfadtyp für die Postgres verwendet wurde (FUSE `/mnt/user` oder
direkt) und ob das System nebenher belastet war. Daraus entstehen die
realistische Zeitangabe fürs README (Phase 29) und die
`query_hashes`-Empfehlungstabelle.

## 7. Stolpersteine

- **`acoustid-index` bleibt anfangs „unhealthy"/„starting"** — normal:
  sein Healthcheck prüft `/<name>/_health` und wird erst grün, nachdem
  der Importer den Index angelegt hat (der Importer wartet deshalb
  bewusst nicht auf „healthy").
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
- Nach dem Probelauf **nichts wegräumen**: Volumes und `import_state`
  sind der Anfang des echten Bootstraps. Nur wenn die Auswertung ein
  anderes `index.query_hashes` ergibt, braucht der Index einen
  Neuaufbau (Vorgehen wird dann nachgereicht).
