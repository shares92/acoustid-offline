# Volume-Migration v1 → v2 (Ein-Container-Umbau)

Wie ein bestehender Bestand aus dem **Fünf-Container-Stack** (v1) in den
**Ein-Container-Betrieb** (v2, ab M1b) übernommen wird — ohne den
414-GB-Replay noch einmal zu fahren.

> **Stand: geschrieben, noch nicht geprobt.** Das Rezept ist auf der
> Betreiber-Hardware (Tower) durchzuspielen, **bevor** produktiv
> geschnitten wird (Risiko R3 der M0-Impact-Analyse). Bis dahin gilt jeder
> Schritt als plausibel, keiner als bewiesen. Der Probelauf-Bestand auf
> Tower ist zugleich das Testobjekt.

---

## 1. Was sich ändert

| | v1 | v2 |
|---|---|---|
| Container | 5 (watchdog, api, db, index, importer) | **1** |
| Compose | `docker-compose.yml` + `docker-compose.watchdog.yml` | eine Datei, ein Service |
| Postgres | Image `postgres:18`, Volume auf `/var/lib/postgresql`, Daten unter `18/docker` | eingebacken, Daten unter `/data/db/18` |
| Suchindex | Image per Digest, Volume auf `/var/lib/acoustid-index` | eingebacken (aus Quelle), Mount `/index` |
| Wächter-Daten | Volume auf `/data` (Cache) | Mount `/config` (Cache) |
| Dumps | Volume `dump-data` auf `/data/dumps` | Mount `/import` (Array) |
| Backup | — | Mount `/backup` (Array), Job ab M2.5 |
| DB-Passwort | `AOFF_DB_PASSWORD` in der `.env` (Pflicht) | erzeugt der Entrypoint (`/config/db-password`) |

Die beiden heiklen Posten sind **das Postgres-Layout** und **die
Wächter-Daten**; alles andere ist ein Verschieben von Verzeichnissen.

---

## 2. Vorbereitung

1. **Bestand feststellen.** Namen der v1-Volumes und ihre Orte auf der
   Platte:

   ```
   docker volume ls --filter label=com.docker.compose.project=acoustid-offline
   docker volume inspect acoustid-offline_db-data --format '{{ .Mountpoint }}'
   docker volume inspect acoustid-offline_index-data --format '{{ .Mountpoint }}'
   docker volume inspect acoustid-offline_watchdog-data --format '{{ .Mountpoint }}'
   ```

   Auf Unraid mit gesetztem `ACOUSTID_WATCHDOG_DATA` sind es Host-Pfade
   statt Volumes — dann diese verwenden.

2. **Platz prüfen.** Verschieben innerhalb desselben Dateisystems ist ein
   `mv` (Sekunden). Über Dateisystemgrenzen hinweg wird kopiert — dann
   muss der Bestand **zweimal** passen:

   ```
   du -sh "$(docker volume inspect acoustid-offline_db-data --format '{{ .Mountpoint }}')"
   df -h /mnt/user
   ```

3. **Alles anhalten.** Postgres darf während des Umzugs nicht laufen,
   sonst wird ein halb geschriebener Checkpoint mitgenommen:

   ```
   docker compose -f docker-compose.yml stop
   docker compose -f docker-compose.watchdog.yml down
   ```

   `stop`/`down` — **niemals `down -v`**: das löscht die Volumes, um die es
   hier geht (die v1-Falle, LEARNINGS).

4. **Sichern, was klein und unersetzlich ist.** Der Datenbestand lässt sich
   im Zweifel neu einspielen, die Wächter-Daten nicht (API-Keys,
   Admin-Login, `config.yaml`, lokale Submissions liegen dagegen in der
   Datenbank):

   ```
   tar czf ~/acoustid-watchdog-backup.tgz \
     -C "$(docker volume inspect acoustid-offline_watchdog-data --format '{{ .Mountpoint }}')" .
   ```

---

## 3. Zielverzeichnisse anlegen

Die Aufteilung Cache/Array ist der Punkt der ganzen Übung — sie entscheidet,
ob das Array je schläft:

| Mount | Ablage | Inhalt |
|---|---|---|
| `/config` | **Cache** | `config.yaml`, SQLite, Lookup-Cache, Logs, DB-Passwort |
| `/index` | **Cache** | Suchindex (~70 GB einplanen) |
| `/data/db` | Array | PostgreSQL |
| `/import` | Array | Dump-Downloads |
| `/backup` | Array | Sicherungen (ab M2.5) |

Beispiel für Unraid (Shares vorher anlegen; `appdata` auf *Prefer/Only:
Cache*):

```
mkdir -p /mnt/user/appdata/musicmeta/{config,index}
mkdir -p /mnt/user/musicmeta/{db,import,backup}
```

