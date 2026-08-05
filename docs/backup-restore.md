# Sicherung und Wiederherstellung

Der Backup-Job sichert genau das, was sich **nicht wiederherstellen lässt**.
Alles andere kommt aus den Quellen zurück: der AcoustID-Bestand aus den
Tagesdeltas, der Suchindex aus dem Bestand, ab M4 die Cover aus CAA/TADB/
Discogs. Diese Auswahl ist ein Entscheid (DECISIONS 2026-07-25 „Backup nur
für lokale Unikate", v2 §6.12) und kein Sparzwang: ein Backup, das 2 TB
Delta-Bestand mitschleppt, wird nie gemacht — und wäre im Ernstfall langsamer
als ein Neuaufbau.

Technische Grundlagen: ARCHITECTURE §5.2 (`local_submission`), §5 „SQLite
(Wächter, Cache)", §6 (`backup.*`).

---

## 1. Was gesichert wird

| Teil | Datei im Sicherungsverzeichnis | Warum |
|---|---|---|
| Eigene Einreichungen | `local_submission.copy.gz` | Sie stehen **nirgends sonst**: der Delta-Strom kennt sie nicht, und upstream liegen sie bestenfalls unter fremden IDs |
| Wächter-Zustand | `watchdog.sqlite3` | API-Keys, Admin-Login, Lauf-Historie, Ereignis-Log |
| Konfiguration | `config.yaml` | Alle Laufzeit-Einstellungen samt Zugängen (Secrets im Klartext!) |
| Beschreibung | `manifest.json` | Spaltenliste, Sequenzstände, Zeilenzahl — das Restore braucht sie |

**Ausdrücklich nicht gesichert:**

- **`lookup-cache.sqlite3`** — er hält nichts, was sich nicht neu berechnen
  ließe, und würde die Sicherung um Hunderte Megabyte aufblähen. Nach einem
  Restore ist er einfach leer; die erste Anfrage füllt ihn wieder.
- Der AcoustID-Datenbestand (`/data/db`) und der Suchindex (`/index`) — beide
  entstehen aus den Tagesdeltas neu (Bootstrap, `docs/importer-job.md`).
- Cover (`backup.include_covers`, Default `false`). Der Schalter steht schon
  im Schema; die Cover-Ablage entsteht mit M4. Bis dahin vermerkt der Job ihn
  nur im Manifest und meldet als Warnung, dass er nichts tut.

**Die Sicherung enthält Secrets im Klartext** (`config.yaml`:
`mb.dsn`, `acoustid.submit.upstream_app_key`, `notify.smtp.pass`, …). Sie
bekommt dieselben Rechte wie das Original (0640) — das Sicherungsverzeichnis
gehört entsprechend geschützt.

---

## 2. Wann gesichert wird

`backup.time` (Default `04:45`, also nach dem Delta-Import um `04:00`) und
`backup.dir`. **Leeres `backup.dir` heißt „aus"** — das ist der
Auslieferungszustand, wie bei allen Zugängen im Schema.

Der Lauf ist ein Job wie jeder andere (E10): der Wächter weckt dafür die
Datenbank, startet `python -m acoustid_importer.backup` als Subprozess,
schreibt das Ergebnis nach `update_run` und legt die Prozesse anschließend
wieder schlafen. Solange er läuft, blockiert er den Idle-Stopp (§8.5).

Ein Lauf schreibt ein **eigenes Verzeichnis**:

```
/backup/backup-20260805-044500/
├── manifest.json
├── local_submission.copy.gz
├── watchdog.sqlite3
└── config.yaml
```

Geschrieben wird zuerst nach `backup-<stempel>.part` und erst der fertige
Satz umbenannt: ein abgebrochener Lauf hinterlässt damit kein Verzeichnis,
das wie eine gültige Sicherung aussieht.

**Aufbewahrung ist Sache des Betreibers.** Der Job löscht nie etwas — eine
automatische Rotation könnte im Fehlerfall die letzte gute Sicherung
mitnehmen, und das ist genau die Sorte Automatismus, die man nachts nicht
will. Ein Cron-Einzeiler auf dem Host genügt:

```bash
find /mnt/user/backups/musicmeta -maxdepth 1 -name 'backup-*' -mtime +30 -exec rm -rf {} +
```

Von Hand auslösen (ohne auf den Termin zu warten):

```bash
docker compose exec app /app/.venv/bin/python -m acoustid_importer.backup \
    --target /backup --report /config/jobs/backup.json
```

---

## 3. Wiederherstellung

Ausgangslage: eine frische Instanz (oder eine, deren `/config` verloren ist).
Der Datenbestand muss **nicht** aus der Sicherung kommen — er wird neu
aufgebaut.

### 3.1 Container einrichten

```bash
# Sicherung bereitlegen (Beispiel)
BACKUP=/mnt/user/backups/musicmeta/backup-20260805-044500

docker compose up -d
docker compose logs app        # beim Erststart steht hier das Admin-Passwort
```

### 3.2 Konfiguration und Wächter-Zustand zurückspielen

Beides sind Dateien auf dem `/config`-Mount. Der Wächter darf dabei **nicht
laufen** — er hält die SQLite offen und seine Konfiguration im Speicher:

```bash
docker compose exec app supervisorctl -c /etc/supervisor/supervisord.conf \
    stop watchdog

cp "$BACKUP/config.yaml"      /mnt/user/appdata/musicmeta/config/config.yaml
cp "$BACKUP/watchdog.sqlite3" /mnt/user/appdata/musicmeta/config/watchdog.sqlite3
chmod 0640 /mnt/user/appdata/musicmeta/config/config.yaml

docker compose exec app supervisorctl -c /etc/supervisor/supervisord.conf \
    start watchdog
```

Danach gelten wieder die alten Einstellungen, API-Keys und das alte
Admin-Passwort. **Das DB-Passwort gehört nicht dazu**: es steht in
`/config/db-password` und wird beim Erststart erzeugt (E16) — die neue
Instanz hat ihr eigenes, und das ist richtig so.

### 3.3 Datenbestand aufbauen

```bash
# Schema anlegen und die Tagesdeltas einspielen (dauert lange, siehe README)
docker compose exec -d app /app/.venv/bin/python -m acoustid_importer \
    --mode bootstrap
```

> **Während des Bootstraps keine Submits annehmen.** Ein von Hand
> gestarteter Lauf hat den Schutz nicht, den der Wächter seinen eigenen
> Jobs gibt (`/config/index-feed.busy`, ARCHITECTURE §8.12): eine
> Einreichung erhöht die Index-Version mitten im Feed. Vereinzelte
> Konflikte heilt der Feed inzwischen selbst, aber der sichere Weg bei
> einem tagelangen Bootstrap ist `acoustid.submit.mode: off` in der
> `config.yaml` — danach wieder zurückstellen.

### 3.4 Eigene Einreichungen zurückspielen

**Erst wenn das Schema steht** (Schritt 3.3 hat die Migrationsgruppe `core`
angewendet). Der Dump ist COPY-Text — genau das Format, das `COPY … FROM`
ohne Umweg wieder einliest, inklusive der `integer[]`-Vektoren:

```bash
# Spaltenliste aus dem Manifest — sie ist der Grund, warum es das Manifest gibt
COLUMNS=$(python3 -c "import json,sys; print(', '.join(
    json.load(open(sys.argv[1]))['local_submission']['columns']))" "$BACKUP/manifest.json")

gunzip -c "$BACKUP/local_submission.copy.gz" | docker compose exec -T app \
    psql -U acoustid -d acoustid -c "COPY local_submission ($COLUMNS) FROM STDIN"
```

Dann die **Sequenzen** nachziehen. Ohne diesen Schritt vergibt die Instanz
Dokument-IDs, die es schon gibt — und der Suchindex bekäme zwei Fingerprints
unter derselben Nummer (§5.3, reservierter Bereich `[2^31, 2^32-1]`):

```bash
python3 -c "import json,sys; d=json.load(open(sys.argv[1]))['local_submission']['sequences']
print('\n'.join(f\"SELECT setval('{name}', {value});\" for name, value in d.items()))" \
    "$BACKUP/manifest.json" \
  | docker compose exec -T app psql -U acoustid -d acoustid
```

### 3.5 Einreichungen neu indexieren

Die wiederhergestellten Zeilen tragen ihren alten Status. Damit sie im
Suchindex wieder auffindbar sind, werden sie einmal auf `new` gesetzt und
nachgetragen — den Nachlauf macht der Warteschlangen-Job (er indexiert
zurückgestellte Einreichungen unabhängig vom Submit-Modus):

```bash
docker compose exec -T app psql -U acoustid -d acoustid \
    -c "UPDATE local_submission SET status = 'new', indexed_at = NULL
        WHERE status IN ('indexed', 'forwarded')"

docker compose exec app /app/.venv/bin/python -m acoustid_api.queuejob \
    --report /config/jobs/queue-send.json
```

> **Achtung im Modus `local+upstream`:** Der Status `forwarded` sagt, dass
> eine Einreichung schon bei api.acoustid.org liegt. Setzt man ihn auf `new`
> zurück, wird sie beim nächsten Warteschlangenlauf **erneut** eingereicht.
> Wer das nicht will, nimmt `forwarded` aus der `WHERE`-Klausel heraus und
> indexiert diese Zeilen von Hand (`status = 'indexed'` genügt dafür nicht —
> dann fehlen sie im Index). Der sichere Weg: erst `acoustid.submit.mode` auf
> `local` stellen, indexieren, dann zurückstellen.

### 3.6 Abnahme

```bash
curl -s http://localhost:8080/status | python3 -m json.tool
```

- `stack.state` ist `sleeping` oder `ready` — nicht `error`.
- `data.last_sequence` zeigt den Stand des Bootstraps.
- Ein `POST /v2/submission_status` mit einer bekannten ID aus der Sicherung
  antwortet mit `"imported"` und der lokalen AcoustID.
- Ein Lookup auf einen wiederhergestellten Fingerprint findet ihn.

---

## 4. Was eine Sicherung **nicht** rettet

- **Den Zeitpunkt.** Zwischen der letzten Sicherung und dem Ausfall
  eingegangene Einreichungen sind weg. Wer das nicht hinnehmen will, sichert
  häufiger — der Job kostet Sekunden, solange `local_submission` klein ist.
- **Den Suchindex.** Er wird aus dem Bestand neu aufgebaut; das dauert (siehe
  README). Eine Dateikopie im Stillstand oder `GET /:index/_snapshot` ist ein
  eigener Weg (ARCHITECTURE §5.3 „Betrieb"), aber kein Teil dieses Jobs.
- **Eine kaputte Sicherung.** Der Job prüft nach dem Schreiben nicht nach.
  Wer sichergehen will, spielt sie einmal auf einer Wegwerf-Instanz ein —
  das ist ohnehin die einzige Probe, die etwas beweist.
