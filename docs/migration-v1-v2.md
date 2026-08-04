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

```
SRC="$(docker volume inspect acoustid-offline_db-data --format '{{ .Mountpoint }}')"
DEST=/mnt/user/musicmeta/db

# 1. Prüfen, dass die Annahme stimmt:
ls "$SRC"                    # erwartet: 18
cat "$SRC/18/docker/PG_VERSION"   # erwartet: 18

# 2. Umziehen (gleiches Dateisystem = mv; sonst rsync -a --info=progress2)
mkdir -p "$DEST"
mv "$SRC/18/docker" "$DEST/18"

# 3. Eigentümer setzen: das neue Image fährt Postgres unter UID/GID 999.
chown -R 999:999 "$DEST/18"
chmod 0700 "$DEST/18"
```

**Prüfkommandos danach** (alle drei müssen stimmen):

```
test -f "$DEST/18/PG_VERSION" && cat "$DEST/18/PG_VERSION"     # 18
ls "$DEST/18/base" >/dev/null && echo "base vorhanden"
stat -c '%u:%g %a' "$DEST/18"                                  # 999:999 700
```

> **Kein `pg_upgrade`.** Die Major-Version bleibt dieselbe; nur der Pfad
> ändert sich. Ein Bestand einer *anderen* Major lässt sich so **nicht**
> übernehmen — der Wächter verweigert dann den Start und sagt es
> (Versions-Drift-Guard, E14).

---

## 5. Suchindex umziehen

```
SRC="$(docker volume inspect acoustid-offline_index-data --format '{{ .Mountpoint }}')"
DEST=/mnt/user/appdata/musicmeta/index

mkdir -p "$DEST"
mv "$SRC"/* "$DEST"/
chown -R 6081:6081 "$DEST"
```

Die UID 6081 ist unverändert die des Upstream-Images — deshalb passt ein
v1-Index-Verzeichnis ohne weiteres Zutun.

> **Wenn etwas schiefgeht, ist der Index das kleinere Übel:** er lässt sich
> aus der Datenbank neu aufbauen (Index-Feed), die Datenbank nicht.

---

## 6. Wächter-Daten umziehen

```
SRC="$(docker volume inspect acoustid-offline_watchdog-data --format '{{ .Mountpoint }}')"
DEST=/mnt/user/appdata/musicmeta/config

mkdir -p "$DEST"
cp -a "$SRC"/. "$DEST"/
```

Enthalten sind `config.yaml`, `watchdog.sqlite3` (API-Keys, Admin-Login,
Lauf-Historie, Ereignis-Log) und `lookup-cache.sqlite3`. Der Lookup-Cache
darf verworfen werden — er füllt sich von selbst; alles andere nicht.

**Nach dem Umzug prüfen:**

```
ls "$DEST"                     # config.yaml, watchdog.sqlite3, …
sqlite3 "$DEST/watchdog.sqlite3" 'select count(*) from api_key;'
```

---

## 7. Datenbank-Passwort übernehmen

v1 hatte das Passwort in der `.env`; v2 erzeugt es beim ersten Start selbst
(E16). Für einen **übernommenen** Bestand existiert die Rolle aber schon —
deshalb das alte Passwort mitnehmen, statt ein neues erzeugen zu lassen:

```
DEST=/mnt/user/appdata/musicmeta/config
printf '%s' "$AOFF_DB_PASSWORD_AUS_DER_ALTEN_ENV" > "$DEST/db-password"
chmod 0600 "$DEST/db-password"
```

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
Zurückschieben:

```
docker compose down                      # NICHT -v
mv "$DEST/18" "$SRC/18/docker"
docker compose -f docker-compose.yml -f docker-compose.watchdog.yml up -d
```

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