---

## 4. Postgres umziehen (der heikle Teil)

Das v1-Volume hängt auf `/var/lib/postgresql`; die Daten liegen darin unter
`18/docker` — so legt es das offizielle `postgres:18`-Image an (LEARNINGS
„Postgres-18-Image ändert das Volume-Layout"). Ziel ist `/data/db/18`.

> **Die Variablennamen sind je Abschnitt eigene** (`DB_V1`/`DB_V2`,
> `INDEX_V1`/… ) und werden nicht wiederverwendet. Grund: der
> Rollback in §9 greift auf die Postgres-Pfade zurück — mit einem
> allgemeinen `$SRC`/`$DEST` stünde dort längst der Index- oder
> Config-Pfad drin, und der Rückweg schöbe die Datenbank ins Leere.

```
DB_V1="$(docker volume inspect acoustid-offline_db-data --format '{{ .Mountpoint }}')"
DB_V2=/mnt/user/musicmeta/db

# 1. Prüfen, dass die Annahme stimmt:
ls "$DB_V1"                          # erwartet: 18
cat "$DB_V1/18/docker/PG_VERSION"    # erwartet: 18

# 2. Umziehen (gleiches Dateisystem = mv; sonst rsync -a --info=progress2)
mkdir -p "$DB_V2"
mv "$DB_V1/18/docker" "$DB_V2/18"

# 3. Eigentümer setzen: das neue Image fährt Postgres unter UID/GID 999.
chown -R 999:999 "$DB_V2/18"
chmod 0700 "$DB_V2/18"
```

**Prüfkommandos danach** (alle drei müssen stimmen):

```
test -f "$DB_V2/18/PG_VERSION" && cat "$DB_V2/18/PG_VERSION"   # 18
ls "$DB_V2/18/base" >/dev/null && echo "base vorhanden"
stat -c '%u:%g %a' "$DB_V2/18"                                 # 999:999 700
```

**Notieren Sie sich beide Pfade** — der Rollback in §9 braucht sie.

> **Kein `pg_upgrade`.** Die Major-Version bleibt dieselbe; nur der Pfad
> ändert sich. Ein Bestand einer *anderen* Major lässt sich so **nicht**
> übernehmen — der Wächter verweigert dann den Start und sagt es
> (Versions-Drift-Guard, E14).

---

## 5. Suchindex umziehen

```
INDEX_V1="$(docker volume inspect acoustid-offline_index-data --format '{{ .Mountpoint }}')"
INDEX_V2=/mnt/user/appdata/musicmeta/index

mkdir -p "$INDEX_V2"
mv "$INDEX_V1"/* "$INDEX_V2"/
chown -R 6081:6081 "$INDEX_V2"
```

Die UID 6081 ist unverändert die des Upstream-Images — deshalb passt ein
v1-Index-Verzeichnis ohne weiteres Zutun.

> **Wenn etwas schiefgeht, ist der Index das kleinere Übel:** er lässt sich
> aus der Datenbank neu aufbauen (Index-Feed), die Datenbank nicht.

---

## 6. Wächter-Daten umziehen

```
CONFIG_V1="$(docker volume inspect acoustid-offline_watchdog-data --format '{{ .Mountpoint }}')"
CONFIG_V2=/mnt/user/appdata/musicmeta/config

mkdir -p "$CONFIG_V2"
cp -a "$CONFIG_V1"/. "$CONFIG_V2"/
```

Enthalten sind `config.yaml`, `watchdog.sqlite3` (API-Keys, Admin-Login,
Lauf-Historie, Ereignis-Log) und `lookup-cache.sqlite3`. Der Lookup-Cache
darf verworfen werden — er füllt sich von selbst; alles andere nicht.

**Nach dem Umzug prüfen:**

```
ls "$CONFIG_V2"                     # config.yaml, watchdog.sqlite3, …
sqlite3 "$CONFIG_V2/watchdog.sqlite3" 'select count(*) from api_key;'
```

Die Dateirechte zieht der Entrypoint beim ersten Start nach: `/config`
bekommt das setgid-Bit und die Gruppe `musicmeta`, `config.yaml` und
`db-password` werden 0640 — nur so kann der (seit v2 unprivilegierte)
API-Dienst sie lesen.

---

## 7. Datenbank-Passwort übernehmen

v1 hatte das Passwort in der `.env`; v2 erzeugt es beim ersten Start selbst
(E16). Für einen **übernommenen** Bestand existiert die Rolle aber schon —
deshalb das alte Passwort mitnehmen, statt ein neues erzeugen zu lassen:

```
CONFIG_V2=/mnt/user/appdata/musicmeta/config
printf '%s' "$AOFF_DB_PASSWORD_AUS_DER_ALTEN_ENV" > "$CONFIG_V2/db-password"
chmod 0640 "$CONFIG_V2/db-password"
```

Alternativ genügt es, `AOFF_DB_PASSWORD` beim ersten Start in der `.env`
stehen zu lassen — der Entrypoint schreibt die Datei dann selbst (und
danach kann der Wert aus der `.env` wieder verschwinden).

Alternative, wenn das alte Passwort nicht mehr vorliegt: Datei **nicht**
anlegen, den Container starten (der Entrypoint erzeugt eines) und die Rolle
danach einmal angleichen:

```
docker compose exec app gosu postgres \
  /usr/lib/postgresql/18/bin/psql -d postgres \
  -c "ALTER ROLE acoustid PASSWORD '$(cat /config/db-password)'"
```

---

## 8. Umschalten

```
cp .env.example .env      # AOFF_DB_PASSWORD bleibt leer (s. o.)
docker compose up -d --build
docker compose logs -f app
```

Erwartet im Log: `entrypoint: vorhandenes Cluster in /data/db/18
(PG_VERSION 18)` — **nicht** `initdb`. Steht dort `initdb`, hat der
Container den Bestand nicht gefunden: sofort stoppen und Mounts prüfen,
bevor irgendetwas geschrieben wird.

**Abnahme** (in dieser Reihenfolge):

```
curl -s localhost:8080/status | jq .          # antwortet, Stack „schlafend"
docker compose exec app supervisorctl status  # watchdog + index laufen, db/api gestoppt
curl -s 'localhost:8080/v2/lookup?client=test&fingerprint=…&duration=641'
docker compose exec app supervisorctl status  # jetzt laufen db und api
curl -s localhost:8080/status | jq .stack     # „bereit"
```

Der Datenstand aus `/status` muss dem vor der Migration entsprechen — das
ist die eigentliche Abnahme: eine Instanz, die einen leeren Bestand fröhlich
neu anlegt, sieht von außen genauso gesund aus.

---

## 9. Zurück (Rollback)

Solange die v1-Volumes nicht gelöscht sind, ist der Weg zurück ein
Zurückschieben. Der folgende Block ist **in sich geschlossen** und läuft
auch in einer frischen Shell — er leitet die Pfade selbst wieder her:

```
# 1. v2 anhalten (NICHT -v; es gibt ohnehin keine benannten Volumes)
docker compose down

# 2. Pfade erneut bestimmen — dieselben wie in §4
DB_V1="$(docker volume inspect acoustid-offline_db-data --format '{{ .Mountpoint }}')"
DB_V2=/mnt/user/musicmeta/db

# 3. Datenbestand zurückschieben
test -f "$DB_V2/18/PG_VERSION" || { echo "kein v2-Bestand unter $DB_V2/18"; exit 1; }
mkdir -p "$DB_V1/18"
mv "$DB_V2/18" "$DB_V1/18/docker"
chown -R 999:999 "$DB_V1/18/docker"   # v1-Image fährt Postgres ebenfalls als 999

# 4. Suchindex zurück (falls schon umgezogen)
INDEX_V1="$(docker volume inspect acoustid-offline_index-data --format '{{ .Mountpoint }}')"
INDEX_V2=/mnt/user/appdata/musicmeta/index
mv "$INDEX_V2"/* "$INDEX_V1"/ 2>/dev/null || true

# 5. v1-Stand auschecken — die beiden Compose-Dateien von v1 gibt es im
#    aktuellen Stand nicht mehr (ein Image, eine Datei).
git checkout <letzter-v1-commit-oder-tag>    # z.B. der Commit vor M1b
docker compose -f docker-compose.yml -f docker-compose.watchdog.yml up -d
```

Die Wächter-Daten (§6) wurden **kopiert**, nicht verschoben — das v1-Volume
ist also unverändert und braucht keinen Rückweg.

Deshalb wird oben **verschoben und nicht kopiert-und-gelöscht**: bis zum
ersten erfolgreichen Start von v2 existiert der Bestand genau einmal, danach
ist der alte Pfad leer und der Rollback ein `mv` in die Gegenrichtung.

Die v1-Volumes erst löschen, wenn v2 einen **vollständigen Delta-Import**
hinter sich hat — nicht schon nach dem ersten grünen `/status`.

---

## 10. Offene Punkte

- [ ] **Probe auf Tower** (Betreiber-Hardware) mit dem Bestand des
      Unraid-Probelaufs — inkl. Zeitmessung für `mv`/`rsync` und der
      Abnahme aus §8.
- [ ] Danach: gemessene Dauer und etwaige Abweichungen hier eintragen,
      Messwerte nach LEARNINGS.
